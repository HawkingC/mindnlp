# coding=utf-8
# Copyright 2022 The OpenBMB Team and The HuggingFace Inc. team. All rights reserved.
# Copyright 2023 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# pylint: disable=C0103
""" MindNLP CPM Ant"""


import math
from typing import List, Optional, Tuple

import mindspore
from mindspore import nn, ops
from mindspore import Tensor, Parameter
from mindspore.nn import CrossEntropyLoss
from mindspore.common.initializer import initializer, Normal

from mindnlp.abc import PreTrainedModel
from .cpm_ant_config import CpmAntConfig
from ..utils.activations import ACT2FN

CPMANT_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "openbmb/cpm-ant-10b",
    # See all CPMAnt models at https://huggingface.co/models?filter=cpmant
]


class CpmAntLayerNorm(nn.Cell):
    """
    We use Root Mean Square (RMS) Layer Normalization, please see https://arxiv.org/abs/1910.07467 for details."
    """

    def __init__(self, config: CpmAntConfig):
        super().__init__()

        self.eps = config.eps
        self.dim_norm = config.hidden_size
        self.weight = Parameter(
            initializer("zeros", (config.hidden_size,)), name="weight"
        )

    def construct(self, hidden_states: Tensor):
        """
        Args:
            hidden_states (`Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        if hidden_states.shape[-1] != self.dim_norm:
            raise AssertionError("hidden_states.shape[-1] != self.dim_norm")
        old_dtype = hidden_states.dtype
        variance = (
            hidden_states.to(mindspore.float32).pow(2).mean(axis=-1, keep_dims=True)
        )
        hidden_states = (hidden_states * ops.rsqrt(variance + self.eps)).to(
            old_dtype
        ) * self.weight
        return hidden_states


class CpmAntAttention(nn.Cell):
    """Cpm Ant attention"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.dim_model = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.dim_head = config.dim_head

        self.project_q = nn.Dense(
            self.dim_model, self.num_heads * self.dim_head, has_bias=False
        )
        self.project_k = nn.Dense(
            self.dim_model, self.num_heads * self.dim_head, has_bias=False
        )
        self.project_v = nn.Dense(
            self.dim_model, self.num_heads * self.dim_head, has_bias=False
        )

        self.attention_out = nn.Dense(
            self.num_heads * self.dim_head, self.dim_model, has_bias=False
        )

        self.softmax = nn.Softmax(axis=-1)

        if config.dropout_p is not None:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_q: Tensor,
        hidden_kv: Tensor,
        attention_mask: Tensor,
        position_bias: Tensor,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[Tensor, Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_q (`Tensor`):
                Input of transformer block(self-attention block). It can be the raw embedding of a batch of sequences.
            hidden_kv (`Tensor` of shape `(batch, len_k, dim_model)`)):
                Tensor *key_value* and *query* of shape `(batch, len_k, dim_model)`
            attention_mask (`Tensor` of shape `(batch, len_seq, len_seq)`):
                Avoid invalid areas to participate in the calculation of self-attention.
            position_bias (`Tensor` of shape `(batch, len_seq, len_seq)`):
                Provide positional information to self-attention block.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple[Tensor, Tensor]`, *optional*):
                Cached past key and value projection states.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        batch_size = hidden_q.shape[0]
        len_q = hidden_q.shape[1]
        len_k = hidden_kv.shape[1]

        query = self.project_q(hidden_q)
        key = self.project_k(hidden_kv)
        value = self.project_v(hidden_kv)

        query = query.view(batch_size, len_q, self.num_heads, self.dim_head).transpose(
            0, 2, 1, 3
        )
        key = key.view(batch_size, len_k, self.num_heads, self.dim_head).transpose(
            0, 2, 1, 3
        )
        value = value.view(batch_size, len_k, self.num_heads, self.dim_head).transpose(
            0, 2, 1, 3
        )

        if past_key_values is not None:
            key = ops.cat([past_key_values[0], key], axis=-2)
            value = ops.cat([past_key_values[1], value], axis=-2)
            len_k = key.shape[-2]

        # (batch_size, num_heads, len_q, dim_head) @ (batch_size, num_heads, dim_head, len_k) -> (batch_size, num_heads, len_q, len_k)
        score = ops.matmul(query, key.swapaxes(-1, -2)) / math.sqrt(self.dim_head)
        score = score + position_bias

        score = ops.masked_fill(
            score,
            attention_mask.view(batch_size, 1, len_q, len_k) == Tensor(False),
            ops.scalar_to_tensor(float("-inf"), dtype=score.dtype),
        )
        score = self.softmax(score)

        score = ops.masked_fill(
            score,
            attention_mask.view(batch_size, 1, len_q, len_k) == Tensor(False),
            ops.scalar_to_tensor(0, dtype=score.dtype),
        )
        if output_attentions:
            attn_weights = score
        else:
            attn_weights = None

        if self.dropout is not None:
            score = self.dropout(score)

        # (batch_size, num_heads, len_q, len_k) @ (batch_size, num_heads, len_k, dim_head) -> (batch_size, num_heads, len_q, dim_head)
        score = ops.matmul(score, value)

        score = score.view(batch_size, self.num_heads, len_q, self.dim_head).transpose(
            0, 2, 1, 3
        )
        score = score.view(batch_size, len_q, self.num_heads * self.dim_head)

        score = self.attention_out(score)

        past_key_values = None
        if use_cache:
            past_key_values = (key, value)

        return score, attn_weights, past_key_values


class CpmAntSelfAttentionBlock(nn.Cell):
    """Cpm Ant self attn block"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.layernorm_before_attention = CpmAntLayerNorm(config)
        self.self_attention = CpmAntAttention(config)
        if config.dropout_p:
            self.dropout = nn.Dropout(config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_states: Tensor,
        attention_mask: Tensor,
        position_bias: Optional[Tensor] = None,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[Tensor, Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`Tensor` of shape `(batch, len_seq, dim_model)`):
                Input of transformer block(self-attention block). It can be the raw embedding of a batch of sequences.
            attention_mask (`Tensor` of shape `(batch, len_seq, len_seq)`):
                Avoid invalid areas to participate in the calculation of self-attention.
            position_bias (`Tensor` of shape `(batch, len_seq, len_seq)`):
                Provide positional information to self-attention block.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple(torch.FloatTensor)`, *optional*):
                Cached past key and value projection states.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        outputs = self.layernorm_before_attention(hidden_states)
        outputs = self.self_attention(
            outputs,
            outputs,
            attention_mask,
            position_bias,
            output_attentions,
            past_key_values,
            use_cache,
        )

        outputs, attn_weights, current_key_value = outputs

        if self.dropout is not None:
            outputs = self.dropout(outputs)
        hidden_states = hidden_states + outputs

        return hidden_states, attn_weights, current_key_value


class CpmAntDenseGatedACT(nn.Cell):
    """Cpm Ant dense gated act"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.w_0 = nn.Dense(config.hidden_size, config.dim_ff, has_bias=False)
        self.w_1 = nn.Dense(config.hidden_size, config.dim_ff, has_bias=False)
        self.act = nn.GELU(False)

    def construct(self, hidden_states: Tensor):
        """Transform an input tensor from one feature space to another via a nonlinear operation

        Args:
            hidden_states (`Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        gate_score = self.act(self.w_0(hidden_states))
        hidden_states = self.w_1(hidden_states)

        hidden_states = gate_score * hidden_states
        return hidden_states


class CpmAntFeedForward(nn.Cell):
    """Cpm Ant feedforward"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.w_in = CpmAntDenseGatedACT(config)
        if config.dropout_p is not None:
            self.dropout = nn.Dropout(p=config.dropout_p)
        else:
            self.dropout = None

        self.w_out = nn.Dense(config.dim_ff, config.hidden_size, has_bias=False)

    def construct(self, hidden_states: Tensor):
        """
        Args:
            hidden_states (`Tensor` of shape `(batch, seq_len, dim_in)`)
        """
        hidden_states = self.w_in(hidden_states)

        if self.dropout is not None:
            hidden_states = self.dropout(hidden_states)

        hidden_states = self.w_out(hidden_states)

        return hidden_states


class CpmAntFFNBlock(nn.Cell):
    """Cpm Ant ffn block"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.layernorm_before_ffn = CpmAntLayerNorm(config)
        self.ffn = CpmAntFeedForward(config)
        if config.dropout_p:
            self.dropout = nn.Dropout(config.dropout_p)
        else:
            self.dropout = None

    def construct(
        self,
        hidden_states: Tensor,
    ):
        """
        Args:
            hidden_states (`Tensor` of shape `(batch, len_seq, dim_model)`):
                Hidden states before feed forward layer.
        """
        ln_outputs = self.layernorm_before_ffn(hidden_states)
        outputs = self.ffn(ln_outputs)
        if self.dropout is not None:
            outputs = self.dropout(outputs)
        hidden_states = hidden_states + outputs
        return hidden_states


class CpmAntTransformerBlock(nn.Cell):
    """Cpm Ant Transformer block"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.self_att = CpmAntSelfAttentionBlock(config)
        self.ffn = CpmAntFFNBlock(config)

    def construct(
        self,
        hidden_states: Tensor,
        attention_mask: Tensor,
        position_bias: Optional[Tensor] = None,
        output_attentions: Optional[bool] = False,
        past_key_values: Optional[Tuple[Tensor, Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`Tensor`):
                Input to the layer of shape `(batch, seq_len, dim_model)`
            attention_mask (`Tensor`):
                Avoid invalid areas to participate in the calculation of shape `(batch, seq_len, seq_len)`
            position_bias (`Tensor`):
                Provides position information to attention mechanism of shape `(num_heads, seq_len, seq_len)`
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            past_key_values (`Tuple[Tensor, Tensor])`, *optional*):
                Cached past key and value projection states
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        hidden_states = self.self_att(
            hidden_states,
            attention_mask=attention_mask,
            position_bias=position_bias,
            output_attentions=output_attentions,
            past_key_values=past_key_values,
            use_cache=use_cache,
        )

        hidden_states, attn_weights, current_key_value = hidden_states

        hidden_states = self.ffn(hidden_states)

        return hidden_states, attn_weights, current_key_value


class CpmAntEncoder(nn.Cell):
    """Cpm Ant encoder"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()
        self.num_layers = config.num_hidden_layers
        self.layers = nn.CellList(
            [CpmAntTransformerBlock(config) for ith in range(self.num_layers)]
        )

        self.output_layernorm = CpmAntLayerNorm(config)

    def construct(
        self,
        hidden_states: Tensor,
        attention_mask: Tensor,
        position_bias: Tensor,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[Tuple[Tensor, Tensor]] = None,
        use_cache: Optional[bool] = None,
    ):
        """
        Args:
            hidden_states (`Tensor`):
                Input to the layer of shape `(batch, seq_len, dim_model)`
            attention_mask (`Tensor`):
                Avoid invalid areas to participate in the calculation of shape `(batch, seq_len, seq_len)`
            position_bias (`Tensor`):
                Provides position information to attention mechanism of shape `(num_heads, seq_len, seq_len)`
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers.
            past_key_values (`Tuple[Tensor, Tensor])`, *optional*):
                Cached past key and value projection states
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
        """
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        current_key_values = () if use_cache else None

        for i, layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)
            layer_outputs = layer(
                hidden_states,
                attention_mask,
                position_bias,
                output_attentions=output_attentions,
                past_key_values=past_key_values[i] if past_key_values else None,
                use_cache=use_cache,
            )
            hidden_states, attn_weights, current_key_value = layer_outputs
            if output_attentions:
                all_self_attns += (attn_weights,)
            if current_key_value is not None:
                current_key_values = current_key_values + (current_key_value,)

        hidden_states = self.output_layernorm(hidden_states)

        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        return hidden_states, current_key_values, all_hidden_states, all_self_attns


class CpmAntIntermediate(nn.Cell):
    """Cpm Ant intermediate"""
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.hidden_size, config.intermediate_size)
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def construct(self, hidden_states: Tensor) -> Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class CpmAntSegmentPositionEmbedding(nn.Cell):
    """Cpm Ant segment position embedding"""
    def __init__(self, config: CpmAntConfig):
        super().__init__()

        self.num_heads = config.num_attention_heads
        self.num_buckets = config.position_bias_num_buckets
        self.max_distance = config.position_bias_max_distance
        self.num_segments = config.segment_types

        self.relative_attention_bias = Parameter(
            initializer(
                "zeros",
                (
                    config.segment_types * config.segment_types
                    + config.position_bias_num_buckets,
                    config.num_attention_heads,
                ),
            ),
            "relative_attention_bias",
        )

    def construct(
        self,
        key_pos: Tensor,
        query_pos: Tensor,
        key_segment: Tensor,
        query_segment: Tensor,
    ):
        batch = key_pos.shape[0]
        keylen = key_pos.shape[1]
        querylen = query_pos.shape[1]

        if key_pos.shape[0] != query_pos.shape[0]:
            raise AssertionError(
                f"key_pos.shape[0] should be equal to query_pos.shape[0], but got {key_pos.shape[0]} and {query_pos.shape[0]}!"
            )
        if keylen != key_segment.shape[1] or querylen != query_segment.shape[1]:
            raise AssertionError(
                f"keylen should be equal to key_segment.shape[1], but got {keylen} and {query_segment.shape[1]}!"
            )
        if querylen != query_segment.shape[1]:
            raise AssertionError(
                f"querylen should be equal to query_segment.shape[1], but got {querylen} and {query_segment.shape[1]}!"
            )

        key_pos = key_pos.view((batch, -1, keylen))
        query_pos = query_pos.view((batch, querylen, -1))
        key_segment = key_segment.view((batch, -1, keylen))
        query_segment = query_segment.view((batch, querylen, -1))

        relative_position_bucket = self._segment_relative_position_bucket(
            query_segment, key_segment
        )
        relative_position_bucket = relative_position_bucket + self.num_buckets

        # (batch, len_q, len_k)
        absolute_position_bucket = self._position_bucket(
            ops.arange(keylen, dtype=mindspore.int32)[None, :]
            - ops.arange(querylen, dtype=mindspore.int32)[:, None],
            num_buckets=self.num_buckets,
            max_distance=self.max_distance,
        )
        relative_position_bucket = ops.where(
            (key_segment == query_segment),
            absolute_position_bucket[None, :, :],
            relative_position_bucket,
        )

        relative_position_bucket = ops.stop_gradient(relative_position_bucket)

        # (batch, len_q, len_k, num_heads)
        relative_position_bucket_shape = relative_position_bucket.shape
        embeds = ops.gather(
            self.relative_attention_bias, relative_position_bucket.reshape(-1), 0
        )
        embeds = embeds.reshape(relative_position_bucket_shape + (self.num_heads,))
        # (batch, num_heads, len_q, len_k)
        embeds = embeds.transpose(0, 3, 1, 2)
        return embeds

    def _segment_relative_position_bucket(self, query_segment, key_segment):
        return query_segment * self.num_segments + key_segment

    def _position_bucket(self, relative_position, num_buckets=32, max_distance=128):
        relative_buckets = 0
        # always bidirectional in CPMAnt
        num_buckets //= 2
        relative_buckets = (relative_position > 0).to(mindspore.int32) * num_buckets
        relative_position = ops.abs(relative_position)
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact
        relative_postion_if_large = max_exact + (
            ops.log(relative_position.float() / max_exact)
            / math.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        ).to(mindspore.int32)
        relative_postion_if_large = ops.minimum(
            relative_postion_if_large,
            ops.full_like(relative_postion_if_large, num_buckets - 1),
        )
        relative_buckets += ops.where(
            is_small, relative_position.to(mindspore.int32), relative_postion_if_large
        )
        return relative_buckets


# Copied from transformers.models.bert.modeling_bert.BertOutput with Bert->CPMAnt
class CpmAntOutput(nn.Cell):
    """Cpm Ant Output"""
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.intermediate_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(
            [config.hidden_size], epsilon=config.layer_norm_eps
        )
        self.dropout = nn.Dropout(p=config.hidden_dropout_prob)

    def construct(self, hidden_states: Tensor, input_tensor: Tensor) -> Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class CpmAntPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = CpmAntConfig
    base_model_prefix = "cpmant"
    supports_gradient_checkpointing = True
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    def _init_weights(self, cell):
        """Initialize the weights"""
        if isinstance(cell, nn.Dense):
            cell.weight.set_data(
                initializer(
                    Normal(self.config.init_std), cell.weight.shape, cell.weight.dtype
                )
            )
            if cell.bias is not None:
                cell.bias.set_data(
                    initializer("zeros", cell.bias.shape, cell.bias.dtype)
                )
        elif isinstance(cell, nn.Embedding):
            embedding_table = initializer(
                Normal(self.config.init_std),
                cell.embedding_table.shape,
                cell.embedding_table.dtype,
            )
            if cell.padding_idx is not None:
                embedding_table[cell.padding_idx] = 0
            cell.embedding_table.set_data(embedding_table)
        elif isinstance(cell, nn.LayerNorm):
            cell.gamma.set_data(initializer("ones", cell.gamma.shape, cell.gamma.dtype))
            cell.beta.set_data(initializer("zeros", cell.beta.shape, cell.beta.dtype))
        elif isinstance(cell, CpmAntLayerNorm):
            cell.weight.set_data(
                initializer("ones", cell.weight.shape, cell.weight.dtype)
            )
        elif isinstance(cell, CpmAntSegmentPositionEmbedding):
            cell.relative_attention_bias.set_data(
                initializer(
                    Normal(self.config.init_std),
                    cell.relative_attention_bias.shape,
                    cell.relative_attention_bias.dtype,
                )
            )


class CpmAntModel(CpmAntPreTrainedModel):
    """Cpm Ant Model"""
    def __init__(self, config: CpmAntConfig):
        super().__init__(config)
        self.encoder = CpmAntEncoder(config)
        self.segment_embedding = nn.Embedding(config.segment_types, config.hidden_size)
        self.input_embedding = nn.Embedding(
            config.vocab_size + config.prompt_types * config.prompt_length,
            config.hidden_size,
        )
        self.position_bias = CpmAntSegmentPositionEmbedding(config)
        self.prompt_length = config.prompt_length
        self.vocab_size = config.vocab_size

        self.post_init()

    def get_input_embeddings(self):
        return self.input_embedding

    def set_input_embeddings(self, value):
        self.input_embedding = value

    def _prepare_attention_mask(self, input_ids, span, context, length):
        batch = input_ids.shape[0]
        seqlen = input_ids.shape[1]
        directional_mask_2d = ops.arange(seqlen) <= ops.arange(seqlen).view(-1, 1)
        attention_mask = context[:, None, :] | (
            context[:, :, None].logical_not()
            & directional_mask_2d.view(1, seqlen, seqlen)
        )
        attention_mask = attention_mask & (span[:, None, :] == span[:, :, None])
        # mask for left padding
        mask_1d = (
            Tensor(list(range(seqlen - self.prompt_length))[::-1])[None, :].tile(
                (batch, 1)
            )
            < length[:, None]
        )
        mask_1d = ops.cat(
            (ops.ones((batch, self.prompt_length)).bool(), mask_1d), axis=1
        )
        attention_mask = (
            mask_1d.view(batch, seqlen, 1)
            & mask_1d.view(batch, 1, seqlen)
            & attention_mask
        )
        return attention_mask

    def construct(
        self,
        input_ids: Optional[Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        past_key_values: Optional[Tuple[Tuple[Tensor]]] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ):
        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        # add prompts ahead
        if input_ids.dtype != mindspore.int32:
            input_ids = input_ids.to(mindspore.int32)
        dtype = input_ids.dtype
        segment = ops.where(input_ids != 0, Tensor(2), Tensor(0)).to(dtype=dtype)
        length = (segment != 0).sum(-1).to(dtype=dtype)
        input_ids = ops.cat(
            (
                ops.arange(
                    self.prompt_length * 2 + self.vocab_size,
                    self.prompt_length * 3 + self.vocab_size,
                    dtype=dtype,
                ).tile((input_ids.shape[0], 1)),
                input_ids,
            ),
            axis=1,
        )
        batch, seq_length = input_ids.shape
        segment = ops.cat(
            (ops.zeros((batch, self.prompt_length), dtype=dtype), segment), axis=1
        )
        context = ops.full((batch, seq_length), 1, dtype=dtype)
        position = ops.arange(seq_length, dtype=dtype).tile((batch, 1))
        span = ops.full((batch, seq_length), 0, dtype=dtype)

        if past_key_values is None:
            past_length = 0
            past_key_values = tuple([None] * self.encoder.num_layers)
            hidden_states = self.input_embedding(input_ids)
            segment_states = self.segment_embedding(segment)
            hidden_states = hidden_states + segment_states
        else:
            past_length = past_key_values[0][0].size(-2)
            segment_states = self.segment_embedding(segment)
            hidden_states = self.input_embedding(input_ids) + segment_states[:, -1:, :]

        attention_mask = self._prepare_attention_mask(input_ids, span, context, length)
        position_bias = self.position_bias(position, position, segment, segment)

        attention_mask = attention_mask[:, past_length:, :]
        position_bias = position_bias[:, :, past_length:, :]
        hidden_states = hidden_states[:, past_length:, :]

        (
            hidden_states,
            present_key_values,
            all_hidden_states,
            all_attentions,
        ) = self.encoder(
            hidden_states,
            attention_mask,
            position_bias,
            output_attentions,
            output_hidden_states,
            past_key_values,
            use_cache,
        )

        if past_length == 0:
            hidden_states = hidden_states[:, self.prompt_length :, :]
            # drop the prompt
            if all_attentions is not None:
                new_attentions = ()
                for attention in all_attentions:
                    new_attentions += (
                        attention[:, :, self.prompt_length :, self.prompt_length :],
                    )
                all_attentions = new_attentions
            if all_hidden_states is not None:
                new_hidden_states = ()
                for hidden_state in all_hidden_states:
                    new_hidden_states += (hidden_state[:, self.prompt_length :, :],)
                all_hidden_states = new_hidden_states

        return tuple(
            v
            for v in [
                hidden_states,
                present_key_values,
                all_hidden_states,
                all_attentions,
            ]
            if v is not None
        )


class CpmAntForCausalLM(CpmAntPreTrainedModel):
    """Cpm Ant for causal lm"""
    _keys_to_ignore_on_load_missing = [r"lm_head.weight"]

    def __init__(self, config: CpmAntConfig):
        super().__init__(config)
        self.cpmant = CpmAntModel(config)

        # lm_head.weight is tied to cpmant.input_embedding.weight
        self.lm_head = nn.Dense(
            config.hidden_size,
            config.vocab_size + config.prompt_types * config.prompt_length,
            has_bias=False,
        )
        self.post_init()

    def construct(
        self,
        input_ids: Optional[Tensor] = None,
        past_key_values: Optional[List[Tuple[Tensor, Tensor]]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        labels: Optional[Tensor] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ) -> Tuple:
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        model_output = self.cpmant(
            input_ids,
            output_attentions,
            output_hidden_states,
            past_key_values,
            use_cache,
            return_dict,
        )
        hidden_states = (
            model_output.last_hidden_state if return_dict else model_output[0]
        )

        logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            loss_func = CrossEntropyLoss()
            loss = loss_func(logits.view(-1, logits.shape[-1]), labels.view(-1))

        output = (logits,) + model_output[1:]
        return ((loss,) + output) if loss is not None else output

    def get_input_embeddings(self):
        return self.cpmant.input_embedding

    def set_input_embeddings(self, value):
        self.cpmant.input_embedding = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        """set output embeddings"""
        self.lm_head = new_embeddings

    def prepare_inputs_for_generation(self, input_ids, **kwargs):
        """prepase inputs for generation"""
        input_ids = input_ids.int()
        # save the memory usage of dummy attention mask
        if "attention_mask" in kwargs:
            kwargs["attention_mask"] = ops.zeros(1, 1)

        return {
            "input_ids": input_ids,
            "use_cache": kwargs["use_cache"],
            "past_key_values": kwargs.get("past_key_values", None),
        }

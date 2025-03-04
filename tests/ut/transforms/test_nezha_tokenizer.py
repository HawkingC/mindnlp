# Copyright 2023 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Test the NezhaTokenizer"""

import mindspore as ms
from mindspore.dataset import GeneratorDataset
from mindnlp.transforms import NezhaTokenizer


def test_nezha_tokenizer_from_pretrained():
    """test NezhaTokenier from pretrained"""
    texts = ['i make a small mistake when i\'m working! 床前明月光']
    test_dataset = GeneratorDataset(texts, 'text')

    bert_tokenizer = NezhaTokenizer.from_pretrained('sijunhe/nezha-cn-base', return_token=True)
    test_dataset = test_dataset.map(operations=bert_tokenizer)
    dataset_after = next(test_dataset.create_tuple_iterator())[0]

    assert len(dataset_after) == 21
    assert dataset_after.dtype == ms.string

def test_nezha_tokenizer_add_special_tokens():
    """test add special tokens."""
    nezha_tokenizer = NezhaTokenizer.from_pretrained('sijunhe/nezha-cn-base')
    cls_id = nezha_tokenizer.token_to_id("[CLS]")

    assert cls_id is not None

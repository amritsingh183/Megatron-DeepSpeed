# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""ORQA finetuning/evaluation."""

from functools import partial
import sys

import math
import torch
import torch.nn.functional as F

from megatron import get_args
from megatron import get_timers
from megatron import get_tokenizer
from megatron import mpu
from megatron import print_rank_0
from megatron.utils import average_losses_across_data_parallel_group
from megatron.model.biencoder_model import biencoder_model_provider
#from tasks.t5_model_utils.finetune_utils_open_retrieval import accuracy_func_provider
#from tasks.t5_model_utils.finetune_utils_open_retrieval import finetune
from pretrain_ict import get_group_world_size_rank
from tasks.finetune_utils import finetune
from tasks.orqa.supervised.eval_utils import accuracy_func_provider
from tasks.orqa.supervised.eval_utils import process_batch, task_collate_fn
from tasks.orqa.evaluate_utils import ORQAEvaluator
from megatron.indexer import IndexBuilder

def orqa(Dataset): # , name_from_datapath_func):

    def cross_entropy_forward_step(batch, model):
        """Simple forward step with cross-entropy loss."""
        args = get_args()
        timers = get_timers()
        tokenizer = get_tokenizer()

        # Get the batch.
        timers('batch generator').start()
        try:
            batch_ = next(batch)
        except BaseException:
            batch_ = batch

        query_tokens, query_mask, query_types, query_pad_mask, \
        context_tokens, context_mask, context_types, context_pad_mask, \
        neg_context_tokens, neg_context_mask, neg_context_types, \
        reference = process_batch(batch_)

        timers('batch generator').stop()
        local_batch_size = query_tokens.shape[0]

        # Text representation of query and context
        query_list, context_list = [], []
        for i in range(local_batch_size):
            query_list.append(tokenizer.decode(query_tokens[i].tolist()))
            context_list.append(tokenizer.decode(context_tokens[i].tolist()))

        if neg_context_tokens is not None:
            context_tokens = torch.cat([context_tokens, neg_context_tokens])
            context_mask = torch.cat([context_mask, neg_context_mask])
            context_types = torch.cat([context_types, neg_context_types])

        # Forward model.
        #query_logits, context_logits = model(query_tokens, query_mask, 
        output_tensor = model(query_tokens, query_mask, 
                                        query_types, context_tokens, 
                                        context_mask, context_types)

        return output_tensor, partial(cross_entropy_loss_func_, query_tokens, context_tokens)


    #def cross_entropy_loss_func(labels, output_tensor):
    def cross_entropy_loss_func_(query_tokens, context_tokens, output_tensor):
        args = get_args() 

        local_batch_size = query_tokens.shape[0]
        group, rank, world_size = get_group_world_size_rank()
        # recall we assert that model_parallel_size == 1
        global_batch_size = world_size * local_batch_size

        query_logits, context_logits = output_tensor

        if world_size > 1:
            input_ = torch.empty_like(context_logits).copy_(\
                context_logits).detach_()
            tensor_list = [torch.empty_like(input_) for _ in range(world_size)]
            tensor_list[rank].copy_(input_)
            torch.distributed.all_gather(tensor_list, input_, group=group)

            # Check if all-gather happens in order
            assert tensor_list[rank].sum().item() == \
                context_logits.sum().item()

            # Preserves the gradient
            tensor_list[rank] = context_logits
            all_context_logits = torch.cat(tensor_list, dim=0).contiguous()

            # Query tensors
            input_ = torch.empty_like(query_logits).copy_(\
                query_logits).detach_()
            tensor_list = [torch.empty_like(input_) for _ in range(world_size)]
            tensor_list[rank].copy_(input_)
            torch.distributed.all_gather(tensor_list, input_, group=group)

            # Check if all-gather happens in order
            assert tensor_list[rank].sum().item() == query_logits.sum().item()

            # Preserves the gradient
            tensor_list[rank] = query_logits
            all_query_logits = torch.cat(tensor_list, dim=0).contiguous()
        else:
            all_query_logits = query_logits
            all_context_logits = context_logits

        retrieval_scores = torch.matmul(all_query_logits,
                            torch.transpose(all_context_logits, 0, 1))
        # Scaling the retrieval scores
        if args.retriever_score_scaling:
            retrieval_scores = retrieval_scores / math.sqrt(args.hidden_size)

        if args.train_with_neg:
            # if the world size is 3, local batch size is 4, and
            # local context size is 8, what we want is
            # labels = [0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19]
            labels = []
            local_context_size = context_tokens.shape[0]
            for i in range(world_size):
                j = i * local_context_size
                labels.extend(list(range(j, j + local_batch_size)))
            labels = torch.LongTensor(labels).cuda()
            assert len(labels) == global_batch_size
        else:
            labels = torch.arange(global_batch_size).long().cuda()

        # Cross-entropy loss.
        softmax_scores = F.log_softmax(retrieval_scores, dim=1)

        loss = F.nll_loss(softmax_scores, labels, reduction='mean')

        max_score, max_idxs = torch.max(softmax_scores, 1)
        correct_predictions_count = (max_idxs == labels).sum().float()

        # Reduce loss for logging.
        reduced_loss = average_losses_across_data_parallel_group([loss, \
            correct_predictions_count])

        # Loss scaling for correct losses in Supervised Retrieval
        loss = loss * mpu.get_data_parallel_world_size()

        return loss, {'lm loss': reduced_loss[0],
                      'correct_prediction_count': reduced_loss[1]}


    def train_valid_datasets_provider():
        """Build train and validation dataset."""
        args = get_args()
        tokenizer = get_tokenizer()

        train_dataset = Dataset('training',
                                args.train_data,
                                tokenizer,
                                args.retriever_seq_length,
                                evaluate=False)
        valid_dataset = Dataset('validation',
                                args.valid_data,
                                tokenizer,
                                args.retriever_seq_length,
                                evaluate=True)
        return train_dataset, valid_dataset

    def model_provider(pre_process=True, post_process=True):
        """Build the model."""
        args = get_args()
        print_rank_0('building retriever model for {} ...'.format(args.task))
        args.only_context_model=False
        args.only_query_model=False
        model = biencoder_model_provider()
        
        #model = biencoder_model_provider(only_context_model=False,
        #            only_query_model=False, 
        #            biencoder_shared_query_context_model=\
        #            args.biencoder_shared_query_context_model,
        #            pre_process=pre_process, post_process=post_process)
        return model

    def single_dataset_provider(datapath):
        args = get_args()
        tokenizer = get_tokenizer()

        #name = name_from_datapath_func(datapath)
        name = datapath[0].split('/')[-1].split('.')[0]
        return Dataset(name,
                       datapath,
                       tokenizer,
                       args.retriever_seq_length,
                       evaluate=True)

    #def distributed_metrics_func_provider():
    def metrics_func_provider():
        """Provide metrics callback function."""

        #def name_from_datapath(datapath):
        #    return datapath[0].split('/')[-1].split('.')[0]
        
        return accuracy_func_provider(single_dataset_provider)

    #def rank0_metrics_func_provider(datapath):
    #    """Provide metrics callback function."""
    #    return accuracy_func_provider(single_dataset_provider, datapath,
    #                                  rank0sampler=True)

    """Finetune/evaluate."""
    finetune(train_valid_datasets_provider,
             model_provider,
             forward_step=cross_entropy_forward_step,
             end_of_epoch_callback_provider=metrics_func_provider,
             task_collate_fn=task_collate_fn)
            #,end_of_training_callback_provider=rank0_metrics_func_provider)


def main():
    args = get_args()

    if args.task == 'RET-FINETUNE-NQ':
        from tasks.orqa.supervised.data import NQSupervisedDataset as Dataset

        #def name_from_datapath(datapath):
        #    return datapath[0].split('/')[-1].split('.')[0]

    else:
        raise NotImplementedError('ORQA task {} is not implemented.'.format(
            args.task))

    orqa(Dataset) #, name_from_datapath)


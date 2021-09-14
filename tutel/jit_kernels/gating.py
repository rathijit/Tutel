# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import os
import torch
from ..impls.jit_compiler import JitCompiler


disable_gate_opt = int(os.environ.get('GATE', '1')) == 0
cumsum_kernels = dict()

def get_cumsum_kernel(samples, global_experts):

  if disable_gate_opt:
    print('[WARN]', "Optimized cumsum is disabled, and may result in big performance regression.")

    def torch_cumsum(mask1):
        locations1 = torch.cumsum(mask1, dim=0) - 1
        return locations1
    return torch_cumsum

  global cumsum_kernels
  if samples in cumsum_kernels:
    return cumsum_kernels[samples]

  base_kernel = JitCompiler.generate_kernel({'batch_num': global_experts, 'num_samples': samples}, '''
    #define thread_num  1024
    #define batch_num   (@batch_num@)

    extern "C" __global__ void cumsum(int* input0 /* (num_samples, batch_num) */, int* output0 /* (num_samples, batch_num) */) {
        // [thread_extent] blockIdx.x = @batch_num@
        // [thread_extent] threadIdx.x = 1024
        __shared__ int temp[thread_num + 1];
        int thid = threadIdx.x, bid = blockIdx.x;
        int last_sum = -1;

        for (int S = 0; S < @num_samples@; S += thread_num, output0 += thread_num * batch_num, input0 += thread_num * batch_num) {
            int offset = 1;
            if (S + thid < @num_samples@)
                    temp[thid] = input0[thid * batch_num + bid];
            for (int d = thread_num >> 1; d > 0; d >>= 1) {
                    __syncthreads();
                    if (thid < d)
                            temp[offset * (2 * thid + 2) - 1] += temp[offset * (2 * thid + 1) - 1];
                    offset *= 2;
            }
            if (thid == 0)
                    temp[thread_num] = temp[thread_num - 1], temp[thread_num - 1] = 0;
            for (int d = 1; d < thread_num; d *= 2) {
                    offset >>= 1;
                    __syncthreads();
                    if (thid < d) {
                            int ai = offset * (2 * thid + 1) - 1;
                            int bi = offset * (2 * thid + 2) - 1;
                            int t = temp[ai];
                            temp[ai] = temp[bi];
                            temp[bi] += t;
                    }
            }
            __syncthreads();
            if (S + thid < @num_samples@)
                    output0[thid * batch_num + bid] = temp[thid + 1] + last_sum;
            last_sum += temp[thread_num];
        }
    }
  ''')

  def optimized_cumsum(mask1):
    locations1 = torch.empty(mask1.shape, dtype=torch.int32, device=mask1.device).contiguous()
    base_kernel(mask1.to(torch.int32), locations1)
    return locations1

  cumsum_kernels[samples] = optimized_cumsum
  return optimized_cumsum

def fast_cumsum_sub_one(data, dim=0):
  if data.dim() != 2 or dim != 0:
    raise Exception("Unimplemented fast_cumsum_sub_one() of data = %s and dim = %s" % (data.size(), dim))
  return get_cumsum_kernel(data.size(0), data.size(1))(data)

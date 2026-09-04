# Copyright 2024 ByteDance and/or its affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build Protenix's task-owned Hopper layer-normalization extension once."""

import os
from typing import Any, Optional

from torch.utils.cpp_extension import load


def compile(
    name: str,
    sources: list[str],
    extra_include_paths: list[str],
    build_directory: Optional[str] = None,
) -> Any:
    # This image is built as a Hopper acceptance candidate. Keep both an SM90 cubin
    # and compute_90 PTX in this one extension. The rest of pinned PyTorch 2.7.1
    # + CUDA 12.6 is not Blackwell-capable, so this does not imply whole-image
    # forward compatibility.
    os.environ["TORCH_CUDA_ARCH_LIST"] = "9.0+PTX"
    gencode_flags = [
        "-gencode",
        "arch=compute_90,code=sm_90",
        "-gencode",
        "arch=compute_90,code=compute_90",
    ]

    return load(
        name=name,
        sources=sources,
        extra_include_paths=extra_include_paths,
        extra_cflags=[
            "-O3",
            "-DVERSION_GE_1_1",
            "-DVERSION_GE_1_3",
            "-DVERSION_GE_1_5",
        ],
        extra_cuda_cflags=[
            "-O3",
            "--use_fast_math",
            "-DVERSION_GE_1_1",
            "-DVERSION_GE_1_3",
            "-DVERSION_GE_1_5",
            "-std=c++17",
            "-maxrregcount=32",
            "-U__CUDA_NO_HALF_OPERATORS__",
            "-U__CUDA_NO_HALF_CONVERSIONS__",
            "--expt-relaxed-constexpr",
            "--expt-extended-lambda",
        ]
        + gencode_flags,
        verbose=True,
        build_directory=build_directory,
    )

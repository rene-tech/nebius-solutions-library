# Public support boundaries

Checked 2026-09-15 with NVIDIA's primary documentation and Tavily extraction.
Tavily request ID: `121d2d56-0dc6-4397-8899-1ef14f70a41b`.

- The [support matrix](https://docs.nvidia.com/bionemo/inference-runtime/references/support-matrix/)
  lists full processing pipelines for OpenFold2, OpenFold3 and Boltz. Protenix
  v2 and Boltz2 affinity expose model modules without a complete processing
  pipeline. A native adapter remains necessary; a module listing does not
  demonstrate end-to-end serving or feature parity.
- The same matrix distinguishes diffusion CUDA-graph support (Boltz,
  OpenFold3 and Protenix) from OpenFold2, which has no such module. This is
  independent of CUDA process checkpoint/restore.
- The [installation requirements](https://docs.nvidia.com/bionemo/inference-runtime/install/)
  explicitly include H100 and L40S among release-qualified GPUs. Our exact
  image, installed dependency versions and test GPU/driver are still recorded
  separately: hardware qualification does not qualify our custom adapters.

These sources establish eligibility, not speedup. All reported speedups come
from the retained local measurements, not NVIDIA's published benchmark numbers.

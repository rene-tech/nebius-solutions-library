# CUDA-aware Open MPI counterfactual

This additive candidate leaves the functional `c6c353e5…` release untouched.
It replaces **only Open MPI 5.0.8**, using the same source archive, compiler,
CUDA toolkit, dependencies, and configure options plus one explicit library
directory. GROMACS, UCX, worker code and scientific inputs remain inherited
from the exact functional image. A successful CPU build is not GPU or customer
qualification.

## Confirmed cause

The retained original build image is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs-engine@sha256:7e531c7b97c8dc7c0aea115396ff2f63f04acf9f0ae00593fa0a9a5ed9369945`.
Its original `/src/openmpi-5.0.8/config.log` records two automatically discovered
driver library directories: `cuda/compat` and `cuda/targets/x86_64-linux/lib/stubs`.
They become a newline-containing library directory. The CUDA symbol link check
then passes the second directory as a bare linker input, producing a file-format
error. Configure nevertheless succeeds with `OPAL_CUDA_SUPPORT=0`. The installed
`MPIX_CUDA_AWARE_SUPPORT` is zero and the initialized real-GPU query returns zero.
UCX's independent CUDA transports do not repair that Open MPI build flag.

The original GROMACS cache records `MPI_SUPPORTS_CUDA_AWARE_DETECTION=1`, and
`libgromacs_mpi.so` imports `MPIX_Query_cuda_support`. Thus no GROMACS source,
binary, physics or compiler change is needed for this controlled candidate.
The original configure log, headers and cache are retained inside the candidate
alongside the corrected logs. The original upstream archive SHA256 is
`f891ddf2dab3b604f2521d0569615bc1c07a1ac86a295ac543e059aecb303621`.

## Build gates

From the runtime directory:

```sh
docker buildx build --platform linux/amd64 \
  -f Containerfile.mpi-cuda-aware --build-arg BUILD_JOBS=8 \
  --build-arg SOURCE_REVISION=EXACT_COMMIT \
  --tag TASK_OWNED_CANDIDATE --load .
python3 -m unittest discover -s ../tests -p test_mpi_cuda_aware.py -v
```

The build requires the positive configure macro, installed extension macro,
`ompi_info` compiled capability and CUDA accelerator component. It also verifies
the unchanged GROMACS query path and rejects installed MPI libraries whose
runtime search path points to driver stubs. Stubs are **build-only**; do not put
them in `LD_LIBRARY_PATH`. Full original/corrected configure, make and install
logs, hashes and a compile-time-only receipt live at
`/opt/fs2-cuda-aware/provenance`.

On the CPU builder, `ompi_info` cannot load the CUDA component because no GPU
driver `libcuda.so.1` is injected. The build gate therefore checks the installed
ELF's actual exported CUDA-component symbol, while the separate real-GPU gate
requires runtime support. The first positive compilation was correctly retained
but its initial validator wrongly required CPU-time DSO loadability; that failed
receipt/build log is preserved. No driver stub was added to runtime paths to
make that check pass.

## Real-GPU gate, separately authorized

Reserve genuinely free QA GPUs before creating a task-owned Pod. Use the
immutable candidate digest, collect GPU UUID/driver/topology and execute:

```sh
timeout 90 /opt/ompi/bin/mpirun -np 2 --mca pml ucx \
  --mca pml_ucx_tls any --mca pml_ucx_devices any \
  -x UCX_TLS=self,sm,cuda_copy,cuda_ipc \
  /opt/fs2-cuda-aware/mpi-device-probe
```

The probe must report initialized query `1` on every rank, distinct physical GPU
UUIDs, and exact integer contents after blocking/nonblocking ring transfers and
Allreduce using actual `cudaMalloc` buffers (4 B, 4 KiB and 1 MiB; three
point-to-point repetitions). No force flag is permitted. One rank is an optional
initialization/loopback check; it does not prove inter-GPU communication.
Keep timeout, nonzero exit and every diagnostic; clean only the owned Pod.

This is a correctness screen, not bandwidth, RDMA, multi-node or speedup proof.
Before any promotion, rerun exact-image matched native workflows, numerical
checks, checkpoint/recovery and the parent-owned hosted acceptance. Historical
functional or PME-control results do not qualify the changed image.

Primary sources (accessed 2026-10-03):

- [Open MPI 5.0.8 CUDA build instructions](https://docs.open-mpi.org/en/v5.0.8/tuning-apps/networking/cuda.html)
- [MPIX_Query_cuda_support semantics](https://docs.open-mpi.org/en/v5.0.8/man-openmpi/man3/MPIX_Query_cuda_support.3.html)
- [Exact Open MPI CUDA configure macro](https://github.com/open-mpi/ompi/blob/v5.0.8/config/opal_check_cuda.m4)
- [Exact GROMACS query detection](https://github.com/gromacs/gromacs/blob/da9e013175bae98b31b34384f6b4864ff29f65a5/cmake/gmxManageGpuAwareMpi.cmake)

Private raw evidence is in
`${FS2_OPERATOR_EVIDENCE}/fs2-gromacs-mpinat-20261003/runtime-cuda-aware-r2/`.
The original configuration is preserved under `original-build/`. A first build
attempt failed before compilation because the existing worker `.dockerignore`
excluded the new probe files; its log remains retained. The additive
Dockerfile-specific ignore file fixes only this candidate build context.

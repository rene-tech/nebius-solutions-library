# Scientific startup measurements

These probes distinguish companion startup, model loading, same-process CUDA
suspend/resume, and a persisted process restore. They are not interchangeable
end-to-end cold-start measurements.

## Companion dispatch

`benchmark_companion_startup.py` alternates fresh baseline and candidate Python
processes verifying the same deterministic artifact. On the retained H100
cluster, with the unchanged 500m CPU / 256Mi container limit, three repetitions
per variant measured:

| Operation | Baseline median | Candidate median |
| --- | ---: | ---: |
| Fresh companion process and 1,048,572-byte artifact verification | 13.761s | 3.427s |

This is a 75.1% reduction of that operation, not of full model startup. The
candidate changes imports/dispatch only; artifact verification remains the same.

## ESMFold2 CUDA RAM proof (2026-09-06)

`probe_esmfold2_cuda_checkpoint.py` loads the pinned production ESMFold2 trunk
and ESMC-6B model, runs ordinary controls with two sequences, then performs three
CUDA lock/checkpoint/restore/unlock cycles. It compares all model-state bytes
before and after each cycle and submits fresh scientific requests after restore.

- Hardware: H100 80GB HBM3, driver 580.159.04, CUDA 13, PyTorch 2.11.0+cu130.
- Runtime image: `sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3`.
- Production artifact mounts: ESMFold2 trunk `136a3580c01cc055ae5a1278bae056e5150a5441ddb89dfbafb9f4e88d763a0c`,
  ESMC-6B `8f21da30919b3e0d7af9ec6c4b9879542234d77d42ce061fef029397a4d39758`,
  CCD `b1c2fe19204c57f7a7cca6ab4cb0cb420b99312fff424ef2e405fc8234b7616e`.
- Checkpoint times: 8.500s, 8.375s, 9.010s; restore times: 3.520s, 3.409s, 4.071s.
- GPU memory reported 0MiB while checkpointed in every cycle.
- All 2,396 model-state tensors (13,629,851,916 bytes) matched exactly after all
  three restores. Two different sequences produced valid structures containing
  506 and 241 atoms with finite confidence values in the same bounds used by the
  scientific qualification. The smoke inputs use one loop/four sampling steps;
  these are not full scientific-quality production performance estimates.
- Identical seeds are not a bitwise-output guarantee: ordinary controls already
  showed small confidence variation. An earlier overstrict confidence-equality
  attempt failed and was retained separately rather than counted as a success.

This proves a CUDA primitive on this runtime. **It does not persist a checkpoint,
release the pod's Kubernetes GPU allocation, or enable production L2/L3/L4.**
The disposable GPU probe was removed; no host driver or policy was changed.

Private evidence (no credentials in these receipts):

| Receipt under `/home/tux/.local/state/fs2-startup-remediation-20260906/` | SHA-256 |
| --- | --- |
| `companion-startup-benchmark.json` | `5ce8598bec6646ef2e4114b38bba025912ece50130374de8c2d4f8d6bbb4ccc3` |
| `esmfold2-cuda-ram-controls-probe.json` | `1cca165238ddbfb82ea0b8d8fc6d8608a8aae5d90f89a9b19d0a0d1f34f3d789` |
| `esmfold2-control-restored-structures.tar` | `9388bfb8696eca8255eab789feb161c6f103bf5a3972bcf6edccd51384a222c6` |

NVIDIA's [pinned CUDA checkpoint documentation](https://github.com/NVIDIA/cuda-checkpoint/tree/00d5cce84c628088d6caa203fc4af40c1538b6f7)
lists driver 580 GPU migration and container partial passthrough support. UVM
and `cuMemExportToShareableHandle()` IPC have separate restrictions. A model
must be tested; GPU family or driver version alone does not qualify it.

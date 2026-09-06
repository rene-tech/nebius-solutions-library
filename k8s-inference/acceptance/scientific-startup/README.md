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

## Fresh-pod persisted ESMFold2 proof

The same immutable model image/artifacts above, with the versioned runtime
scripts captured in `persistent-trimmed/runtime-source.json`, passed three
independent CUDA/CRIU restores. Each repetition deleted the preceding GPU pod
before creating a new pod and verified all tensor bytes before serving two
different inputs at the full 20-loop/200-step settings. This does release and
reacquire the Kubernetes GPU allocation; it is not only same-process offload.

| Repetition | Pod creation to ready | CRIU restore | CUDA restore | 65aa inference | 34aa inference |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 12.687s | 8.590s | 2.319s | 3.107s | 2.789s |
| 2 | 11.453s | 8.272s | 2.343s | 3.102s | 2.828s |
| 3 | 11.453s | 8.298s | 2.332s | 3.132s | 2.810s |

Cache identity: same H100 node/GPU and approximately 19Gi checkpoint on a
128Gi Network SSD PVC, **OS file page cache retained**. No local NVMe or
accounted/reserved RAM tier was used. The six outputs contained 506/241 atoms
and bounded confidence; all 2,396 model tensors matched SHA-256
`e2cf596cd20db6910e534ea62719a8c8fe2bd2cd8cead03ba43b48475c37dbf9`.
The one capture included 320.184s of fsync in addition to CUDA 6.113s and CRIU
9.494s. Capture was fully durable before deleting the donor.

An earlier 31Gi capture failed fresh-pod restore because a Triton executable
mapping lived in the deleted container. The fix persists Torch/Triton/CUDA
generated-code caches beside the checkpoint. Unused allocator trimming reduced
reserved CUDA memory from 26.77GB to 14.32GB while preserving all live tensors;
that one-shot size reduction is not reported as a three-repetition speedup.

Evidence under the same private evidence root:

- `persistent-trimmed/receipt.json`, SHA-256
  `13c21b290dc3fad7968fae3aa1ee5128492ac31cca5c5ade1da1e707a50bc407`.
- `persistent-trimmed/runtime-source.json`, SHA-256
  `28fe3fc8fd4005f1e9f02c51ab745d965684ceecfb94c8faab8376523a7b1ab3`.
- `persist-trimmed-capture.json`, `persist-trim-receipt.json`, six CIF files
  and each restored pod's per-step lifecycle log.

For disk-cold qualification, `benchmark_persistent_restore.py` additionally
accepts `--eviction-holder POD --checkpoint-directory /checkpoints/RUN/images`.
It deletes the previous GPU process first, fsyncs and applies `POSIX_FADV_DONTNEED`
only to this checkpoint's `.img` files, then creates the fresh restore pod.
It never drops host-wide caches. Log files are excluded because they are not
checkpoint memory and may be private to CRIU's root process. An interrupted
eviction attempt may be resumed with explicit `--donor-already-deleted`.

These isolated receipts do not claim the optional original-scientific-command
bridge, another GPU UUID, ESMFold2-Fast, or other scientific models are qualified.

### Disk-cold result

One clean disk-cold repetition also passed exact tensor validation and both
full-production variable requests. After the previous process was deleted,
file-scoped eviction covered all checkpoint `.img` files (~20.15GB); the restored
process's `/proc/PID/io` reported ~20.15GB of actual reads. Pod-to-ready was
**324.646s**, including CRIU 321.422s and CUDA restore 2.320s. Inference afterward
took 3.112s/2.879s and again returned 506/241-atom structures.

This result is materially slower than ordinary ~15s model loading. Keep normal
loading as the default; the warm-file-cache result does not justify enabling
this Network-SSD L2 path. One cold repetition establishes this constraint,
not a statistically qualified performance distribution. Receipt:
`persistent-disk-cold-r2/receipt.json`, SHA-256
`d1088a90e4eaaa745c0c827970866850e8ed169b8e03a3da5e488c562d21585c`.

## Original scientific-wrapper bridge

The immutable bridge image
`sha256:5a275d2d0c7707de24c8ad4c793b9c56d206bafe03ed54d3d0333bbfd075521f`
was captured once with the same runtime/model identities. Its `r3` capture
completed CUDA checkpoint in 6.068s, CRIU dump in 10.834s, and durable file/cache
flush in 316.477s. The donor pod was deleted before fresh request pods started.

A real new public ESMFold2 ubiquitin request supplied its unchanged frozen argv,
prepared handoff, runtime-localization marker and generated stage-runner. This
76-residue input and seed102 differ from the donor's two warmup requests. The
public normal request passed independently. The isolated bridge then passed:

- Read-only shared `images/`, per-attempt writable cache/log scratch, and a
  separate original scientific workspace. Checkpoint pages were not copied.
- Root supervisor -> unchanged stage-runner/client UID/GID10001 -> original
  scientific fold, with full 20-loop/200-step settings and no smoke shortcut.
- A separate UID10001 reader validated the exact original-command digest in
  the mode-0400 completion marker, mode-0600 confidence file and mode-0644 CIF.
  All three files were owned by UID10001. Pods completed and released their GPU.
- Strict restored execution passed in 21.230s and 21.112s pod-to-completion.
  The explicit missing-checkpoint normal-load fallback passed in 34.254s.
  These bounded functional runs are not a three-repetition speedup claim.
- The paired result produced a 49,279-byte CIF for each path; restored/ordinary
  mean pLDDT was 0.802201/0.802381, consistent with ordinary numerical variation.

The diagnostic paired receipt is
`bridge-qualified/readonly-wrapper-final/receipt.json` (SHA-256
`a53f024fb08af5c803d2ee06027067892a5d5a0f39d2c51e4ac7e2964b933985`).
Its pod commands also logged GPU visibility and a lightweight `cuInit` result.
Initial fallback probes failed because the *probe harness* blindly copied
`NVIDIA_VISIBLE_DEVICES=void` into `CUDA_VISIBLE_DEVICES`; this is now conditional
on an actual GPU UUID. It was not a scientific-model or UID permission failure.
An additional fresh fallback pod without the diagnostic/`cuInit` preamble also
passed in 34.270s, with UID10001-readable outputs and unchanged command digest;
its receipt is `bridge-qualified/no-diagnostic-fallback/receipt.json`.

This remains **experimental, not a production cache-level promotion**: only a
single-GPU H100 node and same GPU UUID are qualified. No different-node/device
remap, multi-GPU host allocation, accounted L4 RAM retention, other scientific
profile, or public controller snapshot deployment is claimed. The shared
reference-data publication and execution-map/Helm integration remain optional
future work. Normal loading remains production default because true disk-cold
restoration on this storage is slower.

### Retained reproducibility artifact

Parent explicitly retained the test checkpoint PVC, without a running worker:
namespace `fs2-models`, PVC `fs2-scientific-snapshot-probe-20260906`, PV
`pvc-026bc98a-b6b4-466a-a5eb-90e8ce925c44`, provider Network-SSD disk
`computedisk-e00apdc2ennnb1qg0c` (128Gi). The useful bundle is volume-relative
`startup-snapshot-20260906/r3`, containing `images/`, `cache/`, `worker.log` and
the captured public `fixture/`. Older `r1`/`r2` diagnostic captures also remain;
none is a production dependency. Mount paths must match the original
`/checkpoints/startup-snapshot-20260906/r3` inside a compatible test runtime.

Compatibility manifest SHA-256:
`843b93cb90761dc5fe8f593438a08796f8ca0dc83b04890f79d8dce14d636132`.
Captured source commit is `cd91d6cf` (the model image above); source hashes:

| File | SHA-256 |
| --- | --- |
| `esmfold2_server.py` | `c0c6648e0824030e950b3caa53f2b8123219afc99592d57cd393ab9c22c24993` |
| `process_checkpoint.py` | `836437db380259cd3384417cc6028d3e0dacdd0c08103ca57d0b4c28b9bd9c51` |
| `supervisor.py` | `c752523b6a42ff2cbb6579b23dc2c5ad4aba8e8d3dd4f0080b2002379a6af5ab` |
| `run_esmfold2.py` | `fea343d1e8d50bc453cd11610ae5d65dceb430c78c5374622b77030e86d787f8` |

Deleting the explicitly named test PVC later deletes this retained test disk
and its checkpoints. Do not delete the shared model-artifact or reference-data
volumes. Probe GPU pods and CPU mount holders are removed at handoff.

# CXR native-cache recovery and snapshot isolation

Status at 2026-09-07 13:44:48 UTC: the original native deployment is Ready with
one desired, ready and available replica. Its full original desired spec was
restored unchanged. The public snapshot r01 run passed both original inputs over
HTTP and MCP, with actual CUDA restore evidence, but its overall helper exited 1
while waiting for native readiness. This is partial test success followed by a
separately verified recovery, not a whole-helper pass.

## Failure and exact recovery

The native UID/GID 1000 process repeatedly failed in vLLM FlashInfer warmup,
after weights and AOT artifacts loaded successfully. One `autotune_configs.json`
under the model's image/weights/driver-specific `VLLM_CACHE_ROOT` was UID 0,
GID 1000, mode `0600`, size 231 bytes. The surrounding directories were owned by
1000:1000. The root-supervised **normal-loading** paired trial 3 logged writing
that exact file at 12:02:19.247 UTC, matching its retained modification time;
the contamination therefore predates the public snapshot restore. The earlier
paired loader timings remain measurements of that root-supervised test cohort.

At 13:43:18 UTC, an approved CPU-only helper changed **only that file's owner**
from 0 to 1000. GID 1000, mode `0600`, 231 bytes, inode and modification time were
preserved. Before/after SHA-256 was identical:
`5133f0a84fc4b1c85b6c76ef37b1d5354c3007810b92f9b947f6e62c3e6c43c4`.
The helper retained the original bytes and metadata, then was deleted. No
recursive ownership change, cache deletion, weight change, snapshot-bundle
change, policy update or production Pod replacement occurred. The existing
native Pod became Ready on its normal retry; the public admin API independently
confirmed Ready 1/1 at 13:44:48.758 UTC.

Private evidence is retained under
`releases/expanded-cxr-snapshot-r01/native-cache-permission-diagnosis-20260907/`:
`native-vllm-previous.log`, `native-pod-projection.json`,
`native-cache-stat-observed.txt`, `repair-r03.log`,
`repair-r03-completed-pod.json`, and `post-repair-public-status.json`.
Two unstarted helper attempts are also retained: a CPU node lacked the shared-FS
CSI driver; the tools image then lacked Python. Neither changed the cache. The
successful helper used that image's existing shell utilities on a compatible
CPU node. All three temporary helper Pods were removed.

## Renderer-only prevention

The frozen supervisor already relocates CUDA, Triton, TorchInductor and
FlashInfer workspace caches into snapshot scratch, but does not override
`VLLM_CACHE_ROOT`. That variable had continued pointing beneath the shared
`/model-cache` mount in both donor and restore Pods.

The bounded change in
[serving_snapshot.py](../../../components/control-plane/src/fs2_serve/serving_snapshot.py)
adds a nested mount at the **same captured absolute `VLLM_CACHE_ROOT`** backed by
the existing per-Pod `snapshot-checkpoints` emptyDir. The initializer reads the
original volume read-only and seeds only this vLLM subtree into private scratch.
Copying the entire subtree preserves AOT/mapped-file artifacts, links, bytes and
metadata; copying only the failing JSON would not preserve that filesystem
closure. The measured CXR subtree is 26,834,815 bytes, not the model weights or
GPU checkpoint pages. The original weights mount, environment path and model
arguments remain unchanged. Templates without this variable, and caches already
on Pod-local scratch, retain their previous rendering.

Validation: 32 focused serving-snapshot/metadata tests pass; Ruff and
`git diff --check` pass. Tests execute the metadata-preserving seed command,
check AOT links and private writes, missing-cache fallback, exact existing
default renders, and the previously fixed original HTTP readiness gate. The
retained actual CXR deployment plus qualified bundle also renders successfully.
No frozen source bytes or bundle identities changed. This renderer change is
**not yet deployed or live-restore-qualified** in this note; a subsequent release
must verify the isolated cache mount through the original public semantics and
native-policy restoration.

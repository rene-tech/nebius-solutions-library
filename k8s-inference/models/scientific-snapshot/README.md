# Request-ready scientific process snapshots

This is an isolated integration lane, **not a production-enabled runtime**.
It complements (and does not replace) the scientific batch controller, Kueue,
artifact verification, output collectors, or fast-start qualification policy.

The reusable lifecycle is:

1. A GPU donor pod starts a model worker as a child of the PID-1 supervisor.
2. Load immutable runtime artifacts, then test real requests. Request inputs
   remain dynamic; a captured fixed result is not a reusable model snapshot.
3. Quiesce the worker, optionally release unused allocator cache, suspend CUDA,
   dump the CPU process with CRIU, and flush checkpoint files durably.
4. Delete the donor pod. This releases its Kubernetes GPU allocation.
5. A fresh pod requests a GPU through Kubernetes and restores the process,
   restores CUDA, then accepts new requests. No scheduler allocation is bypassed.

`esmfold2_server.py` supplies the first real scientific worker. Its model paths,
ESMC precision and attention backend are explicit environment settings; the
default is the pinned H100 ESMFold2 runtime. Other models need their own
request-dependent qualification, not just this server's name changed.

`supervisor.py` accepts a runtime command and durable directory. It keeps PID 1
alive across capture and reaps descendants. `process_checkpoint.py` handles
single-process CUDA/CRIU ordering, timing receipts, failed-dump recovery, and
checkpoint-file durability. The runtime image, artifact mounts and checkpoint
paths must match between donor and restore. This lane does not use host PID,
host network, or writable host driver mounts.

## Reused toolchain

The existing FS2 generic Dynamo snapshot toolchain is reused at OCI digest
`sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4`.
It contains CRIU 4.2.1 (`91d552257809d0e5c7148190e9aa0372f13b76a0`) and NVIDIA
cuda-checkpoint (`00d5cce84c628088d6caa203fc4af40c1538b6f7`); its generic
restore-worker was based on Dynamo `f7f37be174d252590c4b56e25ff4262dd82466fd`.
The checkpoint primitives are reused here inside disposable containers rather
than deploying the historical host-agent templates into a serving cluster.

The tools image uses Ubuntu 24.04 while ESMFold2 uses an older runtime libc.
The init container copies the tools' loader/libc into a private tools volume;
CRIU runs using that loader. The model retains its original image libraries and
activation script. No host libraries are replaced. Use an immutable tools
image from the deployment's own regional registry.

## Running the isolated acceptance

See `../../acceptance/scientific-startup/`:

- `checkpoint-pvc.example.yaml` creates disposable 128Gi RWO storage. Nebius's
  [default Compute CSI class](https://docs.nebius.com/kubernetes/storage/disk-over-csi)
  provisions Network SSD/ext4. Do not write experimental dumps into a shared
  model cache with limited free space.
- Create a task-specific ConfigMap from the three runtime scripts.
- `render_persistent_probe.py --help` lists explicit image, node, storage and
  runtime settings for donor/restore pod manifests. The manifests are privileged
  **test pods** but do not mutate any host setting.
- Capture with `process_checkpoint.py capture --pid WORKER_PID --directory
  /checkpoints/RUN/images`; never overwrite an existing checkpoint directory.
- `benchmark_persistent_restore.py` deletes the donor and runs three fresh-pod
  restores, checking all model tensors and two full-production scientific
  requests on every restored worker. It retains receipts and CIF outputs.

The harness intentionally retains the last restore pod and checkpoint PVC for
inspection. The task owner must collect receipts, remove its exact probe pods
and ConfigMap, then delete its own PVC when evidence no longer needs it. The
example storage class has Delete reclamation: deleting that claim removes the
test disk and its checkpoint, not the shared model artifacts.

## Qualification boundaries

A same-process CUDA RAM checkpoint passed three cycles. Persisted CRIU restore
then passed three fresh-pod lifecycles on H100 driver 580.159.04, after deletion
of the donor/previous GPU pod. Every restored process reproduced the exact
2,396 model tensors (13,629,851,916 bytes) and served fresh 65/34-residue inputs
with full 20-loop/200-step settings. Pod creation to request readiness was
12.687, 11.453 and 11.453 seconds; the same-node OS filesystem cache was retained.
These are not cold-Network-SSD timings or a production L4 qualification.

Releasing only unused allocator blocks reduced captured reserved CUDA memory
from 26,772,242,432 to 14,317,256,704 bytes without changing model tensors.
That capture took 6.113 seconds for CUDA, 9.494 seconds for CRIU, and a further
320.184 seconds to fsync the approximately 19Gi checkpoint on this test's
128Gi Network SSD. Persisted storage throughput matters; do not enable a
snapshot strategy that is slower than ordinary model loading.

Raw evidence is described in `../../acceptance/scientific-startup/README.md`.
The optional one-shot bridge still requires its own frozen scientific-wrapper
qualification before deployment; experimental worker `/fold` results alone do
not qualify the batch controller integration.

RWO same-node testing does not qualify multi-node fan-out, another GPU type,
another driver, MSA inputs, or another model profile. OS page cache is not
reserved RAM: production L4 must use the existing accounted host-memory holder
and demonstrate that Kubernetes GPU allocation can still be released. Capture
time includes file fsync; otherwise delayed CSI unmount writeback is incorrectly
attributed to the next restore. Production shared snapshot storage and runtime
configuration remain Terraform/Helm-owned.

## Optional one-shot scientific bridge

`Dockerfile.esmfold2` layers the bridge over an immutable, already-qualified
ESMFold2 image. Normal image entrypoint behavior is unchanged. For an opt-in
snapshot invocation, preserve the existing scientific command after:

```text
python /opt/fs2/snapshot/supervisor.py --directory /checkpoints/RUN \
  --source-directory /snapshot-bundle --request-uid 10001 --request-gid 10001 \
  --fallback normal-load restore -- \
  python /WORKING_DIRECTORY/.fs2/stage-runner.py -- ORIGINAL_SCIENTIFIC_COMMAND
```

The worker runs the original `run_esmfold2.py fold` with a preloaded model;
frozen handoff, artifact-localization, model binding and per-request identity
checks remain intact, and the normal CIF/confidence outputs are written.
The root supervisor launches the existing stage-runner/client as UID/GID10001.
The serial worker temporarily uses that effective UID/GID while executing the
fold, preserving ownership of mode-0600 confidence outputs. The stage-runner
keeps its mode-0400 completion marker and the original command's digest; these
are readable by the unchanged UID10001 collector. This is an output-ownership
contract inside one trusted request pod, not a new tenant isolation boundary.
The supervisor exits with the request status and terminates the restored worker
so the Job releases its GPU. On missing/incompatible/failed restoration, it
records `normal-load-fallback` and executes the original normal-load command.
Kueue admission, scientific workspace preparation, collection and accounting
remain owned by the existing controller.

Compatibility checks bind runtime/tools image digests, model revision, driver,
kernel, GPU type and captured executable cache bytes. Additional kernels from
later request shapes are allowed; modified or missing captured files are not.
GPU UUID changes require explicit `--allow-device-remap` and a separately
qualified cross-GPU restore. Matching GPU model names alone do not prove
portability. This lane requires the isolated container privileges and tool
mounts demonstrated by the probe renderer; it does not change host drivers.

Use read-only shared images and per-attempt scratch, not one writable RWO
checkpoint directory for a concurrent batch fleet:

- Mount the captured bundle at `/snapshot-bundle` read-only.
- Mount per-attempt emptyDir at the original captured `/checkpoints/RUN` path.
- Overlay `/checkpoints/RUN/images` with the bundle's `images/` read-only.
- The supervisor copies only `cache/` and `worker.log` to scratch (684KiB/4KiB
  in this proof), never the ~19Gi checkpoint pages. Generated request kernels,
  runtime logs, and ordinary scientific outputs are not shared across attempts.
- CRIU uses its separate `/tmp/fs2-checkpoint-work` for restoration logs and
  statistics, as specified by the [CRIU directory contract](https://criu.org/Directories).
- Preserve original runtime model mounts, request workspace, image activation,
  and identity environment. Tools init copies the pinned snapshot binaries and
  their private libc/loader; the exact init spec is in the isolated renderer.

`qualify_scientific_bridge.py` exercises this layout using an actual captured
controller-issued frozen workspace and its unmodified stage-runner. It runs both
strict restored execution and intentionally unavailable-checkpoint fallback,
with a UID10001 reader validating command digest, confidence and CIF readability.
This acceptance path must pass before promoting its image/profile; it does not
publish snapshots into production reference data or add a customer UI toggle.

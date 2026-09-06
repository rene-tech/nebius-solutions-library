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

A same-process CUDA RAM checkpoint passed three cycles; persisted CRIU capture
also succeeded on H100 driver 580.159.04. Fresh-pod restore is being qualified
separately. Do not mark a model L2/L3/L4 eligible from capture alone.

RWO same-node testing does not qualify multi-node fan-out, another GPU type,
another driver, MSA inputs, or another model profile. OS page cache is not
reserved RAM: production L4 must use the existing accounted host-memory holder
and demonstrate that Kubernetes GPU allocation can still be released. Capture
time includes file fsync; otherwise delayed CSI unmount writeback is incorrectly
attributed to the next restore. Production shared snapshot storage and runtime
configuration remain Terraform/Helm-owned.

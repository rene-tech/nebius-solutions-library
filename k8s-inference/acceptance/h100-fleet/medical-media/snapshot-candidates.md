# Remaining medical/media snapshot candidates — 2026-09-07

These are explicit candidate decisions, not unmeasured startup results.
Read-only `nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory`
was run inside the current production Pods; it does not reserve or evict memory.
GPU allocation is not a checkpoint-size measurement.

| Model | Measured normal container → ready | Observed GPU process allocation | Current decision |
|---|---:|---:|---|
| NV-Segment-CT | 11.100 s earlier native median, n=3 | 1,936 MiB, one GPU | Subsequently qualified three normal/three restore pairs with both original masks; matched container-to-ready 12.782→8.498 s, Pod-request 17.758→14.525 s |
| SDXL | 11.702 s median, n=3 | 10,068 MiB, one GPU | No restore timing yet; lower priority because its native loader is already short and state is substantially larger than Segment |
| Evo2-40B v5 | 23.548 s median, n=3 RWO/page-cache; 44.373 s shared-FS n=1 | 47,672 + 46,590 MiB, two GPUs | Requires a separately versioned two-GPU snapshot identity/remapping contract before a valid experiment |

Segment and SDXL share the existing measured media runtime family but have
different immutable image digests. Each must retain its own exact image,
weights, precision, original request settings and output oracle. A small
snapshot can still cost more than an 11-second normal load once Pod startup,
filesystem preparation, CRIU and CUDA restore are included. SDXL has no
measured snapshot benefit yet; it is neither declared unsupported nor enabled.
For context only, the standalone OpenFold3 first restored 11.34-GB bundle took
13.944 seconds container-to-observed-ready. That is not a prediction for SDXL.

Evo2's limitation is concrete in the **current adapter**, not a claim about
CUDA's general capabilities: `models/scientific-snapshot/process_checkpoint.py`
requires exactly one assigned GPU in `runtime_identity()` and its restore
path records/maps a single old UUID to a single new UUID. The qualified Evo2
process places unchanged model layers across two H100s. Reusing that one-GPU
manifest, hiding a GPU, or reducing the model is not a valid Evo2 checkpoint.
A separate source/manifest version must preserve both ordered device identities,
memory placement and the exact original two DNA outputs across donor-deleted
and cross-node restores. Existing frozen one-GPU bundles must remain unchanged.

The observed two-GPU allocation is about 92 GiB, before measuring CPU checkpoint
pages. Its potential restore I/O cost versus the 23–44-second cache-conditioned
normal loader makes benefit uncertain. No Evo2 capture/restore benchmark or
speedup is claimed, and no timeout/model-memory setting was changed to make
the experiment look favorable. Original Evo2 source weights and receipts remain
retained while the normal production service stays available.

# GROMACS public REST concurrency qualification — 2026-10-03

## Result

**32/32 requests passed; 13 simultaneous GPU processes demonstrated; 16-GPU
simultaneous execution is not qualified.** All requests used the existing
internal `system/qa` key. No customer key was used for inference.

| Measurement | First burst (includes node scale-out) | Second burst (warm capacity) |
| --- | ---: | ---: |
| Independent requests submitted together | 16 | 16 |
| Successful, independently validated results | 16 | 16 |
| Idempotent replays reused their original operation | 16 | 16 |
| Admission-to-terminal minimum / median / maximum | 199.9 / 357.4 / 474.5 s | 199.6 / 209.9 / 454.2 s |

Across both bursts: 32 distinct input hashes and trajectory hashes; 1,824
published artifacts (650,421,571 bytes) verified by SHA-256 and length; 208
authenticated catalog-availability probes, zero errors, 0.348 s median and
2.391 s maximum latency. This is observed availability during this test, not
an availability guarantee for every endpoint or future workload.

For this 6,598-atom input only, median production throughput was 779.1 ns/day
on one-GPU L40S nodes (n=16), 727.2 ns/day on one-GPU H100 nodes (n=12), and
614.0 ns/day on a shared eight-H100 node (n=4). Shared-node contention, CPU
shape and this small system matter; these are not general GPU rankings.

Closeout verified: zero active internal QA operations; zero remaining GROMACS
Jobs/Pods; API unchanged and 3/3 Ready; internal key concurrency restored to 2,
its non-expiring lifetime/grants/scopes preserved. No customer policy changed.
The node autoscaler remains responsible for eventual idle-node scale-in; this
test does not claim that its six added nodes have already been removed.

## Identity and scope

This is an **internal system/qa test**, never a test under a customer key.
The owner explicitly rejected borrowing Lynx's key or changing its concurrency
policy. Lynx's key, settings, workspace and LibreChat instance were not changed.
Only the existing system/qa inference key's concurrency was temporarily changed
from 2 to 16, with a recorded restore after task-owned work drains. No new
identity, bucket, cloud quota, node-group limit or serving release was created.

The test exercises separate REST requests, not one MPI request spread over
sixteen GPUs. HTTP 202, logical `running`, running GPU containers, and GROMACS
GPU processes are recorded separately. No broad model/platform readiness claim
can be inferred from this bounded workload.

## Exact target

- Project `project-e00rene`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, eu-north1.
- Public origin `https://89.169.99.188`, ordinary inference API key and REST
  artifact upload, submission, polling, result and download endpoints.
- Tenant/principal `system/qa`; key ID
  `56130b22-ae09-42fc-a0f0-48012f22fb71` (identifier, not credential).
- API image `sha256:6a2876b380f43717ea37cb21dd4504af5884c9a305c486562a42ef103420f946`,
  source `e275c94918d9400e2f6809f4cf06cfe21e4e8e27`.
- GROMACS image
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643`.
- Native NVIDIA base
  `nvcr.io/nvidia/gromacs@sha256:0e52e3ae971453898956379952b9ea606f5400cbdb4d439773ecbae4d8f5ad59`,
  GROMACS 2026.2-dev, mixed precision, CUDA, one thread-MPI rank/eight OpenMP
  threads per independent one-GPU request.
- Artifact init/collector image
  `sha256:719ec336ef93e582f3031735dba974b61ea97f1c4ce47e630fe18eae9e9cd77c`.
- Scheduling ConfigMap `fs2-r927c465c6d-scientific-scheduling-a99526bda8db`;
  execution ConfigMap `fs2-r927c465c6d-scientific-execution-64277b315007`.

## Workload and validation

Two consecutive batches of sixteen independent requests use the immutable
system QA alanine starter topology: capped ACE–ALA–NME, ff14SB/TIP3P,
6,598 atoms. The original input archive SHA-256 is
`8d2d7f61ddb7d329387fc64b2b36b27511f8575cfcaffdf2bc0763ed579f008e`.
Coordinates, topology and topology audit remain byte-identical; each request
gets different recorded seeds. Each workflow minimizes, runs 20 ps NVT and
20 ps NPT, then 1 ns production at a 2 fs timestep. Production coordinates
are saved every 10 ps (101 frames); energies every 1 ps (1,001 rows).

This is an execution/scaling test, **not a converged scientific study** or a
capacity qualification for arbitrarily large systems. The early production
frames still include relaxation after the deliberately short equilibration.

The runner pre-stages inputs, submits sixteen requests concurrently, replays
each identical idempotency key, follows every operation, and downloads all
published artifacts through the same non-admin REST identity. It verifies
size/SHA-256 before materializing native files. The independent summarizer checks:

- all thirteen native commands succeeded;
- result identity, requested seeds and full 500,000-step production length;
- trajectory atom/frame counts and final time via the retained `gmx check` log;
- native PP and PME GPU offload, and successful `mdrun` completion;
- finite, correctly labeled, monotonic energy series at expected output times;
- basic temperature sanity, without asserting ensemble convergence;
- every attempt's durable resource-release flag.

The native NVT energy record contains no Density column. The starter request
asks for it anyway, producing `String 'Density' does not match anything` before
successfully extracting the four available quantities. This is retained as a
nonfatal fixture diagnostic; NVT density is not manufactured. Production/NPT
density is present and validated. Native performance notes about short neighbor
lists and CPU updates for the stochastic integrator are retained, not hidden.

## Capacity findings

Initial free eligible capacity was seven GPUs: four one-GPU H100 nodes, one
GPU on a shared eight-H100 node, and two one-GPU L40S nodes. Four additional
free L40S GPUs on the `l40s-4x` pool were **not** in GROMACS's allowed pools.

The first burst triggered the existing node autoscaler to grow `l40s-1x` from
8 to 14 nodes, within the unchanged maximum of 16. The six new nodes became
Ready and executed real requests. A recorded new-node example took about
163 seconds from request admission to the GROMACS container starting; the
native GROMACS image pull itself took 6.66 seconds (489,672,996 image bytes).

The warm second batch reached **13 simultaneous GROMACS GPU processes**:
four H100 single-node slots, one shared-node H100 slot and eight L40S slots.
Three further requests waited on H100 pools, then ran as their predecessors
released those GPUs. Kueue quota admission is not a promise of immediately
available physical capacity. Existing non-Kueue GPU occupancy and fixed H100
pool sizes must be accounted for when interpreting admission headroom.

**Sixteen accepted concurrent requests is not sixteen executing GPUs.**
The sixteen-GPU simultaneous target remains unqualified. The smallest apparent
next step is to qualify and enable GROMACS on the already-free `l40s-4x` pool;
it was not silently enabled by this test. Otherwise additional eligible capacity
is required. No customer limits need to change for internal qualification.

GPU-process snapshotting is not used by this native engine workflow. Its
recorded checkpoints are GROMACS native `.cpt`/workflow checkpoints, not CUDA
process snapshots. This test does not qualify checkpoint restore or preemption
recovery, seventeen-request overload behavior, customer-specific storage/access,
LibreChat, MPI, or long/heavy molecular systems.

## Evidence and reproduction

The final secret-free per-request metrics are in [results.json](results.json),
generated by `summarize.py` after all receipts and the policy-restore record exist.
Raw inputs, native files, API receipts, lifecycle observations, checksums and
GPU-process samples remain under:

`/home/tux/secure-handoff/fs2-gromacs-internal-api-concurrency-20261003/`

All workspace writes are under the internal tenant prefix
`runs/fs2-gromacs-internal-api-concurrency-r20261003/`. No customer bucket was
used. The existing autoscaler owns later idle-node scale-in; do not manually
delete its new nodes or change its minimum during closeout.

From the task worktree, the invocation was:

```bash
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/gromacs-concurrency-20261003/verify_concurrency.py \
  --client-root /home/tux/worktrees/scientific-ai-workbench-lifecycle-20261002/templates/hcls-librechat \
  --qa-env /home/tux/secure-handoff/fs2-agent-reliability-20261001/reliability-r7/runtime.env \
  --fixture /home/tux/secure-handoff/fs2-agent-reliability-20261001/reliability-r7/workspace/examples/v3/molecular-dynamics/alanine-quickstart/gromacs \
  --output /home/tux/secure-handoff/fs2-gromacs-internal-api-concurrency-20261003
```

This dated runner deliberately accepts only the existing internal QA identity.
Never substitute a customer's environment/key to bypass its identity check.
Use the same receipt directory to resume, not new idempotency keys after an
ambiguous response. A new independent campaign needs an explicitly new task/run
namespace; this invocation is not an invitation to rerun work blindly.

```bash
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/gromacs-concurrency-20261003/test_runner.py
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/acceptance/gromacs-concurrency-20261003/summarize.py \
  /home/tux/secure-handoff/fs2-gromacs-internal-api-concurrency-20261003 \
  --output k8s-inference/acceptance/gromacs-concurrency-20261003/results.json
```

Task Deck:
`dashboard/data/epics/nim-fast-start-platform/tasks/fs2-gromacs-internal-api-concurrency-r20261003/task.md`.

# Native MD persistent GPU snapshot: bounded initial screen

This is isolated qualification, not a public snapshot feature or a release
approval. GROMACS has one successful persistent fresh-Pod continuation with
native output validation. End-to-end benefit is **not established**. NAMD has
a durable capture; fresh-worker restore awaits the original GPU becoming free.
LAMMPS has not yet run. AMBER is gated on its private native qualification.

## Scope and immutable identity

Source starts at `63e8bc72849d34f1c40f55c794ceb38028c6585b`, in a clean detached
worktree. This harness reuses the unchanged platform
`models/scientific-snapshot/process_checkpoint.py` and supervisor GPU binding.
It adds no controller, catalog flag, application setting or production rollout.

Cluster `mk8scluster-e00j5z9te7x5dd9g6a`, project `project-e00rene`, eu-north1,
context `fs2-remediation-sandbox2`, namespace `fs2-models`. One existing on-demand
H100 at a time; no node added or customer workload interrupted. Node
`computeinstance-e00bwrmx5x05qn4bc8`, GPU
`GPU-ade41b30-29a5-be1f-71a2-95104de12b84`, H100 80 GB HBM3, SM90,
driver `580.173.02`, kernel `6.11.0-1016-nvidia`.

All image paths start with `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/`:

| Component | Immutable image suffix |
|---|---|
| GROMACS, released NGC binary + PLUMED | `fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643` |
| NAMD 3.0.2 CUDA 12.9 GPU resident | `fs2-platform/namd-worker@sha256:1af3abab5c794c38ef19d557bd5ca0e754fd078343f36b7e41a9eeee2ac7354f` |
| LAMMPS qualified native worker, queued only | `fs2-platform/lammps-worker@sha256:e4e21f952285134be263c9ea3f1f06ce461fdca2409622b568b186b12f7f199c` |
| CRIU / CUDA checkpoint tools | `fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4` |

CRIU 4.2.1 commit `91d552257809d0e5c7148190e9aa0372f13b76a0`
(binary SHA256 `59dceb7629ea2dfc00be9ff170ba35ec490ca129093bbea399b176f025cdde2e`).
CUDA checkpoint commit `00d5cce84c628088d6caa203fc4af40c1538b6f7`
(binary SHA256 `707fa7f54136824d6c1d6dd724b9b1717610f831033c00d06da474de363a06db`).
GROMACS binary SHA256
`68e3fac29b6b528acc7f4458e9f49b7b42f979af26d450c8ae9207d90325b774`.

Parent authorized isolated privileged root containers for this screen only.
No host PID/IPC/network, host mounts, service-account tokens, driver changes,
global policy changes or unrelated process inspection. The child native engine
runs as UID/GID10001. GPU UUID binding follows the scheduler allocation.
Task32GiB RWO PVC `fs2-md-snapshot-r20260923`, storage class
`compute-csi-default-sc`; no shared model cache. Initial hard stop16:27UTC.

## GROMACS: persistent continuation passes once

Synthetic lysozyme,23961atoms,200000steps/400ps, exact TPR SHA256
`e21e2a07ed800f66b6c581beb1b7dabbd695df786f4faf3692ac090875be588f`.
Command is recorded in `gromacs-plan.json`: one thread-MPI rank,8OpenMP threads,
pinning off, native checkpoint cadence0.02minutes. This deliberately short
checkpoint cadence is a qualification choice, not a recommended default.

Donor UID `86703d76-1521-4b2f-b128-9292fc2617e5` reached logged step10000.
CUDA checkpoint and CRIU dump killed its native PID220. Saved files were fsynced,
inventoried, archived and hashed; donor Pod was deleted before the restore Pod
was created. Fresh Pod UID `a37f6b7b-c294-4a9f-8705-58bb6011caf8` restored that
saved process on the same GPU. Every inventoried saved-file size/hash was checked
before restore. The restored PID is necessarily220, but its start ticks changed;
kernel PID-namespace inode numbers happened to be recycled and are not used as
independent-worker proof.

Continuation advanced beyond captured10000 to requested200000. Native `gmx check`
read all201XTC frames through400ps;201energy samples are finite. Final checkpoint
step200000 and nonempty final coordinates pass. This is a continuation, never an
independent replica; no stochastic-ensemble independence is asserted.

| Measured phase | Seconds |
|---|---:|
| Donor probe start to native logged10000 | 4.845 |
| CUDA checkpoint | 0.446 |
| CRIU dump | 0.390 |
| Saved-state durability flush | 41.983 |
| Complete capture probe | 48.238 |
| Fresh restore Pod creation to runtime start | 52 |
| Restore helper, including CUDA restore/unlock | 0.754 |
| Restore helper start to later native step11000 | 0.806 |
| Complete restore probe: hash verification + restore + remaining simulation | 83.704 |
| Image-cached native checkpoint restart to step11000 | 3.565 |
| Image-cached native checkpoint restart to completion | 29.339 |

Raw saved image bundle667545742bytes. Fast helper timing excludes the preceding
full saved-image hash read, Pod scheduling, volume attachment and image startup.
The first harness did not separately time that hash read; do not derive an exact
breakdown by subtracting unrelated runs. The subsequent harness explicitly
records pre-helper and probe-start-to-useful-step time.

Native comparator starts from the retained **step9500 native checkpoint**, while
the CUDA capture had logged10000: not exactly state matched, not3repetitions and
not proof of a net speedup. Both finish the same200000step TPR and pass the same
native validators. Chaotic trajectory divergence is expected; bitwise equality
was not required or claimed. Snapshot downtime also affects native wall-clock
timers; engine-reported `ns/day` including this pause is not steady throughput.
No cold-node or disk-cache eviction experiment was performed.

## NAMD: capture passes; restore awaiting capacity

Public ApoA1 fixture,92224atoms,NVE100000steps/200ps. The exact qualified
`fs2-production-part000001.namd` begins from equilibrated step21000 and requests
121000. Only9allowlisted input files were copied; no previous production outputs.
`namd-input-hashes.json` records every input hash. Four CPU workers, one GPU.

Donor UID `b956034f-c134-4b68-b993-4ce01741b765`, native PID180, logged31000.
CUDA checkpoint0.576s, CRIU dump0.714s, fsync87.885s; raw saved bytes1374302284,
complete capture98.158s. Donor process terminated; hashed full archive retained
before Pod deletion. These facts prove capture only, not persistent restore.

At15:57UTC a finite hosted NPT qualification workflow acquired the original H100.
The task restore Pod stayed Pending with insufficient GPU/CPU; it was removed
without interfering with the running workflow. No cross-GPU UUID remapping was
enabled. Its Pending object/events are retained separately from engine failures.

## Remaining gates and reproducibility

LAMMPS uses the qualified synthetic LJ131072atom input archive
`4f94b8e91c775993926300f3b6f92788a3aca6de38ee580e6d6a628c18ab0508`, request
`a4788ef394e405d1aa5aa73a17691469ea0ba069f3b71f6acec305fcf2d8d861`.
The harness follows all native production segment logs rather than assuming a
single process survives a native timer boundary. Its existing speed-oriented
neighbor policy is not an accuracy default. No LAMMPS GPU snapshot result yet.
AMBER is not screened until private native qualification succeeds.

Evidence root: `/home/tux/secure-handoff/fs2-md-snapshot-20260923`. Exact rendered
Pod/PVC manifests, immutable source ConfigMaps, plans, observed Pod objects,
command logs, native validators, capture/restore receipts, CRIU logs and hashed
archives are retained. Tools are run only inside the task-owned Pod:

```sh
python3 /snapshot-source/snapshot_probe.py capture --directory /checkpoints/CASE --plan /checkpoints/CASE/plan.json
# Archive/verify capture, delete donor, wait for its deletion, recheck capacity,
# then create a fresh one-GPU Pod with identical image/tools/source/mount paths.
python3 /snapshot-source/snapshot_probe.py restore --directory /checkpoints/CASE --plan /checkpoints/CASE/plan.json
```

GROMACS and NAMD use immutable source ConfigMap `fs2-md-snapshot-source-cf7e2c6`;
segmented LAMMPS uses `fs2-md-snapshot-source-22c8be8`. Keep each captured source
unchanged for its restore. Tests:13pass, including unchanged platform checkpoint
identity/cache/tree tests and new one-GPU/no-host/path/Pod-identity guards.

Before adoption: exact-state-matched repeated native restart comparison, full
recovery latency and durable storage cost, supported scientific protocols and
stochastic state checks, cancellation/error fallback, actual customer API/bucket
acceptance, image/runtime changes requalified, and parent-controlled release.
Successful same-GPU isolated continuation is not proof of cross-GPU portability,
safe public privilege, independent replica generation or a beneficial product.

The distinction between CUDA suspend/resume and persistent CPU+GPU restore
follows the [pinned NVIDIA tool documentation](https://github.com/NVIDIA/cuda-checkpoint/blob/00d5cce84c628088d6caa203fc4af40c1538b6f7/README.md)
and [CRIU GPU checkpoint guidance](https://www.criu.org/GPU_Checkpointing).

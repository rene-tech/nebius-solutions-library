# Isolated single-L40S incremental performance investigation

Owner: `fs2-l40s-incremental-performance-r20261006`, NIM Fast Start Platform.
User approved the bounded investigation on 6 October 2026, explicitly without
interrupting the current customer workflow. This is native performance research,
not a production rollout or a new customer-ready claim.

## Contract

- Original 185,486-particle CHARMM membrane input, 2 fs timestep; source SHA-256
  `e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
- Production worker digest `ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`.
- One L40S, eight requested/limited CPUs, 16 GiB RAM per task-owned pod.
- No customer API credentials, bucket writes, node changes, clocks/power policy,
  image rollout, or changes to the active customer pod.
- Explicitly exclude the customer node `computeinstance-e00xwjv9khjp8fhp3v`.
- Use already idle single-L40S nodes; their CPU platform may differ from the
  AMD-backed customer node. Do not silently transfer host-specific results.
- Only finite run length changes in the scientific input. Keep force field,
  masses, timestep, cutoffs, thermostat/barostat and output cadence unchanged.

## Experiments

1. CPU: 6/7/8 threads, task-process-only physical-core affinity, and AVX-512 if
   an alternative binary is present in the exact worker. Three alternating-order
   50,000-step screens, then three alternating-order 500,000-step confirmations
   of the baseline and best screened candidate on the same node.
2. Checkpoint: three paired 500,000-step runs of continuous versus five-minute
   closed segments. Both write native checkpoints every five minutes. Compare
   completed simulation per native process wall; no claim that this already
   implements consistent live object-storage exports.
3. Profile: bounded CUDA timeline in a separate idle pod, without CPU sampling,
   hardware counters, privileged access, or changes to monitoring. Missing tools
   or permissions remain explicit rather than modifying node policy.

Every completed run validates native checkpoints, finite energies, trajectory
files requested by the original input, finite coordinates and particle count.
The supervisor records immutable runtime/input/source identities and the
customer pod identity/readiness/restart counters before and after, and deletes
only its UID-checked task pod. Raw scientific artifacts remain private.

## Reproduction

```bash
python3 -m pytest -q k8s-inference/acceptance/l40s-incremental-20261006/test_experiment.py
python3 k8s-inference/acceptance/l40s-incremental-20261006/run_experiment.py \
  --mode cpu \
  --node <idle-single-L40S-node> \
  --name fs2-lynx-perf-cpu-incremental-cpu-r1 \
  --tpr <private-original.tpr> \
  --output <new-private-evidence-directory>
```

Run `checkpoint` and `profile` modes on different idle nodes in parallel. Do not
run two benchmark cohorts concurrently on one GPU/node. Profiling measurements
are never pooled with uninstrumented throughput measurements.

## Status

Implementation and seven focused unit tests pass. Throughput cohorts are running.
Private evidence root: `/home/tux/secure-handoff/fs2-l40s-incremental-20261006/`.
No production defaults have changed.

## Settled GPU profile (completed)

`profile-r4` captured a five-second CUDA interval starting 45 seconds into a
100,000-step run, after the normal PME tuning period. Nsight Systems 2025.6.3
was copied into task-pod scratch storage, including its matching importer and
report tools. No node/driver policy, CPU sampling, hardware counters or DCGM
changes were required. The finite simulation subsequently completed and passed
native energy, trajectory and checkpoint validation. All 54 retained files
(92,831,247 bytes) were independently rehashed after download.

- GPU-event span: 4.982577 s; union of kernel/copy/memset intervals: 4.299756 s
  (86.296% of that span). The union does not double-count concurrent streams.
- H2D: 14,944,304,396 bytes in 0.651103 s of recorded copy activity.
- D2H: 14,518,099,588 bytes in 0.572483 s of recorded copy activity.
- Largest kernel: short-range Ewald/Lennard-Jones force-switch calculation,
  6,093 calls and 2.826915 s summed kernel duration. GPU PME and bonded kernels
  were present; this was not a CPU fallback.
- CUDA event synchronization consumed 2.921442 s of host API duration. This is
  waiting, not independent GPU execution time or proven removable overhead.
- Runtime kernel launches: 0.282637 s; driver kernel launches: 0.137554 s.
  Do not add overlapping CPU/GPU/API categories as an execution-time budget.

These are instrumented diagnostic observations, not an uninstrumented capacity
benchmark. They do not prove an attainable speedup or causally assign every
copy to CMAP. Together with retained native CPU-force/CMAP evidence, they support
investigating unnecessary CPU/GPU round trips rather than expecting a simple
thread-count change to double throughput. Raw trace and SQLite export remain
private under `profile-r4/results/profile/`.

### Retained diagnostic attempts

- `profile-r1`: profiler absent from the production worker; environment inventory
  only, not a captured profile.
- `profile-r2`: relocated CLI rejected a changed installation-directory layout.
- `profile-r3`: trace captured startup PME tuning, not settled execution. The
  bounded profiler returned while its target was still writing native output;
  independent inventory verification correctly rejected the mutable energy
  artifact. It is not the accepted profile/artifact cohort. `profile-r4` waits
  for target completion before inventory, export and cleanup.

All four profile pods were removed by their own UID-fenced supervisor. Their
before/after customer observations retained the original customer pod UID,
Ready status, image identities and zero restarts.

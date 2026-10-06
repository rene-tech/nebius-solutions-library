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
4. PME: three paired 500,000-step runs with normal autotuning versus
   `-notunepme`, retaining the original input's grid/cutoff/tolerance. This tests
   the cost of repeated tuning, not a new electrostatics accuracy setting.
5. OpenMP: three alternating-order 100,000-step runs for each of default,
   `OMP_WAIT_POLICY=ACTIVE` and `PASSIVE`, with the same eight-CPU allocation.

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

The other modes are `checkpoint`, `profile`, `pme`, and `wait-policy`.
Run independent cohorts on different idle nodes in parallel. Do not
run two benchmark cohorts concurrently on one GPU/node. Profiling measurements
are never pooled with uninstrumented throughput measurements.

For profiling, pass `--profile-tools` with the complete local Nsight Systems
installation directory. Its `target-linux-x64` and `host-linux-x64` layout must
be retained. The accepted run used `/opt/nvidia/nsight-systems/2025.6.3`.

After a supervisor exits, independently verify its downloaded artifacts:

```bash
python3 k8s-inference/acceptance/l40s-incremental-20261006/analyze_evidence.py \
  <private-evidence-directory>/results \
  --output <private-evidence-directory>/analysis.json
```

Comparisons are paired by repetition on one host. The analyzer rejects
missing/duplicate pairs, different simulation lengths and invalid runs; it
does not select the fastest observation or treat native measurements as API
delivery rates. Three repetitions describe observed variability, not a formal
statistical-significance or cross-workload claim.

## Status

Implementation and nine focused unit tests pass. CPU, checkpoint and PME
confirmations are running. The profile and OpenMP cohorts are complete.
Private evidence root: `/home/tux/secure-handoff/fs2-l40s-incremental-20261006/`.
No production defaults have changed.

## Hardware boundary

The spare `l40s-1x` nodes use Intel Xeon Gold 6338, eight physical cores / sixteen
exposed logical CPUs, and a 325 W L40S limit. Each benchmark requests and limits
eight CPUs. A read-only observation during confirmation found software power
capping active, no thermal slowdown, about 303.5 W average board power and
2,385 MHz SM clock. No power limit or clock was changed.

The customer job runs on an AMD-backed four-L40S node with one GPU allocated.
A read-only NVML query showed its GPU already at the 350 W factory default;
increasing the test nodes' power limit is therefore not an available new
optimization for that customer job. Host-specific CPU/SIMD findings require
an isolated matched AMD/350 W confirmation before any future promotion. Do not
compare absolute rates across these different hosts as an A/B experiment.

## Completed OpenMP screen

Three 100,000-step (0.2 ns) runs per case, with alternating order. These short
runs include startup/tuning and show variability; they are not steady-state
throughput estimates for a fourteen-day job.

| Policy | Median process-inclusive ns/day | Range | Median paired change vs default | Paired change range |
| --- | ---: | ---: | ---: | ---: |
| Default | 194.646 | 183.573–195.096 | — | — |
| ACTIVE | 183.963 | 183.958–195.104 | +0.209% | −5.706% to +0.235% |
| PASSIVE | 177.541 | 177.180–177.817 | −8.787% | −9.183% to −3.136% |

The paired statistic differs from the ratio of independent medians because
the baseline and ACTIVE both show short-run variability. ACTIVE does not show
a repeatable speedup. PASSIVE uses about 2.36 CPU cores instead of roughly
6.9–7.25 for default (including native checkpoint inspection), but slows this
latency-oriented workload. Keep the existing policy. All ten runs including
warmup passed; 173 files / 243,930,013 bytes were independently rehashed.
The exact task pod was deleted and absence observed. Customer UID, images,
readiness and zero restart counters were unchanged before/after.

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

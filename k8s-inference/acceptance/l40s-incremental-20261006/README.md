# Isolated single-L40S incremental performance investigation

Owner: `fs2-l40s-incremental-performance-r20261006`, NIM Fast Start Platform.
User approved the bounded investigation on 6 October 2026, explicitly without
interrupting the current customer workflow. This is native performance research,
not a production rollout or a new customer-ready claim.

## Outcome — 6 October 2026

The bounded investigation is complete. No production image, defaults, customer
credentials, customer storage or running customer process was changed.

| Change | Median paired process-inclusive speed change | Decision |
| --- | ---: | --- |
| Keep original PME grid; skip repeated autotuning | +2.197% | Small input-specific candidate; not promoted |
| Continuous process instead of five-minute native restarts | +0.851% | Small native-only saving; preserve existing durable recovery |
| Installed AVX-512 instead of AVX2 | −1.581% | Keep AVX2 |
| OpenMP ACTIVE instead of default | +0.209%, with one negative repeat | No repeatable gain; keep default |
| OpenMP PASSIVE instead of default | −8.787% | Lower CPU use but slower; reject for this latency goal |

Six/seven threads and explicit affinity also failed to beat the eight-thread
baseline in screening. There is no measured large speedup from the tested
unchanged-protocol settings. Further CPU/GPU round-trip or CMAP work is an
unproven engine-development hypothesis, not a delivered optimization.

The paired comparisons apply only within each test host. Spare nodes use
Intel/325 W L40S; the current customer uses AMD/350 W L40S. No finding is silently
promoted to the different customer execution shape. PME and restart savings
overlap and must not be added together.

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

Every throughput run validates native checkpoints, finite energies, trajectory
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

All five accepted cohorts are complete and independently verified. Nine focused
unit tests pass; including the unchanged resource/topology helpers, 21 pass.
Private evidence root: `/home/tux/secure-handoff/fs2-l40s-incremental-20261006/`.
No production defaults have changed. Fifty receipt-based finite runs passed,
plus the accepted instrumented profile target: 51 executions including five
warmups. Rejected profiler setup/capture attempts are recorded separately and
are not counted as accepted evidence.

| Cohort | Evidence directory | Verified files | Verified bytes |
| --- | --- | ---: | ---: |
| CPU screen and confirmation | `cpu-r1` | 408 | 615,798,517 |
| Native restart comparison | `checkpoint-r1` | 143 | 223,828,749 |
| Original-grid PME comparison | `pme-r1` | 134 | 210,430,365 |
| OpenMP wait-policy screen | `wait-r1` | 173 | 243,930,013 |
| Accepted settled profile | `profile-r4` | 54 | 92,831,247 |
| Total | | 912 | 1,386,818,891 |

Context: `nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`, namespace
`fs2-models`, project `project-e00rene`, region `eu-north1`. Existing idle regular
single-L40S nodes were used; no capacity was created. CPU/checkpoint/PME nodes
were respectively `computeinstance-e00r165maajnbg7wrr`,
`computeinstance-e00ax3mgt7y3a0asa6`, `computeinstance-e00sa78kng1kwhej6q`.
Profile and later OpenMP used `computeinstance-e00pz1f67nxthz924e` sequentially.

Every supervisor observed unchanged customer identity, images, readiness and
zero restarts, and removed only its own UID-checked pod. At 09:39 UTC, the task
label selected no remaining pods; customer pod
`fs2-workflow-mas1-20e-a1-ec69e0520fc9-fjr6j` retained UID
`51b9d39a-8e8b-45da-b198-1fb730ff89e5`, Running, both containers Ready, zero
restarts. Temporary scratch pods are gone; downloaded raw evidence is retained
privately. No customer API key was used.

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

## CPU screen

Three 50,000-step (0.1 ns) repetitions per case on the same node, alternating
forward/reverse order after a separate warmup. All eighteen screens passed.

| Case | Native median ns/day | Process-inclusive median ns/day | Process-inclusive range |
| --- | ---: | ---: | ---: |
| Eight threads, AVX2 baseline | 203.486 | 177.766 | 177.577–178.111 |
| Seven threads | 167.870 | 145.913 | 145.789–170.918 |
| Six threads | 168.697 | 150.751 | 149.855–175.803 |
| Eight threads, explicit physical-core affinity | 179.573 | 159.351 | 159.068–177.958 |
| Seven threads, explicit physical-core affinity | 202.198 | 171.085 | 145.282–171.764 |
| Eight threads, installed AVX-512 binary | 202.113 | 176.659 | 158.785–177.762 |

None beats the baseline median. The highest non-baseline candidate, AVX-512,
was compared against AVX2 in three longer paired 500,000-step runs. Short-run
variability is visible; do not interpret an isolated best sample as a gain.
Observed explicit binding put the OpenMP threads on separate physical cores;
CUDA helper threads inherited CPU 0. Relocating individual helper threads was
not implemented or tested, so this screen does not rule out that separate idea.

Native `gmx check -s1 ... -s2 ...` output is retained alongside the MDP diff:
the finite TPRs differ only in `nsteps`. The initial state is the retained original
input, not a copy of the customer's changing in-flight checkpoint. Valid finite
energies/coordinates/checkpoints establish execution and artifact integrity,
not long-timescale ensemble convergence or bitwise trajectory equivalence.

### Completed long CPU confirmation

Three paired 500,000-step runs, alternating order, same host and resource budget.

| Build | Native median ns/day | Process-inclusive median ns/day | Process-inclusive range |
| --- | ---: | ---: | ---: |
| Eight threads, AVX2 | 217.551 | 214.264 | 214.121–214.315 |
| Eight threads, AVX-512 | 214.115 | 210.927 | 210.553–213.863 |

Paired AVX-512 changes: **−1.666%, −1.581%, −0.188%**; median **−1.581%**.
All six long runs passed. The installed alternate binary genuinely reports
AVX-512, not just an AVX-512 directory name. The baseline is mixed-precision
GROMACS 2026.2-dev, thread-MPI, GCC 12.3.0, CUDA 13.0.88 with driver 580.173.02;
full compiler and runtime inventories are retained. Keep the existing AVX2
build; do not extrapolate SIMD conclusions to an untested AMD processor.

## Completed checkpoint comparison

Three paired 500,000-step (1 ns) runs per arm, same host. Both arms keep native
checkpoint writes every five minutes. The segmented arm closes with `-maxh`
and resumes from the exact previous checkpoint with `-append`; each pair
completes exactly 500,000 useful steps. Intermediate checkpoint files, hashes
and start/end step ranges are retained. There is no double-counting of restart
work in the reported simulation rate.

| Mode | Native median ns/day | Process-inclusive median ns/day | Process-inclusive range |
| --- | ---: | ---: | ---: |
| Five-minute closed segments | Not averaged across unequal segments | 212.098 | 209.502–212.355 |
| Continuous process | 217.414 | 214.162 | 213.896–216.988 |

Paired continuous-versus-segmented speed changes: **+3.573%, +0.851%, +0.848%**;
median **+0.851%**. Process-inclusive wall time sums native process execution,
including startup and native output writes. It excludes harness inspection
between segments, API admission/staging and object-storage export. This is
not a measurement of the entire production pipeline.

All seven runs including warmup passed; 143 files / 223,828,749 bytes were
independently rehashed. The supervisor removed its exact pod, observed absence
and recorded unchanged customer UID/images/readiness/zero restarts.

Recommendation: do not change the active customer's recovery path for this
small native-only gain. A future continuous worker still needs consistent
durable object-storage exports and a real interruption/recovery acceptance
test. This experiment uses native append, not the production immutable part
packaging, and does not implement or qualify that feature.

## Completed original-grid PME comparison

Three paired 500,000-step (1 ns) runs, same host and resources. The only command
change is `-notunepme`; original TPR grid, cutoff and accuracy settings remain.
Normal autotuning tried alternatives and selected the original 96 x 96 x 144
grid and 1.2 nm Coulomb cutoff. Native logs retain each tuning observation.

| Mode | Native median ns/day | Process-inclusive median ns/day | Process-inclusive range |
| --- | ---: | ---: | ---: |
| Normal PME autotuning | 214.935 | 211.738 | 211.582–214.430 |
| Original grid, no repeated tuning | 219.978 | 216.638 | 216.231–216.845 |

Paired gains: **+2.314%, +2.197%, +1.126%**; median **+2.197%**. All seven runs,
including warmup, passed; 134 files / 210,430,365 bytes independently rehashed.
In the first pair the native wall time fell from 401.982 to 392.768 s and the
native `Rest` category from 12.399 to 4.251 s, consistent with avoiding search
overhead. This is not evidence of a proportional steady-state kernel speedup.

Recommendation: a small candidate for this exact input and execution shape,
not a global GROMACS default. Confirm on an isolated AMD/350 W host through the
normal API and resumed late-state path before promotion. Other inputs/hardware
may benefit from autotuning. Do not add this gain to the continuous-process
gain: avoiding restarts also avoids repeated startup/tuning. No combined
factorial test, production rollout or customer restart was performed.

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

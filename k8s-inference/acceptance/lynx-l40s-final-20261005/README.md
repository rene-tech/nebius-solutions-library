# Final bounded L40S tuning — 2026-10-05

Linked task `fs2-lynx-l40s-final-round-r20261005`, parent
`fs2-lynx-gromacs-longrun-performance-r20261005`. This supersedes further MPI
benchmarks. Parent owns the actual six-hour checkpoint resume in the demo
account and its >=200 ns/day delivered acceptance; this task cannot establish
that public handoff with a native Pod result.

One dedicated free L40S, eight requested/limited CPUs and 16 GiB. The short
screens use single worker
`5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd`;
the final confirmation uses
`ca863f44c7d17c8096267ec43939cda8b9546f0d3b11440e62bcf9149bc94a1e`
(source `c5c66f320`, the same native engine with the additive nonempty-file
expansion wrapper). Neither result is reassigned to another image.
The approved private original TPR has SHA256
`e2ee73571f0dd9855709d2a957e41e5ad52316b3f1d2b1808e65476f4bd8ef10`.
Only a finite nsteps override is applied. Seeds, timestep, force field, physical
cutoffs, original positive Verlet tolerance and output cadence stay fixed.

`run_screen.py` reuses the existing Pod/capacity and native validation helpers.
It uses no API key, public lane or customer bucket. It refuses another active
application on the dedicated single-L40S target; cleanup checks the exact Pod
UID/task label and retains absence. No node policy, clock, limit or privilege
changes. Provider/system DaemonSets remain untouched.

`screen_inside.py` reuses `benchmark_sm89.py`: each list interval 200/100/300
gets a fresh-cache run and three warm 50000-step (100 ps) repeats, with native
energy/checkpoint/coordinate validation. One process-local affinity variant
uses eight distinct allowed physical cores, preferring the GPU's NUMA node;
actual native thread masks are retained. It does not change memory policy or
claim isolated physical CPUs. A single explicit CUDA Graph eligibility attempt
retains any incompatibility rather than changing CHARMM CMAP forces or retrying.
Graph eligibility is not a performance claim. Affinity/environment changes are
operator-only experiments, not silently published API features.

`--public-pin-probe` runs exactly one additional four-run control using the
existing public `-pin on` option with `nstlist=200`, without taskset or OpenMP
binding variables. It is distinct from binding the process before CUDA creates
its helper threads, and cannot be inferred from that operator-only experiment.

The best public-expressible list recipe receives a separate three-by-1ns
confirmation through the existing worker/recipe harness. Native counters,
process wall, setup/validation and public delivered clocks remain distinct.
The dedicated single-L40S CPU platform differs from prior AMD four-L40S public
runs; do not claim matched cross-host speedups. All failed candidates stay in
the denominator, and no customer default is changed here.

Primary sources checked October 5:
[GROMACS performance guide](https://manual.gromacs.org/2026.2/user-guide/mdrun-performance.html)
for accuracy-preserving list tuning and measured rank/thread/affinity choices;
[GROMACS environment variables](https://manual.gromacs.org/2026.2/user-guide/environment-variables.html)
for conditional experimental `GMX_CUDA_GRAPH`. Installed 2026.2-dev dispatch
and retained logs, not merely the flag, decide whether graphs were used.

## Completed short screen

The existing public-compatible recipe remains the choice: eight threads,
`-nb gpu -bonded gpu -pme auto -update auto -pin auto -nstlist 200`.
Twenty-one short native runs passed; none of the tested alternatives improved
the retained three-warm-repeat median. Each list/affinity/pin control also has
one separately retained fresh-cache run. These 100 ps screens are not sustained
capacity estimates.

| Candidate | Warm native ns/day | Median |
| --- | --- | --- |
| List 200, pin auto | 204.085, 203.282, 202.958 | 203.282 |
| List 100, pin auto | 174.409, 195.515, 195.799 | 195.515 |
| List 300, pin auto | 175.843, 199.371, 176.501 | 176.501 |
| List 200, external physical-core affinity | 203.144, 179.464, 202.422 | 202.422 |
| List 200, public pin on | 179.265, 203.552, 180.475 | 180.475 |

The single fresh-cache CUDA Graph eligibility run returned 203.064 ns/day.
Its log accepted the experimental option conditionally on no CPU forces, while
the CPU Force bucket remained populated. Graph execution and benefit are
unproven; this is not a failed native simulation or a recommended graph mode.

Actual placement was `computeinstance-e00r165maajnbg7wrr`, `l40s-1x`, Intel
Xeon Gold 6338, eight visible physical cores / sixteen SMT threads, one NUMA
node. GPU UUID `GPU-0e31142d-657d-54cb-aee6-dba624382c24`, PCI `8D:00.0`,
NUMA 0, driver `580.173.02`. The cgroup quota was eight CPUs. Default masks
were `0-15`; the external experiment verified distinct masks on CPUs
`0,2,4,6,8,10,12,14` without changing NUMA memory policy. Even explicit
`-pin on` logged pinning disabled by the CPU quota and retained masks `0-15`.
Thus no working pinning optimization can be claimed from the public flag.

This host is not the prior AMD EPYC-Genoa four-L40S customer/public host.
Do not merge their rates, change public placement, or infer a matched speedup.
Native short-range/most bonded, PME and update/constraints were GPU-dispatched;
CHARMM CMAP physics remained intact. Full TPR comparison completed all sections
with only `nsteps` different (500000000 to 50000); seeds, timestep, force field,
cutoffs, tolerance and output cadence were not edited.

Private evidence root:
`/home/tux/secure-handoff/fs2-lynx-l40s-final-20261005`.
The two original receipts are `screen-r1/receipt.json` and
`public-pin-r1/receipt.json`. Both exact owned Pods were deleted with absence
observed before the next Pod was created. Their 273 output files were rehashed.

The final three-by-1ns confirmation is retained separately in
`confirmation-r1`, with request/fixture under `confirmation-fixture-r1` and
read-only GPU/CPU samples in `confirmation-observer-r1`. The sampler starts
after native execution began; that initial interval remains unknown.

## Final exact-wrapper confirmation

Completed at **20:33:56 UTC** on October 5. Three independent 500000-step
(1 ns) repeats passed on the exact `ca863f44...` wrapper, each in one native
segment: **217.082, 216.630 and 217.533 ns/day**, median **217.082**. These
rates are calculated from the retained native Wall counters; the third native
printed rate is 217.534 owing to its counter precision. The observed maximum is
not a guaranteed upper speed bound on this or the separate AMD host.

| Clock for the same 3 ns | Seconds | ns/day |
| --- | ---: | ---: |
| Native mdrun Wall counter | 1194.025 | 217.081 |
| Actual mdrun subprocess wall | 1212.454 | 213.781 |
| Local worker workflow wall | 1217.257 | 212.938 |

The subprocess is 18.429 seconds longer than the native counter; no separate
initialization timer assigns that difference to a specific phase. The twelve
analysis commands took a measured total 0.384 seconds. Native GPU counter time
is not pure GPU compute. Platform publication, customer export and public
delivered throughput are **unmeasured**, not zero. The dedicated Pod's scheduled
to observed-release interval is bounded at 1262.493–1280.760 seconds, including
qualification/evidence handling; it is not a cloud invoice clock.

Actual dispatch stayed GPU short-range/most bonded, GPU PME and GPU
update/constraints, eight OpenMP threads and one thread-MPI rank. At the
unchanged positive tolerance, GROMACS changed its neighbor-list buffer radius
from 1.209 to 1.742 nm for list200; this is not a change to the physical force
cutoffs. Native counters show neighbor search 10.3–10.4%, CPU Force 11.4–12.0%
and `Wait GPU state copy` 65.7–66.4%. The last is a native wait/synchronization
bucket, not an isolated transfer or pure-CPU measurement.

The 98 retained irregular samples had no query failures, mean GPU utilization
82.837% and mean CPU use 7.562 cores over the sampled interval. GPU memory's
observed maximum was 567 MiB. The sample window is incomplete and includes
non-integration work; these are not time-integrated capacity or energy values.

All three final native checkpoints reached exactly 500000 steps; all energy
and 185486-atom coordinate checks passed, with no fatal/LINCS warnings and no
local checkpoint step replay. All 48 final inventory files were rehashed
(132981982 retained bytes, not measured transfer bytes). Independent final
validation rehashed 32 report references and 321 native inventory entries
across all 24 runs and confirmed non-overlapping one-GPU Pod lifetimes. Exact
final Pod `fs2-lynx-perf-cpu-final-confirm-r1`, UID
`af90a64b-1367-4284-a352-beee9bb15c48`, was deleted with absence observed; the
task-only sampler then exited normally on its STOP marker.

No additional tuning improvement was found. Hand over the unchanged recipe
above for the separately owned DEMO checkpoint-resume acceptance. Native-only
results do not satisfy or predict its >=200 ns/day delivered requirement, nor
qualify six-hour timeout behavior.

Immutable report: `final-report-r1/report.json`, SHA256
`fc386fc9a5eee457b7069119d4920fb26e6cad21ccab26bfe939987a03e80cf3`.
Final native receipt: `confirmation-r1/receipt.json`, SHA256
`85a5433afa1a4fb67fded24f35f1438a0d6aa4ee896ca8b4e3708f83709be8d4`.
The output paths are relative to the private evidence root above; no trajectory
copies or public/customer credentials are committed.

`report.py` creates one fresh output directory, reuses the existing native,
phase, allocation-bound and sample validators, and rehashes the source evidence.
It distinguishes native counter, mdrun process and local worker wall clocks.
Public delivered throughput, remote checkpoint publication, customer export
and total network bytes remain null. Retained file bytes are not transfer bytes.
The original-state repeats are neither a continued customer trajectory nor an
ensemble-convergence claim.

Run the collector only after the native Pod is absent and the observer has
stopped:

```sh
components/control-plane/.venv/bin/python acceptance/lynx-l40s-final-20261005/report.py \
  --screen "$LYNX_FINAL_EVIDENCE/screen-r1" \
  --public-pin "$LYNX_FINAL_EVIDENCE/public-pin-r1" \
  --confirmation "$LYNX_FINAL_EVIDENCE/confirmation-r1" \
  --fixture "$LYNX_FINAL_EVIDENCE/confirmation-fixture-r1" \
  --samples "$LYNX_FINAL_EVIDENCE/confirmation-observer-r1/samples.jsonl" \
  --output "$LYNX_FINAL_EVIDENCE/final-report-r1"
```

`LYNX_FINAL_EVIDENCE` denotes the exact private evidence root above, not a
customer bucket. Focused tests: `python -m pytest -q
acceptance/lynx-l40s-final-20261005`; Ruff checks the same directory.

# Final bounded L40S tuning — 2026-10-05

Linked task `fs2-lynx-l40s-final-round-r20261005`, parent
`fs2-lynx-gromacs-longrun-performance-r20261005`. This supersedes further MPI
benchmarks. Parent owns the actual six-hour checkpoint resume in the demo
account and its >=200 ns/day delivered acceptance; this task cannot establish
that public handoff with a native Pod result.

One dedicated free L40S, eight requested/limited CPUs, 16 GiB, unchanged single
worker `5acd77d66257593896c2fe09cac15392adfffaf00fc7f7625d228a6c8d6084dd`.
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

Status: source/tests prepared; no native or public result is implied by this plan.

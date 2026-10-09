# Exact runtime candidate regression, 2026-10-03

Six bounded native shapes passed on free GPUs. This is **not hosted API/MCP or
customer readiness**, a performance recommendation, or GPU process snapshot
qualification. The wrapper source identity is `0aecab6bd`.

Exact images, in `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs`:

- Single: `sha256:dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93`.
- External MPI: `sha256:c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea`.

The registry index/platform distinction is retained in each direct-Pod receipt.
Observed driver was `580.173.02` throughout. The single engine reports
`2026.2-dev`; external MPI reports `2026.2-dev-20260505-da9e013-local`,
Open MPI 5.0.8, cuFFT, and `Multi-GPU FFT: none`. They are not identical engines.

## Scope and results

Immutable MPINAT MEM input: 81,743 atoms, 2 fs, three 10,000-step/20 ps timing
repeats. Bundle SHA256:
`a2748bf521f68334fa0ebb38a5cd5957a82b4a9718b948e1fc9ac253342743d2`.
The corrected saved REST-C parameters remove the old forced halfway timing
reset; original TPR bytes, physical parameters, seeds, constraints and native
output cadence remain unchanged. These repeats restart the same benchmark
state, not three independent equilibrium samples.

| Engine / shape | Observed pool | Native ns/day, three warm-up-inclusive repeats |
| --- | --- | --- |
| Single / 1 GPU H100 | h100-ondemand-1x | 144.606, 91.682, 106.327 |
| Single / 1 GPU L40S | l40s-1x | 85.440, 87.925, 87.022 |
| MPI / 1 node × 1 H100 | h100-ondemand-1x | 59.061, 66.993, 62.835 |
| MPI / 1 node × 2 L40S | l40s-4x | 41.778, 44.873, 34.576 |
| MPI / 1 node × 4 L40S | l40s-4x | 47.123, 48.194, 45.292 |
| MPI / 2 nodes × 1 H100 | h100-ondemand-1x | 29.183, 28.960, 29.532 |

Every successful workspace inventory was SHA256/size checked. All 18 native MD
runs completed to checkpoint step 10,000, and every final GRO contains 81,743
finite coordinates and a finite positive periodic cell. Native potential energy
and temperature are finite at 20 ps. MPI results contain complete observed
rank/local-rank/GPU-UUID bindings. Legacy 2×1 final checkpoint steps were also
independently read with the exact binary's `gmx_mpi dump -cp`.

The original fixture outputs only a final energy sample; these checks do not
establish energy drift or ensemble convergence. All qualification Pods and the
legacy JobSet were deleted after evidence copying; Pod absence was observed.
No customer workload, pool, quota, node, service image or catalog was changed.

## Retained limitations and failures

- Single native placement was automatic PME; MPI regression deliberately used
  `-pme cpu -npme 0` and CPU update for original all-bond constraint chains.
  Bonded placement remained automatic and sometimes offloaded bonded terms.
  Consequently the above single/MPI ratios are **not matched PME speedups**.
- PME/DLB tuning may still be unfinished at 10,000 steps. No forced counter reset
  means timing includes warm-up; do not label it post-tuning steady state.
- Local MPI configured UCX-local, cross-node MPI host-staged TCP. Actual
  transport telemetry is null, not evidence of RDMA/CUDA-IPC bandwidth.
- PMIx's missing compression-library warning and CPU quota/pinning warnings are
  retained. No warning suppression or unmeasured runtime rebuild was performed.
- Initial single H100 and L40S attempts mistakenly selected the old forced-reset
  request and hit the known PME-tuning fatal at step 5,000. Full failed results
  and logs remain in `single-h100` and `single-l40s`; corrected runs are separate.
- A 5 MiB output-budget check correctly failed after finite-TPR conversion and
  retained its diagnostic log with incomplete bounded inventory. Both native
  and committed generations were zero before the first checkpoint. An initial
  validator incorrectly required strict generation inequality; its failed
  receipt and the unchanged-artifact `validation-correction.json` are both kept.
- An early MPI 1×1 L40S run with the old forced reset passed under CPU PME; it is
  retained as unmatched context, not merged into the corrected timing series.
- Legacy 2×1 has an additive release-binder-compatible receipt derived from the
  original qualification and observed Pod/image identities. Its compatibility
  `capacity-before.json` explicitly contains only pool identity, not a retained
  legacy occupancy snapshot; unavailable capacity values are null.
- No new 8/16-GPU, native interruption/restart, API/MCP, artifact transport, or
  persistent CUDA process-snapshot claim follows from this regression.

## Evidence and reproduction

Private evidence base:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/runtime-qualification-r1`.
The summary is `summary-01.json`, SHA256
`949e92e4c9770ed2b372a6ef802fb88c7ed879f9a81134e66d8a4e1835898cb7`.
It binds each fixture, request, native result, final-state validation, image,
GPU identity, raw timing series and cleanup proof. Originals are not overwritten.

`qualify_candidate.py` provides bounded prepare/run/validate and legacy cleanup
commands. `candidate_report.py` produces the additive summary without cloud
calls. Its `--project-legacy` option is one-time/exclusive-create; ordinary
subsequent reporting omits that flag. Live runs require a fresh explicit node
capacity check and create only uniquely labelled task resources.

Tests, from this directory:

```text
python3 -m unittest -v test_qualify_candidate.py test_candidate_report.py
```

18 focused tests pass. The separate named fixed-input PME control in
`pme_control.py` is follow-up optimization evidence, not a mutation of this frozen
six-shape regression.

# Completed baseline and CUDA-aware hosted delta — 3 October 2026

Fixed observation cutoff: **14:51:03.124921 UTC**. This adds to, and does not
rewrite, [the 14:01:36 findings](BENCHMARK_FINDINGS.md). The corrected baseline
now has **24/24 single-GPU inputs and all eight REST/raw-MCP MPI cases**
(1×1, 1×2, 1×4, 2×1). Three separately grouped CUDA-aware candidate cases also
completed. These are throughput/artifact/transport results, not a complete
customer release or scientific-convergence verdict.

## Six additional completion rows

Native rates are median (minimum–maximum), three 10,000-step starts from the
same finite TPR, without forced timing reset. Every new row completed all three
timings and reverified delivery: 38 artifacts for PEP; 36 for each MPI case.
PEP has 12,495,503 particles; MEM has 81,743. Both use 2 fs steps: summed
integration over the three repeats is 0.06 ns, **not one continuous trajectory**.
M particle-steps/s is derived native throughput, not GPU utilization.

| Cohort / input / path | Shape | Actual GPU | Native ns/day, n=3 | M particle-steps/s | Allocation-share USD |
| --- | --- | --- | ---: | ---: | ---: |
| Baseline PEP / REST | 1×1 | L40S AMD | 2.070 (2.066–2.082) | 149.686 | 1.860–1.876 |
| Baseline MEM / REST | 1×4 | L40S AMD | 49.192 (46.794–53.137) | 23.270 | 0.457–0.488 |
| Baseline MEM / raw MCP | 1×4 | L40S AMD | 47.967 (45.690–49.926) | 22.691 | 0.024–0.481 |
| Candidate MEM / REST | 2×1 | H100 | 64.343 (64.088–64.648) | 30.437 | 0.37250 † |
| Candidate MEM / raw MCP | 1×2 | L40S AMD | 99.052 (98.056–99.350) | 46.857 | 0.15483 † |
| Candidate MEM / raw MCP | 1×4 | L40S AMD | 172.341 (161.721–172.839) | 81.526 | 0.25382 † |

Costs use the **2026-10-03 eu-north1 on-demand allocation-share scenario**,
not an invoice, customer charge or GPU-busy cost. H100 is $4.50/GPU-hour;
the observed four-L40S AMD VM is $9.1376/hour including its CPU/RAM, apportioned
as $2.2844 per allocated GPU-hour. Storage, network, taxes, discounts, control
plane and unallocated VM time are excluded. Rates and preset assumptions remain
the [dated reference file](public_cost_references.json), sourced from
[official pricing](https://docs.nebius.com/compute/resources/pricing).

Baseline ranges come from Pod observation bounds; the wide MCP 1×4 range is
missing terminal-state sampling, not a measured near-zero cost. **†** Candidate
costs instead use the bounded durable production scheduler clocks: respectively
298, 244 and 400 GPU-seconds. All are reconciled/application-observed and inside
the independent offline bounds. Corresponding delivered-work efficiency is
0.725, 0.885 and 0.540 simulated ns per allocated GPU-hour. No HTTP-versus-MCP
overhead or universal GPU-scaling conclusion follows from these different runs.

## Runtime and protocol separation

Baseline MPI remains `c6c353e55deade8c8fe7a2f02e68bcdb639c7680c8435b665e72815d0213c9ea`
with the saved `auto` protocol. PEP uses single-GPU digest
`dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93`;
earlier single-GPU images remain identified in the original findings.

The three candidate worker imageIDs are exactly
`5884569e80a4d8dd5cc04b05fe7700165139874cde00b2774cba7c7c96965e23`,
with API/collector digest
`57c352294a7b24bcea39470f849b4c66fa1db777e7559ff9d0966af6124862da`.
All use eight threads per rank and explicit
`-nb gpu -update cpu -bonded cpu -notunepme -pme gpu -pmefft gpu -npme 1`.
Native logs confirm GPU PME and, for both local L40S shapes, CUDA-aware MPI with
direct GPU communication. Cross-node H100 remains **host-staged TCP**, not RDMA.

MEM's original TPR is
`5099268bf3a3d948c03b3b78432f29a2c5d4207b54d02e7e294f8138b587d473`;
the finite TPR is
`4a0d845ae01b8c95b1077cd099c5769fce99d939844e24d21f1d1d90752540b9`.
These identities match all eleven retained MPI runs. Fixed GPU PME changes the
offload/tuning protocol relative to the earlier `auto` cohort, so their rate
ratios are **not matched runtime speedups**. Separately matched native controls,
including the retained strict printed-value failures and descriptive envelope
misses, remain in [CUDA_AWARE_CONTROL.md](CUDA_AWARE_CONTROL.md); this report
does not widen their tolerances or substitute for scientific validation.

The [primary MPINAT publication](https://www.mpinat.mpg.de/632182/bench.pdf)
provides no numerical MEM/PEP baseline. Its other historical figures use
GROMACS 2018, CUDA 8 and GTX 1080, with different/unspecified timer details.
The original twelve comparisons and RIB input-version warning remain unchanged;
all machine-readable public matched-speedup fields remain null.

## Measured phase reservation, not pure computation

Below are admitted GPU count × measured process/counter seconds, summed over
the operation. They are **overlapping scopes, not an additive timeline**.
Native Wall t is inside mdrun process wall and is not pure GPU integration.

| Candidate path / shape | Init-container GPU-s | Native Wall t GPU-s | MPI staging GPU-s | Analysis GPU-s | Post-worker collector GPU-s |
| --- | ---: | ---: | ---: | ---: | ---: |
| REST 2×1 | 8 | 161.114 | 0.649 | 1.369 | unknown |
| raw MCP 1×2 | 10 | 104.932 | 0.015 | 6.492 | 6 |
| raw MCP 1×4 | 20 | 122.852 | 0.030 | 19.406 | unknown |

Cross-node input staging measured 0.324643 seconds and 3,424,316 transferred
bytes; local runs transferred zero peer bytes. Exact native initialization,
pure integration, checkpoint duration and total export remain **null**.
Production `active_compute` still includes broader worker activity, and zero
classified checkpoint/export time is not proof of zero work. Production rows
retain `trace_context_missing`; REST 2×1 also retains
`phase_classification_incomplete`.

The future-interval quota fix is now proven live for REST operation
`59d0ffc8-9ee2-4553-ae19-1269466115fa`: frozen gang size two, actual Kueue PodSet
count two/GPU usage two, and both durable quota edges count **two GPUs**.
The quota rollup is **313.1329 GPU-s = 2 × 156.56645 s**. This separate clock
need not equal scheduler occupancy 298 or device allocation 296.016037 GPU-s.
Historical undercounted quota rows were not modified; generic top-level
reserved-GPU zeros remain reservation semantics, not measured free execution.

## Preserved limitations and evidence

The 58-operation snapshot retains **18 failed and one cancelled** operation,
including the original before-native MPI failure. Four REST-A successes remain
historical forced-reset runs, outside the 35 corrected comparison rows.
The same twelve explicitly verified retry groups retain thirty memberships;
candidate/control runs never enter them. PEP's now-completed retry proves at
least **10,000 duplicated durable steps** (0.02 ns, 124,955,030,000 particle-steps).
The RIB and PEP-h lower bounds remain 20,000 each; MEM remains zero *known*
duplicated durable steps, not zero wasted cost. PEP's entire retry-chain cost has
unknown lower bound and $3.601 upper bound; uncommitted lost work remains unknown.

[Actual Lynx requests and assumed examples](LYNX_WORKLOADS.md) remain distinct:
three evidence-backed requests, six explicitly assumed CPU controls, and a
separately assumed hosted example. Raw MCP timing is not actual-agent MD
qualification. The current agent/client acceptance is tracked separately by the
release owner. Whole-node 8/16-GPU tests remain capacity-blocked; no customer
movement or capacity claim is implied by this report.

Private immutable snapshot:
`/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003/benchmark-findings-r2`.
It contains 58 operations/attempts, 433 checkpoints/commands (134 mdrun), 54 Pod
allocations, all 58 selected receipts/plans and 35 completed comparison rows.
The collector rehashed 1,493 retained artifacts / 17,658,317,949 bytes and used
hard links rather than copying trajectories. No original campaign, old report,
runtime, policy or historical database row was changed.

- `report.json`: `845d095f5b53876ce30a3e57d333036d3c640e3dc7ab033fe4da631cf3426bbc`.
- Additive three-operation `durable-lifecycle-sidecar.json`, captured 14:53:33 UTC:
  `3a8e04ff00ceb14dc1248cd5a84d9897bd3645c7ba095d93523e5e0df271c711`.
- Actual admission and quota edges in sibling `quota-candidate-r1/quota-terminal.json`:
  `9d488dd877c43ccd651a3f9c783263c5609039a2501f672e6bde6c341a7f649e`.
- Append-only observer handoff in sibling `observer-handoff-r1/receipt.json`:
  `1a8d58f1bab82dbd3a57b1a4fdc08f1a49ee1bdcf4315146a3ddf5e8e7e025b1`.

The old observer exited naturally after the corrected observer produced
successful samples; both current Pod streams overlapped by about 46 seconds.
Earlier gaps are not filled with zeroes. Telemetry-d continues under the root
campaign owner; this snapshot has its own cutoff. Focused ledger/cost/native
timing/plan tests: **57 passed**. Private `EVIDENCE.md` records exact collection
commands, hashes, validation and cleanup ownership.

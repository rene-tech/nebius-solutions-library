# Task Deck handover

The user requested a complete, supervised BioNeMo Inference Runtime evaluation
on unused Scientific AI H100/L40S capacity, including current-runtime baselines,
actual incremental speedups, model/feature quality, snapshots, costs and a report.
This was an evaluation authorization, not a production-promotion authorization.

The manager used one shared worktree and three parallel workers. Root also
owned the Protenix core comparison. Workers handed capacity back between fixed
studies; no new capacity, quota, driver or customer-serving change was made.
The final review happened after the GPU studies, not as a recurring review loop.

The original [experiment contract](experiment-contract.md) and the task records
below are retained for resumption. They include chronological observations;
later closure receipts supersede older running/pending states. The consolidated
[report](../report/report.md), [review](../report/final-review.md) and
[final cluster receipt](../report/final-cluster-state.json) are the closure view.
`done` means the evaluation/report is complete, **not** that every prototype passed
or was deployed. Negative and unsupported results are intentional outcomes.

| Ticket | Scope |
|---|---|
| [Manager](tasks/fs2-bioir-manager-r20260915.md) | Inventory, supervision, integration and publication |
| [Boltz2](tasks/fs2-bioir-boltz2-r20260915.md) | Matched H100/L40S native, resident, optional-kernel and BioIR comparisons |
| [OpenFold](tasks/fs2-bioir-openfold-r20260915.md) | OpenFold2/3 precision, graph, residency and quality comparisons |
| [Coverage](tasks/fs2-bioir-coverage-r20260915.md) | Eight other current baselines and sourced applicability; related-model notes |
| [Protenix](tasks/fs2-bioir-protenix-r20260915.md) | Same-H100 prepared-input, native batch and reference-quality study |
| [Snapshots](tasks/fs2-bioir-snapshot-r20260915.md) | Boltz2/OpenFold2 fresh-pod restore, controls and fallback |
| [Protenix snapshots](tasks/fs2-bioir-protenix-snapshot-r20260915.md) | Three independent cohorts; final non-preemptible control matrix |
| [OpenFold3 snapshots](tasks/fs2-bioir-openfold3-snapshot-r20260915.md) | Restore and ordinary-load repeated-key failures, fallback and cleanup |
| [Report](tasks/fs2-bioir-report-r20260915.md) | Consolidation, final cross-lane review and decision-ready handover |

Canonical live cards remain under
`/home/tux/dashboard/data/epics/nim-fast-start-platform/tasks/`.
Their publication receipt is appended after the containing Git commit exists;
the committed snapshots cannot include their own future commit hash. Use
`git log -1 -- k8s-inference/acceptance/bioir-20260915` to identify the publication.

To resume proposed optimization work, use the recommendations in the report and
obtain the user's implementation direction. Recheck current capacity before
running retained manifests; node names, free GPUs and snapshot identities are
not reusable capacity guarantees. Temporary checkpoint pages were removed;
recapture them using the pinned sources. No evaluation worker is intended to
remain running after this handover.

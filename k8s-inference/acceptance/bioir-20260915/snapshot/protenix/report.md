# Protenix BIR snapshot evaluation

Primary cohort: **y0jt**, technical evidence status **pass**. Restore container→ready median **88.909s**, ordinary-load median **57.820s**; ratio **1.538×** (above 1 means slower). Numerical and scientific equivalence are **not established**. No promotion.

| Independent cohort | Completed fresh restores | Matched normal controls | Interpretation |
| --- | ---: | ---: | --- |
| Original fkt | 2 | 0 | Restore3 interrupted; no matched ratio |
| Replacement xjaw | 3 | 2 | Normal3 interrupted; fallback unrun; descriptive 3v2 only |
| Final non-preemptible y0jt | 3 | 3 | pass; independent fresh donor and driver |

Read the [primary cohort report](y0jt/report.md) for exact image/source/checkpoint, GPU/driver, phase clocks, matched request histories and quality dispersion. Its 18 real predictions passed native validation, exact sequence, finite-coordinate and actual graph-state checks. Matched-history structure byte identity: 0/6 against normal control1. Both repeated normal and restored outputs are compared; variation is retained without attributing its cause. First-output shapes differ, so no first-output speed ratio is claimed.

The [original fkt report](fkt-report.md) retains two valid restores, 12 failed CPU-preparation stages and two pre-inference harness failures. The [xjaw partial report](xjaw/report.md) retains three valid restores and two matched controls: 87.741s/60.411s = 1.452× slower readiness descriptively, with the missing third control and fallback explicit. Their [fkt incident](../../report/capacity-incident-fkt.json) and [xjaw incident](../../report/capacity-incident-xjaw.json) record heartbeat loss before provider Stop operations. The initiating causes remain unresolved; neither preemption nor CUDA/CRIU faults are proven. Physical GPU UUIDs and the final driver differ: **do not pool these cohorts into a matched speedup or quality claim**.

The underlying Protenix model-lane experimental-reference quality regression remains disqualifying independently of technical snapshot compatibility. No production image, route, model, driver, quota or customer workload was changed.

Verification: [primary checks](y0jt/verification.json), [partial xjaw checks](xjaw/verification.json). Resource disposition: [cleanup receipts and remaining objects](cleanup.json). Only exact task-owned resources receive normal deletion requests. Stopped-node pods/PVCs or cloud volumes may remain unreclaimed; no force deletion, storage-finalizer edits or stopped-VM changes. GPU diagnostics and Kubernetes ownership/incident workflows governed these boundaries.

Rebuild reports using the matching `BIOIR_SNAPSHOT_COHORT=fkt`, `xjaw` or `y0jt`; `summarize.py`, `write_report.py` (use `--complete` only for a fully completed matrix) and `verify_evidence.py` (`--partial` for xjaw). Then use `aggregate.py --primary y0jt`. Preserve `fkt-result.json`/`fkt-report.md` before replacing root entrypoints. New execution requires a fresh authorized capacity check; retained manifests and names must not be replayed blindly.

The identity-mismatch fallback rejected restore before executing any restore command, loaded a new worker and validated both inputs. Its 13.421s container→HTTP-ready observation reused the donor executable cache; it is a single separate cached-start path, not one of the three cold-cache normal controls or a qualified cache-only optimization. See [fallback audit](y0jt/fallback-verification.json).

Final cleanup observation: all three cohorts’ task pods, PVCs, PVs and ConfigMaps are absent through normal deletion. The final y0jt H100 reports 0 MiB and no compute processes. Historical pending-reclamation receipts remain. The final manager receipt shows fkt and xjaw Ready=True again (transitions 23:17:17 and 23:16:58 UTC); their earlier STOPPED observations are historical, not current disposition. Their GPU memory was not re-probed after interruption. Manager cross-check: [final cluster state](../../report/final-cluster-state.json). No local benchmark runner remains.

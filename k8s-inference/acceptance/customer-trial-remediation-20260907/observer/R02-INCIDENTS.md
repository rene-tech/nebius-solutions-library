# r02 incidents — retained failures, not clean acceptance

Scientific campaign began 2026-09-07T20:10:47.879537Z on the unchanged deployed source `5f5061b28ee71a59432492a1bdf6106428a85367`. These observations do not authorize intervention or relabel a failing request. Root owns fixes/deployment and the workload lane owns test cleanup.

## Proteina submission returned 409 after persisting work

`batch-07-proteina` submitted at 20:25:39.342432Z and received HTTP 409 without a client-visible operation ID. The CP access log records status 409 at 20:25:39.796Z, server duration 231.256 ms. The client made no retry.

The server nevertheless created operation `d6ddbe20-6c2b-4c2e-ab65-03442c9c1b82` at 20:25:39.761496Z. The workload lane reconstructed the final uploaded-input request offline and verified exact equality of its deterministic idempotency key with this operation. This is therefore task-owned work, not an unrelated contemporaneous operation. It automatically succeeded at 20:29:43.523512Z, published a result, and released all four attempts. **The original customer outcome remains failed.** No synthetic successful submitted receipt was created.

Private evidence under `.../trial-customer-remediation-20260907/r02/observer/`: `proteina2-submit-409-loki.json` (SHA256 `01bf1c962831a3841f23cb8402d36cd60a894d0436dc933043d4adcdde596f0f`), `proteina2-failed-submission-association.json`, `proteina2-input-artifact-metadata.json`, and `proteina2-associated-operation-terminal.json`. The workload lane separately retains its exact reconstructed-request association. This observation proves persistence/response inconsistency; it does not alone prove the source-level cause of the 409.

## RF bulk priority requeue blocked reconciliation

RF four-shard operation `daae227f-f7a0-4fe7-9912-1a95c675c3d9` started at 20:30:55.694572Z with priority -100. Actual Kueue events prove shard 003 was preempted by ESMFold2-Fast (priority 0) at 20:31:48Z and shard 000 by AlphaFold3 (priority 0) at 20:31:59Z. Their same `a1` Jobs resumed with replacement Pods; do not describe those Pod replacements as a completed application-level attempt-2 retry.

At 20:36:53Z, three retained Jobs (000/001/003) were Complete=True, with completion times 20:33:29Z, 20:33:59Z, and 20:34:57Z. Runtime and artifact-collector containers exited 0. The original shard-002 Job/Pod was gone. The public operation still reported running; admin displayed the same stale stage states. Those states are not evidence that the completed containers were still computing.

The CP repeatedly logged `scientific batch reconcile failed` from 20:34:39.577Z. The complete traceback identifies `_fence_same_workload_requeue` in `scientific_batch/controller.py`: its synthetic PREEMPTED observation clears `pod_uids` while retaining Pod lifecycle evidence. Dataclass validation raises `ValueError: Pod lifecycle evidence must uniquely bind an observed Pod UID`. This blocks reconciliation rather than completing the intended immutable-attempt preemption/retry boundary.

Private evidence: `rf-bulk-shard-000-preemption.json`, `rf-bulk-shard-003-preemption.json`, `rf-bulk-completed-jobs-stalled-public.json`, and `rf-bulk-finalization-full-trace.json` (SHA256 `00a18d799ced754d4f4035d592f802b8a3f481b274504a4df614a10a46604b43`; 1,158 lines from a five-second unfiltered window, below the 5,000-line limit). The broader filtered window contains 2,454 lines. Root received the exact diagnosis; observer made no production change.

## Admin result-publication race

The browser lane separately observed Protenix become terminal before artifacts and semantic-validation publication. The UI stopped polling at that first terminal state and needed navigation to show the later artifacts. That lane owns exact browser receipts and the fix. The prior restore-duration bug is fixed: the fresh r02 Protenix CUDA/CRIU marker and lifecycle ledger show 4.196746 seconds, displayed as 4.2 seconds. Do not combine this successful check with the publication race into an overall pass.

## Finalization

These incident observations were captured while r02 was active. Its test workers subsequently stopped after bounded failure handling; final counts, whole-window Qwen publication logs and stop receipts are in REPORT-r02.md. No inference retry, cancellation, policy change, capacity-limit increase, or deployment was performed by observer during r02. The retained RF operation remains nonterminal pending root's repair release on September 8.

# Retained RF batch recovered automatically

After root deployed source `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`, the original stalled r02 RFdiffusion operation `daae227f-f7a0-4fe7-9912-1a95c675c3d9` **succeeded and fully released its resources without cancellation, resubmission or manual workload changes**.

| UTC, 2026-09-08 | Observed or authoritative event |
| --- | --- |
| 07:32:36.720969 | Before recovery: public operation running, result unpublished; three completed old Jobs still retained in `fs2-models`. |
| 07:33:08.015020 | Fresh attempt-2 work created automatically for shards 000, 002 and 003. Shard 001's original successful attempt was retained. |
| 07:33:30.494304 | Observer sees the three replacement attempts running; original attempts are released. |
| 07:34:46.830146 | Authoritative public operation completion: succeeded. |
| 07:34:50.945516 | First sampled succeeded/published state; all eight attempts released, no matching Jobs or Pods. |
| 07:35:17.561737 | Second independent terminal/empty-resource confirmation. |
| 07:35:19.194885 | Bounded watcher exited normally after seven samples; process absence confirmed. |

Final history is preserved: **three preempted attempts and five successful attempts**—the successful original shard, three automatic replacement shards and the collection stage. All eight report released resources. All seven public status reads returned HTTP 200. A separate read-only result fetch at 07:35:42.566821 returned HTTP 200 in 0.443 seconds and confirmed published semantic validation **passed**, validator `rfdiffusion-v1-1-0`.

[Selected before/after receipt and hashes](RF-RECOVERY-r20260908.json) bind the exact operation, source, old Job identities, attempt counts, terminal state, semantic result and cleanup to private raw evidence. Raw receipts remain under `releases/trial-customer-remediation-20260907/recovery-r02-r20260908/` in the private H100 acceptance directory. No credentials or biological payloads are in the selected export.

This is **post-release recovery evidence**, not a rewrite of r02. The original customer's blocked/interrupted request and failed cohort remain recorded exactly as before; its client was not restarted and no terminal client receipt was fabricated. No r03 requests were sent. The next unchanged customer cohort still requires root START after deployment/no-op, admin and separate Qwen qualification checks.

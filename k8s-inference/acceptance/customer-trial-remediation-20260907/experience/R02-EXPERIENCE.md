# R02 operator experience — failed cohort

Deployed source: `5f5061b28ee71a59432492a1bdf6106428a85367`.
The browser used the existing bootstrap operator with known trial fixtures. This is
not a test of unaided customer onboarding or least-privileged customer access.

The phase-duration fix passed live, but this cohort is **not clean**. It exposed an
automatic result-publication UI race, an accepted Proteina request whose client received
409, and an RF batch controller reconciliation stall. No production changes, model
submissions, policy changes, client retries, or server-side cancellation were made by
the browser lane. A single ordinary page navigation was needed to retrieve Protenix
results; that is preserved as a failure of automatic completion, not hidden recovery.

## What worked

- RFdiffusion advanced from running to complete without navigation or refresh; the
  browser downloaded and hash-verified its 20,368-byte structure.
- New Protenix restore time was **4.196746 seconds**, visibly **4.2s estimated**, from
  actual lifecycle signal boundaries. Its separate one-GPU ledger reconciled at
  35 occupied GPU-s = 16.803254 active + 18.196746 idle. The old retained Protenix run
  also passed the exact 3.946846-second preflight.
- Context and run observation timestamps were clearly labelled. Phase copy explicitly
  described wall-time unions, not additive latency or GPU-seconds.
- ESMFold2 advanced automatically through matching-node wait, artifact loading,
  computation and published results. Its structure download was hash-verified.
- The second BindCraft request visibly waited for admission, then automatically
  advanced to active computation on the suitable reserved pool. No manual action was needed.
- The four-shard RF batch initially showed four running shards while individual rows
  distinguished two computing from two loading artifacts. Later it showed a retryable
  preemption alongside three running shards rather than declaring the whole run failed.
- The live scientific run list showed the active mixed workload and model policies;
  model/policy settings were not changed.

## Must fix before an unattended trial

1. **Result publication polling.** Protenix became controller-successful before its
   artifacts were published. Polling stopped at that instant, leaving empty results
   indefinitely until one page navigation. The precise evidence and **offline, not
   deployed** correction are in [TERMINAL-PUBLICATION-RACE.md](TERMINAL-PUBLICATION-RACE.md).
   The narrow correction passed all 151 UI tests and TypeScript checks.
2. **Proteina submit consistency.** The client received 409 while the server committed
   and successfully executed its request. The workload/observer lanes matched the
   exact deterministic idempotency identity. Admin exposed the resulting operation
   `d6ddbe20-6c2b-4c2e-ab65-03442c9c1b82`, which completed with validated artifacts.
   That server completion does not convert the original client failure into a pass.
3. **RF preemption reconciliation.** The observer identified a controller validation
   exception during same-workload requeue: retained Pod lifecycle evidence no longer
   matched cleared Pod UID bindings. Public state stayed running after jobs completed.
   The browser accurately reproduced that stale API state; this is a backend stall,
   not a second UI rendering bug. The operation remains nonterminal and has not been
   manually cancelled. Runtime/source diagnosis belongs to the observer's report.

## Separate unresolved preflight observation

The first post-release browser attempt remained blank before sign-in. The same release
subsequently loaded correctly in a separate diagnostic and the authorized second
preflight passed all phase, timestamp and download assertions. The original cause is
unknown; it is not relabelled a harness-only failure. See
[phase-followup-20260907.json](phase-followup-20260907.json).

The full browser session also captured three failed automatic detail reads with
`net::ERR_NETWORK_CHANGED`: 20:11:54.990Z (RFdiffusion), 20:13:05.773Z and
20:13:06.859Z (Protenix). Later automatic queries recovered without manual interaction.
These are real browser transport failures, not hidden HTTP successes. Their cause
was not diagnosed, and they do not establish the cause of the earlier blank page.
There were no page JavaScript exceptions or unexpected HTTP error statuses; the
initial unauthenticated session request returned the expected 401.

## Evidence and handoff

The browser records actual UI-generated responses; it does not issue a separate
scientific status polling loop. Explicit navigation/snapshot/download actions and
their timestamps remain in the exported observation receipt. Raw responses and
screenshots stay private. The final
[browser observation receipt](r02-browser-observations.json) contains 316 automatic
scientific UI query responses, nine sampled run-detail views across six models, 27
explicit browser actions, and three exact-byte structure downloads. Some sampled
views were left before their run completed; their last observed state is not a
claim about the later terminal outcome reported by the workload lane.

The failure boundary was retained at 20:40:23.571Z. The final unchanged RF state was
captured at 20:42:15.439Z, over 111 seconds later. The browser closed at
20:42:15.747Z; the exact local Node process then exited after closing its terminal
input. No browser or browser-generated polling remains. The underlying RF operation
`daae227f-f7a0-4fe7-9912-1a95c675c3d9` is still nonterminal; stopping local observation
did not repair, cancel, or otherwise mutate it.

Receipt SHA-256: `13b25c2529ffc91bcc41d109ed02848886c008fb0f4caa89d7533d4e3b9a8a9a`.

Only the two scientific detail UI files have an offline production fix. The release
owner must integrate/deploy that fix together with the backend corrections, then run
the next authorized cohort. This lane has not declared the cluster ready or changed
the running release.

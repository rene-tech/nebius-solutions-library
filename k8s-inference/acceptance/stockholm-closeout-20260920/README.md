# Stockholm closeout — 20 September 2026

Stockholm's active platform identities were removed at **06:20:13 UTC**:
20 configured team accounts deleted, all 22 retained keys revoked, and the tenant
excluded from active user/key discovery. Two keys were already-revoked test keys.
Historical token records remain as non-authenticating references, not active
credentials. The database retains a deletion marker so old operation history
cannot recreate the accounts. This is not a purge of historical evidence.

Other tenants' account and key policies compared equal immediately before/after.
No buckets, cloud service accounts, tenant-owned deployments or operator accounts
belonged to Stockholm. No shared models, GPU pools, other tenants, Rene workbench
or shared legacy LibreChat endpoint were deleted or reconfigured.

## What was finished and verified

- Added the missing admin tenant-retirement API and migration0034. It refuses
  active work or owned cloud resources and requires archive checksum and expected
  identity counts. Historical operation/usage relationships remain intact.
- Deployed through Helm199, backend source `27de3311e`, index `803227d1...`,
  chart source `b4a64d1d3`; predecessor index `8cda3019...`/source `4f6fbc30d`
  is included in ancestry. Starter examples, SAM2/video changes and live Helm
  settings are preserved. Admin image is unchanged. Exact identities:
  [machine-readable receipt](receipt.json).
- A separate disposable tenant completed actual public Qwen inference with a
  normal inference key, then was retired. Its old key returned HTTP401. The
  first test used an inadequate32-token reasoning budget and produced no final
  answer; that failed semantic check is retained. The corrected published
  `/no_think`,1024-completion-token recipe returned `READY`. Its observed cold
  start was118.387seconds; this is not a fast-start performance qualification.
- Stockholm's user/key APIs are empty; all22 retained tokens are revoked and
  unresolvable by the authentication query. All140 original retained operation
  IDs, statuses and model IDs compare equal, and149 terminal facts remain.
- Three **naturally scheduled** maintenance runs on the final image completed at
  06:19:15,06:20:15 and06:21:14UTC. Gateway3/3, controller2/2, admin2/2 Ready;
  public `/readyz` and `/admin/` returned200.
- Focused PostgreSQL/users/retention/schema tests:38 passed; overlapping affected
  users/MCP/packaging tests:54 passed; chart tests:137 passed; current generic MCP
  control regressions:83 passed. These are not independent customer scenarios.
  A pre-existing Starlette deprecation warning remains. Initial missing test
  fixture fields and chart schema pins were corrected before deployment.

The old request-envelope, maintenance and truthful-accounting implementation is
deployed. September17's two bounded release146 SDK/HTTP cohorts remain valid only
at that historical scope:26 protein operations, two ESMFold2 batches, concurrency
five and six checked artifact downloads. They do not qualify today's complete
LibreChat/catalog experience. No all-model or next-event-ready claim is made.

## Evidence preserved

Protected archive:
`/home/tux/secure-handoff/stockholm-closeout-20260920-verified`.
Manifest SHA256:
`ad61b851f9e838bab31865f08a5779a451625d0d0841149971f65437a77df185`.
410275 JSON records were decompressed/parsed, and78 files hashed, including the
earlier release146 acceptance receipts. The export includes403037 debug records,
readable ledger projections and related lifecycle/artifact metadata. Full ledger
columns denied to the runtime role were not exported; privileges were not widened.

The first export stopped at the ledger's column-level access restriction and is
retained as an incomplete attempt. The second exports the allowed column
projection and is the verified archive used for retirement. Captured payloads
and credentials are private and are **not** in this repository. The archive is
not a turnkey database restore; account recovery requires an explicit operator
decision. There is no automatic undo or reactivation of old keys.

The independent post-deletion receipt and comparison are in
`/home/tux/secure-handoff/stockholm-closeout-retirement-20260920/`.
The deleted-account canary receipts are in
`/home/tux/secure-handoff/stockholm-closeout-canary-20260920/`.
The source scripts in this directory reproduce export, aggregate analysis,
guarded rollout, explicit retirement and independent verification.

## Lessons and remaining work

| Insight | Evidence and next action |
| --- | --- |
| Tests must use the participant's actual client and policy | Internal/raw-client success missed the event's generic envelope. Complete the fresh-event actual-LibreChat gate; never substitute SDK evidence. Keep one client instance per user. |
| HTTP success is not model success | Retained September11 metadata still shows OpenFold2 upstream400 twice and Boltz2 upstream422 once alongside MCP200 invocations. Current normalization has regression coverage. Follow operations to semantic results. |
| Transport traffic distorts raw request totals |399146 of403037 retained exchanges were GET `/mcp` HTTP200, including after the event. Investigate reconnect/idle-client behavior and separate it from inference usage; do not silently discard debugging logs. |
| Rules without a receiver notify nobody | All13 rules evaluate, but Alertmanager has only receiver `null`. A destination and delivered controlled test are still needed. Three certificate alerts fire; two lifecycle alerts moved from firing to pending after rollout, not proof of resolution. |
| Allocation budgets are not measured GPU use | Explicit accounting quality/unknowns are deployed. Later phase and DiffDock attribution improvements exist, but fleet-wide shared-serving attribution is still incomplete. No fabricated billing total or historical Pod attribution. |
| Export before retention erases the event cohort | The current140 operations include six retained event Cosmos operations and134 remediation-canary operations, including uploads. Their all-success status is survivor/remediation evidence, not the original event success rate. Original terminal accounting is incomplete even though request logs survive. |
| Close event credentials deliberately |20 team keys remained active without expiry until this closeout. Use the existing expiry setting when agreed with the event owner, then export, drain and retire. Do not leave stale clients authorized indefinitely. |
| Use qualified examples and measure waiting | The short Qwen test budget was unsuitable; the corrected seeded recipe passed. Its118-second cold start remains a participant-experience issue for future-event qualification, not hidden as pure model execution time. |

Follow-up Task Deck cards:

- `fs2-event-actual-client-qualification-r20260920`: ready; actual-client/full
  capability coverage and visible queue/cold-start behavior.
- `fs2-event-alert-delivery-r20260920`: blocked on the chosen operator destination.
- `fs2-event-mcp-idle-transport-r20260920`: ready; explain/reduce idle transport
  activity without hiding requests or breaking reconnect/resume.
- `fs2-stockholm-usage-accounting-reconciliation-r20260915`: ready; continue only
  the remaining fleet attribution/coverage work, reusing existing improvements.

The old Stockholm parent and superseded child cards are archived with explicit
dispositions rather than falsely marked fully qualified. No new workers are
running. Operational guidance is in [the event runbook](../../docs/event-closeout.md)
and linked from the mandatory customer release policy.

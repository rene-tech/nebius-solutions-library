# Customer workbenches — 2 October 2026

## Scope and current verdict

Customer overview and Serverless-only LibreChat lifecycle implementation. This is
not a new model qualification, cluster deployment, tenant/bucket cleanup verdict,
or claim that every legacy client has been migrated. Existing customer resources
remain unchanged. **LynxKite is explicitly held and untouched.**

Admin: `https://89.169.99.188/admin/customers`.
Project: `project-e00rene`, region `eu-north1`.
Cluster: `mk8scluster-e00j5z9te7x5dd9g6a`, namespace `fs2-system`.
Context: `nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a`.

The live replacement, failed-successor recovery and one-shot restart paths passed.
Managed client distribution is private; public anonymous
self-service is not claimed or enabled.

## Source custody and retained release

Backend work started from **the deployed** `697f8f5aac09f4d60b0a11d718903bd3e60f746e`,
not stale `origin/main`. At inspection those histories diverged by 9/706 commits.
No unrelated source was merged or the dirty canonical checkout overwritten.
Source is saved on `agent/fs2-customer-workbenches-20261002` in
`rene-tech/nebius-solutions-library`. Integration to main needs the existing live
history reconciled, not a blind force-push or replacement with main.

| Component | Exact source | Registry digest |
|---|---|---|
| API | `d7d6e8b44926cf177d2a72923b6e73db370f5178` | `sha256:f8e30440c7296e7d0d36ffd8b6763b06109b05722371a77b757a3f8eea82f053` |
| Admin | `f9e1f66664e3f99352c85df5384ac4c73b09a468` | `sha256:de4df56d87fe141bdc456cc6bee8309e6a2abb21cdc92e13cba3400a3438366f` |
| Client candidate | `4612a2b` in `rene-tech/serverless-ai-cookbook` | `sha256:873be148673dec6dc191ba30d2933c01acee4622fd3fa14888388378539f13d3` |
| Retained controller + schema metadata only | Original controller runtime unchanged | `sha256:fb6d32098a2e32853aaac1eb5a6ee117b3212e06ad357aa790668f5450a18179` |

Registry prefix: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh`.
API/controller repository: `fs2-platform/fs2-serve-control-plane`.
Admin repository: `fs2-platform/admin-console`. Client repository: `lc`.
Kimi K3/high, scientific skills `2026.10.01.6`, model tools and permissions unchanged.

Schema migration `0037_customer_workbenches.sql` completed at 14:57:28 UTC.
The model controller/maintenance/init schema manifest was advanced without replacing
its existing runtime with the older API source. No model replicas, node capacity,
Gateway, certificate or API-key changes. Scoped patches are retained here because
a full Helm apply from stale source would overwrite unrelated live changes.
Reconcile these values into the retained Helm release before its next full apply;
the patches are evidence, not a replacement for portable chart configuration.

Admin source tree: `14a234204b9a0136eb5313914878e8cbc1a995d4`.
Admin CycloneDX SBOM SHA-256:
`c7463e5630d5423d710572c73964cd3d740ca4c885703ea0b59acbcf7edf48a7`.
Both new image builds retain registry SBOM/provenance attestations.

## Qualification evidence

| Check | Result |
|---|---|
| Backend workbench, real PostgreSQL and users | 29 passed, final targeted run |
| Broader backend/storage/schema regression | 47 passed earlier in this change |
| Admin customer/navigation tests + typecheck | 9 passed; real HTTP serializer covered |
| Explicit Helm lifecycle values | Passed |
| Broader Helm subset | 143 passed, 1 pre-existing failure: nodes RBAC test expects `get`, baseline already has `get,list`; unrelated RBAC not changed |
| Client state/deploy/preview/scientist/deploy-link tests | 56 passed |
| Seed merge | 3 passed |
| Actual image Python 3.11 restore | Passed; unsupported tar extraction `filter` was fixed before candidate R3 |
| Second writer on mounted state filesystem | Rejected by filesystem lock |
| First real replacement | 14/14 state/login/artifact checks passed |
| Unchanged R3 replacement through admin button | 14/14 passed; operation resumed across API worker rollout, no duplicate successor |
| Fresh public chat on replaced instance | 6.098 s, Kimi K3/high retained, execution tool wrote byte-correct workspace artifact |
| Browser customer overview | Real Lynx model usage, storage, held client, Users links and unavailable-state handling observed |
| Browser customer profile save | Succeeded, same system metadata retained |
| Failure recovery | Automatic rollback completed 16:13:48 UTC; all 15 state checks passed, including the preceding real chat; changed attachment restored and synthetic failed-upgrade file removed |
| Fresh public chat after rollback | 7.663 s; tool wrote byte-correct bucket artifact; authenticated hosted model discovery passed |
| Restart of recovered container | All 16 checks passed, including the chat created after rollback; no snapshot replay/data rewind |

Fixture includes two local logins; exact original owner/password and encryption
key hashes; two conversations/messages; modified agent instructions; a custom
preset; encrypted plugin credential; uploaded attachment; bucket file; actual
public login and history reads. Hashes and credentials stay private. A subsequent
real tool-using chat and its workspace artifact must also survive rollback.

Found and fixed: SDK `deepcopy` corrupts protobuf extensions (native constructor
now tested); live-window query keys abandoned slow queries on each clock tick;
admin forms double-serialized JSON (now tested through `fetch`). The first SDK
failure stopped before cloud mutation and is retained, not counted as a pass.
The first fresh-chat harness used a non-browser User-Agent and received an SSE
error; replay with the actual browser request contract succeeded. This was not
silently counted as a clean first attempt.

## Task-owned resources and operation IDs

One CPU D3 4 vCPU/16 GiB QA workbench; no GPU, new tenant, bucket or inference key.
Filesystem: `computefilesystem-e00gqq5jjwr47xn4b4`, 32 GiB NETWORK_SSD.
Existing bucket: `fs2-system-35f1ad07f2fe32cf`.
Separate study owner: `system-state-qualification-20261002`.
Binding: `cf7cb511-1e24-4238-8029-b7068ffa9bfe`.

| Operation | Outcome / endpoint |
|---|---|
| `5341b372-b741-4606-9ef4-89f0fd38728e` | Preflight SDK failure; source untouched |
| `b85b4972-cb66-45ee-b08b-4ba44514c546` | 15:31:57–15:39:38 UTC; `aiendpoint-e00emepynsymjnwj73` → `aiendpoint-e00jmv74erzn03v2tv` |
| `651a7bd7-b6cc-404a-8991-2de8e8e5d0f6` | 15:45:58–15:53:36 UTC; → `aiendpoint-e00z1zjpc15rz6r956` |
| `891d01b4-6bf3-4a44-94a9-64b6865ff938` | 15:57:47–16:13:48 UTC; deliberately unhealthy `aiendpoint-e00tmqfnmwn7h2a9zr` rolled back automatically to `aiendpoint-e00b7bgf52mxk5ahhw` |

The QA-only unhealthy image is
`lc@sha256:200909191b99f7fc57e634da4a18dc43c86ac62db85cb74b794e4ad1a4339c42`.
It takes the normal state snapshot then sleeps without starting MongoDB or HTTP.
Never promote it; it has been removed from the release selection. Only R3 remains.
The 2,900,597-byte pre-upgrade snapshot was verified before changing the synthetic
attachment. Rollback restored the original bytes, removed the added canary,
retained displaced state, and survived a worker rollout during recovery.

Lynx hold: `aiendpoint-e00a5xqzhy0d3zjg56`; bucket `fs2-lynx-c327dcc386444425`.
It remains RUNNING on original client `e96a6650…`, with both customer logins and
their shared key unchanged. Rene, Kopra, demo and existing QA endpoints were only
registered as metadata; local-disk clients require migration before replacement.
No customer or ambiguous unassigned resource was stopped/deleted.

After qualification, the four stopped task-owned endpoint records listed above
(`e00emepynsymjnwj73`, `e00jmv74erzn03v2tv`, `e00z1zjpc15rz6r956`,
`e00tmqfnmwn7h2a9zr`) were deleted and all four get requests confirmed NotFound.
The independently created QA filesystem remains READY, with test data and snapshots
recoverable. Final QA endpoint `aiendpoint-e00b7bgf52mxk5ahhw` is STOPPED; no QA CPU
capacity remains allocated by this task. Its binding remains visible for repeatable
future tests. Existing customer and older independent QA clients remain untouched.

## Rollback and operational limits

Previous API: `sha256:e0906d1765cf2c14503ce3459cee5407730e3749308f952a1e14e52c50521128`.
Previous admin: `sha256:d0c75f341328ff06eaae841493039cbd0b29ed251dfc3affdecc686dc7c1b3b4`.
Previous controller: `sha256:3840f45bf49e5736da009820f374da58897ca94b242fc62a3eb5a3f6121a78fb`.
Prepared API rollback retaining the additive schema contract:
`sha256:cabc855c7cff64c6a34afceaa758e69c7c007d9174f52246337c3cabc9729b2c`.
Do not delete migration rows or customer state for rollback.

Observed full runtime replacement took about 7m40s, including image pull and
provisioning; the tunnel URL changes. Not zero downtime. Finish active chat turns
before replacing. Completed backend scientific jobs remain durable independently.
Filesystem snapshots are not off-filesystem disaster recovery. Self-service image
publication and legacy client data migration remain explicit follow-up work.

# Scientific AI → Serverless 2.0: implementation handoff

Updated 12 October 2026. **The isolated scientific-ai2 fleet now hosts the
internal GROMACS REST/MCP pilot. No customer cutover.** Current release,
automatic cross-GPU recovery, source defects and live acceptance are recorded in
[NEW-FLEET-RESULTS.md](../acceptance/serverless2-gromacs-20261011/NEW-FLEET-RESULTS.md).
The earlier shared-fleet evidence below is historical, not the deployment target.
All existing platform
features and routes remain in service on the current implementation. This
migration must not interrupt them.

11 October historical update: API image r10 / GPU workers r6 add pod-bound storage authorization
and reject unsupported resume options instead of silently ignoring them. Shared
Serverless API 0.11.20 fixes private-input delivery during fallback/resume and
cleanup across continuation chains. r10 also treats an authenticated worker's
startup-status propagation delay as retryable, without granting early access.
Read the current
[follow-up receipt](../acceptance/serverless2-gromacs-20261011/FOLLOW-UP.md)
alongside the historical r5 results below. No customer release is implied.

## Branch isolation and disposition

Owner: Rene's Scientific AI migration workstream. Active development is on the
single temporary branch `scientific-ai-serverless2-migration` in the personal
fork, using `/home/tux/worktrees/scientific-ai-s2-gromacs-20261011`.
Do not integrate this unfinished migration into `main` at routine turn closeout.

The initial application commit `66c04af8337051ea81f9952df1034be7fc6ff637` was
prematurely pushed to `main`. History-preserving revert
`f3d651e0c9ad9dd726c26fc46d82e050105e25c1` restores its complete tree to
`de8a2a18c8fea3da73c1905883a8cb8225119ecd`. The feature branch incorporates that
revert and reapplies the migration as `1e6b28bacb2830bb2b464d714429b6b7230ae0f8`,
so it remains normally mergeable; the reapplied tree matches the tested original.
No history rewrite, customer deployment, or cloud resource change was performed.

Keep production fixes on the existing implementation. Test the migration only
on the new isolated scientific-ai2 fleet; leave shared s2pr2 unchanged.
Integration requires the real S2 bindings, agreed
RTX-to-L40S recovery and storage-isolation acceptance, passing release checks,
and an explicit integration decision. Production cutover is a separate decision.
Retire this one branch after accepted integration, not before.

The initial GitHub validation run
[38136271343](https://github.com/rene-tech/nebius-solutions-library/actions/runs/38136271343)
passed the new offline application job but failed the broader static-validation
job, including missing `pytest` and `cryptography` dependencies. Full CI remains
an open integration gate; local component results do not replace it.

## Agreed pilot

Start a **new GROMACS job using synthetic data**, rather than copying a customer
checkpoint. Prefer one RTX PRO 6000 Blackwell; fall back to L40S within the
operator's placement policy. Select the GPU-specific optimized image at actual
placement. Keep checkpoints/results in the approved customer bucket and scope
storage access to the request/attempt. Resume native checkpoints after abrupt
spot loss, including on another qualified GPU and cluster without the old PVC.
Current target: the isolated `scientific-ai2` fleet and existing `system/qa`
identity. Shared `s2pr2` was the initial pilot; do not modify it for this phase.

The S2 developer handoff is maintained locally at
`/home/tux/nebius-serverless-2/docs/dev-fleet/SCIENTIFIC-AI-EXECUTION-REQUIREMENTS-20261011.md`.
Its five requirements are agreed; integration is owned by the Serverless team.
The runtime source is the Solutions Library Serverless PR, not the historical
example GROMACS catalog in the Serverless notes repository.

The delivered S2 implementation has now been inspected and its live discovery
verified. The original [integration blockers](S2-INTEGRATION-GAPS-20261011.md)
were fixed in API 0.11.11 / jobs 0.1.14; all three application probes pass against
source `56aaa9eb3`. The application now has a typed MCP/REST gateway, a fenced
file-lifecycle bridge and request-scoped S3 URL broker. Exact native SM89/SM120
GROMACS 2026.4 images are built, pinned and deployed. Two unchanged-application-release REST/MCP
cohorts completed, and an abruptly lost RTX run resumed on L40S without its old
scratch disk: 2,000,000 native steps, 22 verified files and 2,001 continuous frames.
See the [live acceptance report](../acceptance/serverless2-gromacs-20261011/LIVE-PILOT.md)
for timings and limits. This is explicit API resume, not automatic cross-class
failover or broad customer/PHI qualification.

The shared S2 API moved from 0.11.11 to 0.11.12/0.11.13 under other workstreams
during the test. The report separates this from unchanged Scientific AI image
qualification; the three probes also pass against final S2 source `fdcbf7785`.
Two additional REST/MCP cohorts passed on the final 0.11.13 API image.

## Implemented on the application side

[components/scientific-ai](../components/scientific-ai/README.md) is the new small
application package. It reuses the current GROMACS scientific worker and adds:

- private run manifests, job-bound grants, pinned runtime/image validation;
- opaque S2 submissions plus authoritative status/cancel passthrough;
- per-job AWS SDK web-identity refresh with no broad credential fallback;
- streaming customer-object transfers and verified manifest-last checkpoints;
- native recovery into a new workspace, with compatibility distinct from image identity;
- export-only recovery, including recovery after a lost finalization callback.
- existing scientific artifact/input envelopes, UUID operation handles and typed
  GROMACS tools over the same application methods;
- a 60-second, operation-prefix-bound S3 URL broker that verifies the active S2
  attempt and signed projected pod identity; GPU pods receive no tenant storage
  secret. Offline JWT verification does not prove immediate Pod deletion;
- a small isolated Terraform root for the pilot API, without a second scheduler.

The result envelope explicitly uses `scientific-run-result/v2`: legacy v1's
controller-specific admission UIDs are not fabricated. Output/input artifact
manifests remain v1-compatible. The first pilot handles one native job per
submission, not legacy batch fan-out or MPI. Those existing features are still
served by the old implementation and must be migrated before a full cutover.

Only optional constructor/hooks were added to `fs2_gromacs.worker.Workflow`.
Existing CLI, checkpoint hashes, environment behavior and local/companion paths
keep their defaults. No customer image is rebuilt or deployed by source integration.

## Gates after the successful internal execution pilot

| S2 requirement | Scientific AI integration action / acceptance |
| --- | --- |
| S2-01 identity/private bindings | Projected pod JWT authorization is implemented alongside the online attempt check. Qualify external IAM, token renewal/revocation and protected-data handling. No hardware attestation or immediate Pod-deletion guarantee is claimed. |
| S2-02 application I/O | GPU pods use scoped object URLs, not tenant S3 keys. The trusted API holds the internal test bucket credential. Real external customer storage, large multipart artifacts and protected-data cleanup still need qualification. |
| S2-03 durable attempt lifecycle | The authenticated file bridge, live checkpoints, explicit cancellation and same-key resume were exercised. Partitioned stale-worker and lost-ack live drills remain. Do not build a second scheduler/operation ledger. |
| S2-04 placement/recovery | Exact SM120/SM89 builds and fresh-PVC cross-cluster native recovery passed. Automatic cross-class recovery after a placed worker loses capacity remains an S2 gate. Startup/network-volume attachment and realistic PME/protein throughput need measurement/optimization. |
| S2-05 finalization/cleanup | Completed-result recovery ran no new engine commands. Denied final storage, missing/lost final acknowledgments, scratch protection and failure-time cleanup still require live drills. |

The initial shared S2 handoff listed RTX/L40S pools in us-central1/eu-north1.
Current bounds/regions are in
[scientific-ai2/README.md](../deployments/scientific-ai2/README.md): RTX in
eu-south1, L40S in eu-north1. Check live capacity; do not substitute H100
for an RTX/L40S acceptance gate.
Do not reprovision current Scientific AI customer nodes for migration testing.

After closing these gates, migrate the remaining REST/MCP operation behavior,
batch fan-out, tenant access, usage/metrics, admin management and subsequent Apps
incrementally. Preserve each old path until its replacement passes the current
[customer release policy](../CUSTOMER_RELEASE_POLICY.md); customer-facing changes
are allowed where needed, but feature removal is not.

## Evidence and limitations

Component tests and the fresh real-engine local recovery test are recorded in
[the dated evidence](../acceptance/serverless2-gromacs-20261011/README.md).
The current offline suite is 123 passing tests; AWS federation still uses SDK
stubs. The deployed internal pilot uses the existing demo workspace under an
owned run prefix, not an AWS customer role. This does not qualify AWS federation,
HIPAA, physical scratch erasure or unrestricted production use. Previously
signed S3 URLs can remain valid for up to 60 seconds after cancellation. No
production key, bucket, cluster, DNS or customer job is changed by this pilot.

The deployed pilot is `https://api.195.242.30.226.sslip.io/scientific-ai` (REST
under `/v1`, MCP under `/mcp`), authenticated with existing internal S2 keys.
Runtime source is `73414081c`; immutable image digests, operation IDs, observed
costs, retention, exact failure history and reproduction are in the live report.
Terraform owns only the pilot's five application resources; the final plan has
no drift. Leave customer routes and `forge.nebius.cloud` unchanged.

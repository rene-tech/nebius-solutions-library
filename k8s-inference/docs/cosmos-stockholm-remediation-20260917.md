# Cosmos and Stockholm remediation — 17 September 2026

Release status: **not deployed; not customer-qualified**. The implementation is
being integrated on `agent/fs2-cosmos-stockholm-remediation-r20260917`, based on
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. This baseline preserves the newer
speech, tenant storage, and workshop APIs. The old dirty `main` checkout was not
reset or overwritten. This document is not a replacement for live evidence.

## What changed

| Area | Implementation | Remaining live proof |
| --- | --- | --- |
| Stockholm MCP | Normalize misplaced generic gateway controls before SDK defaults; reject conflicting copies before admission; keep controls out of runtime payloads | Exact public named/generic OpenFold2 and Boltz2 results through the installed LibreChat/skills client |
| Cosmos media | Five distinct typed tools, mode-specific validation, HTTPS/artifact inputs, binary artifact outputs, V2V and transfer runtime adapter | Publish integrated adapter and CP; Timothy's MP4 URL and upload flows with decoded, changed MP4 output |
| LeRobot | Pinned v3 reader/writer, action/state preservation, scoped parent-to-child Cosmos calls, migration 0032, cancellation/attempt fencing | Publish CPU image and qualified catalog/execution binding; two augmentation dimensions, real Cosmos output, dataset reload |
| Outcomes | Transport status separated from semantic/tool result; durable terminal failures exported by tenant/model/protocol/workload/error class; admin request logs and readiness evidence | Real scrape/alert/admin correlation and failed-versus-successful customer operation proof |
| Usage | Conservative admission budget explicitly named; recorded occupancy, classified idle, startup and unknown separated; read-only historical export | Stockholm historical export, coordinated CP/admin rollout and customer observations |
| Operations | Bounded dependency-readiness checks; retention preserves referenced/in-flight scientific operations and restricted-role cleanup works | Verify against actual cluster/database without deleting customer evidence |
| Acceptance | Cosmos executable public runner; capability-level evidence expiration; Stockholm exact-release verifier | Actual client traces, representative fixtures for every advertised App, two unchanged public cohorts |

Forward/inverse dynamics are **not** advertised: the prior pinned H100 preview
failed those modes. LeRobot augmentation preserves the original action trajectory;
it does not invent replacement robot actions. The existing local H100 media and
snapshot receipts are dated 15 September and do not qualify this integrated build.
See [Cosmos media evidence](../models/general-media/evidence/cosmos3-nano-media-qualification-20260915.md)
and the [LeRobot contract](../models/general-media/lerobot-augmentation/README.md).

## Outcome metrics and diagnosis

`fs2_serve_customer_operations_total` is a gauge projected from immutable terminal
usage facts, not an in-process incrementing counter. Deleting retained operation
details does not remove its terminal failure; the unavailable error class becomes
`unknown`. `fs2_serve_customer_operations_last_10m` projects the last ten minutes.
Both use bounded tenant/model/protocol/outcome/workload/error-class dimensions,
not request IDs, principals, keys, or payloads. Error classes are enumerated.

All gateway replicas read the same database. The new alert and Grafana queries
deduplicate by those dimensions before summing. More than 10% failed/expired/
preempted terminal operations out of at least five non-cancelled completions in
ten minutes, sustained for two minutes, raises `Fs2ServeCustomerOperationFailureRate`.
An HTTP 200 does not affect this calculation. Cancelled work is shown but excluded
from the failure-rate denominator. The public semantic-failure alert separately
covers tool errors and timeouts, including failures before a durable operation.

On an alert, open Admin → Apps → Runs and filter the tenant/model; follow the
operation ID to attempts, Request log, Containers and App Logs. Compare terminal
result, upstream error, retry count and lifecycle phases. Preserve the original
failure before any retry. A successful admission is not a successful inference.
The new terminal-outcome Grafana panel retains tenant, model, outcome and error
class. Existing queue/current-state and readiness/deployment panels remain.

The Prometheus regression runs the real `promtool` evaluator. It proves that two
replicas projecting four failures do not become eight and cross the five-operation
threshold, and that five failures plus five successes alert even alongside a
fully successful HTTP transport series. A separate real-PostgreSQL test proves
terminal facts survive missing operation detail. These are local tests, not
evidence that the production scraper or alert receiver saw this release.

## Deployment block and exact resume sequence

The authorized kubeconfig for `project-e00rene`, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`, region `eu-north1`, context `fs2-storage-h100`,
is rejected with `JwtKeyNotExists`. The configured public key ID is
`publickey-i00xt9sw5qagz124ja`. A working authorized profile/kubeconfig must be
restored by the owner. No quota increase, replacement identity, unrelated project
credentials, or production deployment was attempted to bypass this.

The last documented CP release is Helm 138 / image
`sha256:d0b3b02e201d8dc34ca7368e8ad34fece02e15841595de8bb970748c3dc300d6`
from source `1f745bf18`. **Re-read the actual deployment before changing it**;
this historical receipt cannot prove that no later release occurred.

After access is restored:

1. Read actual CP/admin/runtime images, release values, database contract,
   public discovery, customer grants and installed LibreChat/skill build. Retain
   predecessor digests and current working speech/storage/workshop checks.
2. Build/publish CP, admin and LeRobot images from the final integrated commit;
   retain digests. Apply additive migration 0032 and verify its release contract.
   Update CP and admin together because usage-response names changed. Build/publish
   the Cosmos adapter/template containing this source; do not change pinned model,
   upstream runtime, quality controls, offload or quantization silently.
3. Bind the new LeRobot CPU image and scoped internal API through the existing
   scientific execution map and catalog promotion path. Do not mark the candidate
   profile qualified merely because the image builds. It needs the real media
   workflow, artifact publication and reader-reopen checks.
4. Deploy using the normal tracked Helm/Terraform release path. Observe readiness,
   logs, DB migration, metrics, existing speech/storage and model regression paths.
   No Terraform apply or managed-resource drift claim was made in this session.
5. Run the [Cosmos customer runner](../acceptance/cosmos3-customer-20260915/README.md)
   and [Stockholm release verifier](../acceptance/stockholm-customer-20260917/README.md)
   with exact same-policy canaries, real client traces and representative workload
   fixtures. The latter does not create missing per-model live fixtures for you.
   Use [CUSTOMER_RELEASE_POLICY.md](../CUSTOMER_RELEASE_POLICY.md), not a smoke
   shortcut. Do not broaden a real customer key silently.
6. Read-only export the actual Stockholm usage cohort using
   [the reconciliation CLI](stockholm-usage-accounting-20260917.md). Observe real
   Prometheus, alerts and admin pages for the same operations. Preserve failed,
   skipped or uncovered capabilities. Require two consecutive unchanged cohorts
   before saying the customer workflows are ready.
7. Remove/revoke only explicitly created canary resources and retain the evidence.
   Do not delete existing customer workloads, tokens, artifacts or logs.

## Local resources and evidence

No cloud capacity, tenant keys, models or workloads were changed during this work.
Task-owned local PostgreSQL 16 container `fs2-stockholm-retention-pg-20260917`
listens only on `127.0.0.1:33474`; isolated databases `stockholm_retention`,
`cosmos_delegation`, `mindguard_usage` and `semantic_outcomes` hold synthetic
fixtures. The container is retained for repeatable diagnosis. No old PostgreSQL
socket/temp directories or other agents' resources were deleted.

Detailed component evidence and known negative attempts:

- [MCP envelope implementation](stockholm-mcp-envelope-remediation-20260917.md).
- [Usage implementation and local suites](stockholm-usage-accounting-20260917.md).
- [LeRobot runtime and delegation](../models/general-media/lerobot-augmentation/README.md).
- [Customer capability gate](../acceptance/customer-readiness/capability_gate.py).

Final local integration test totals and exact limitations are recorded in the
[verification summary](cosmos-stockholm-local-verification-20260917.md).
Task cards remain blocked/review,
not done, while deployment and customer-shaped evidence are missing.

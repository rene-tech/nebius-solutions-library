# Cosmos and Stockholm remediation — 17 September 2026

Release status: **Helm 145 bounded tests recorded; upload-demand correction
rolling out as Helm 146; not customer-qualified**.
The implementation is integrated on `agent/fs2-cosmos-stockholm-remediation-r20260917`, based on
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. This baseline preserves the newer
speech, tenant storage, and workshop APIs. The old dirty `main` checkout was not
reset or overwritten. This document is not a replacement for live evidence.

## Latest deployed state

The explicit `sandbox2` profile restored authorized access. The original global
profile is unchanged. Helm release 145 deploys the corrected CP/controller
`sha256:849020eabbcf07d07112bcebae4032639e8e995a9eda078410ccf4879a188ee8`
and admin `sha256:6428b3500d2dd6784c0ff2308335b3d2962f725434cc5e7ea8fd5098dbf21659`.
The complete additive model contract retains 20 model identities and all 22 old
template revisions, adding only the new Cosmos template. It does not drop the
five live speech models absent from the older Terraform state.

The owner API drained Cosmos and applied template
`cosmos3-nano.stockholm-v2`, digest
`sha256:a4c96a343622a0e809e61f132c71032dd6e845c9ea7d846563cdb0ce36cf0fab`.
Generation 16 was observed `Cold`, with lifecycle restored to `Enabled`.
Minimum 0, maximum 4, idle/cooldown 30 seconds and all other original settings
were preserved. Runtime image, model revision and r7 GPU snapshot are unchanged.
The isolated snapshot preview and its ConfigMap/port-forward were removed;
media and receipts remain protected. The LeRobot CPU image is published, but its
App remains unrouted and is not claimed ready.

Gateway 3/3, admin 2/2 and controller 2/2 were ready on release 145 at 15:29 UTC.
Nine public/admin read APIs returned HTTP 200 at 15:30:38 UTC and retained 34 Apps.
These are availability checks, not inference qualification. The release-145
readback at 15:29:56 UTC confirmed all three metrics endpoints HTTP 200 and
actually scraped by Prometheus; all 13 rules had healthy evaluation. Existing
two lifecycle and three certificate alerts remain open. GPU observer coverage
was 15/15 Ready but only 12/15 updated at that instant; that is not proof of
homogeneous observer versions or exact per-request GPU attribution. The receipt is
`final-deployment-verification-r145.json` under the protected rollout directory,
SHA-256 `8062cf90707b03e3d98ae19f88044002eea986f2cde0d995313945243de31c95`.

Helm 141's global watcher waited on unchanged GPU observers on unavailable nodes
after the application rollout had finished. Only the task-owned local Helm wait
was interrupted. Forward release 142 used `--wait=hookOnly` with the exact same
desired resources and explicit application/controller/metrics verification.
The failed/cancelled 141 receipt is retained; no rollback or further node removal
was used to make it appear successful.

Source and reproducible release helpers are retained on the integration branch.
The upload-demand correction is source `bce48ba034675eaf17bec473e3990fd4003185f2`.
See the [complete contract handoff](../acceptance/cosmos-stockholm-deployment-20260917/CONTRACT-HANDOFF.md)
for hashes, preservation proof, owner API cutover and rollback ordering.
The final bounded public cohorts are separate from these deployment checks.

### Public Cosmos failure found after rollout

The first real cold operation `bb49f4b9-7226-4d83-903f-668383b9a0b6` failed with
`runtime_protocol_error` / HTTP 502 on release 142. Its normal autoscaler added
preemptible capacity; the r7 snapshot restored, and the adapter generated HTTP
200 MP4 responses on all three built-in attempts. `RuntimeClient` then tried to
JSON-decode the binary result before artifact externalization. This is a gateway
defect, not a passing video workflow; subsequent public media submissions were
paused while the narrow fix and regression tests were prepared.

Recorded activation/cold time was 443.976 seconds, including the new-node path;
this failed operation is not a successful cold-start benchmark. No output
artifact or output-video decode passed. Its runtime identity fields were empty
because trusted placement observation occurs after semantic decoding. Do not
interpret the empty identity/zero GPU-count field as zero GPU work, or its
10,800 reserved GPU-seconds as measured consumption. The actual observed Pod,
node, GPU and restore/adapter logs are retained in the separate failure receipt.

Correction `c7f99b46fcd22f8ee3d63b8603715eb52a58ac4c` preserves validated native
Cosmos PNG/MP4 bytes until the existing artifact publisher stores them. Other
native/LLM JSON validation, Magpie WAV behavior, legacy Cosmos JSON responses,
size limits and usage handling remain unchanged. Structural validation is not a
visual-quality or full-decoder claim; public acceptance still downloads and
decodes the output. The focused regression suite passed 157 tests, including the
real operation worker and artifact service. The validator also accepted the
actual r7 GPU preview MP4/PNG files. Release 143 contains this correction; new
public cohorts started after all three gateways were Ready on its digest. The
failed release-142 operation remains in the evidence and is not overwritten.

### Release-143 compatibility result and next correction

The first fresh public V2V/transfer matrix passed all eight combinations of
HTTP/MCP, HTTPS/upload input and the two video modes, on attempt one. Every MP4
was downloaded, SHA/size checked and fully decoded; same-operation replay was
verified. Its cached-node preemptible H100 cold start was 38.023528 seconds,
41.930 seconds end to end, not a new-node provisioning benchmark. Numeric/data
integrity does not establish robot-action fidelity: generated motion/details
can still differ from the source.

An additional typed T2I check found a separate real HTTP 422: the gateway added
`output_delivery=inline-base64`, but the exact image request DTO forbids that
field. Operation `82a3903b-2e4f-4670-8bed-5159d4e8aff9` failed on attempt one
after a 300.254-second activation on a node which first pulled the runtime
image. The snapshot restored, but image generation was never accepted. This is
not a passing image workflow or a successful uncached-node latency benchmark.

Source `f8cbadb3bf14ad5ec157cf8f41466d95de645053` removes only that T2I fixed
default, retaining PNG format and the legacy JSON image response. All four video
modes keep artifact delivery. The actual adapter schemas for all five modes,
MCP T2I/V2V admission, legacy output/artifact and Magpie regressions passed in a
177-test focused suite. Runtime, snapshot, template, model, admin and schema are
unchanged. This typed-only correction was deployed as release 144; no customer
cohort was started on it because the same unsupported field was also discovered
in the generic named-tool schema.

Final source `ad819a0118b7fcd113ce68d3e597d475c0813582` also removes that field
from the generic text-to-image schema branch and clarifies that delivery options
apply to videos. All five generic branches are tested against the actual adapter
DTOs. Unsupported image delivery fields are rejected before admission by the
schema-aware named native tool; the arbitrary-payload `invoke_model` API remains
a pass-through and is not claimed to validate every model field. Legacy T2I
without the field and T2V inline/artifact behavior are preserved. The complete
focused suite passed 179 tests. Helm 145 deploys this final source; final-image
public compatibility and two bounded cohorts resumed only after all gateways
were on its immutable digest.

First final-image T2I operation `ee17634b-9e72-4771-bba0-8364337b3851` succeeded
on attempt one. Its externalized JSON/base64 result was downloaded and verified,
then decoded into a 196,993-byte 256x256 PNG. In-flight/terminal replay retained
the same operation. Cached-node cold activation was 37.839581 seconds and total
request time 40.245465 seconds; this is not an image-pull/new-node measurement.
Live adapter/runtime logs and Pod/events were archived before natural scale-down.

Stockholm's release-143 cohort one passed 13 protein calls with actual peak-five
outstanding operations, an ESMFold2 batch and all three artifact downloads, with
zero transport failures. Cohort two completed six protein calls before the
operator pause. One OpenFold2 call spent about 134 seconds in activation, then
completed inference in 1.7 seconds on attempt one. Its exact readiness-wait cause
is not established; Ready containers are not equivalent to a Ready Pod. These
are retained intermediate results, not the final unchanged-release pair or
actual hosted-LibreChat/full-catalog qualification.

### Release-145 results and CPU-upload scaling correction

Stockholm completed two unchanged release-145 bounded cohorts: 26 OpenFold2/
Boltz2 serving operations, two ESMFold2 batches, concurrency five in each cohort,
and six verified result downloads, with no transport failures. The exact 28
inference operations reconciled to usage. All 102 operations in that disposable
canary's history were terminal before its key was revoked at 15:49:21 UTC;
the public read then returned 401 and other key metadata was unchanged. Six
serving operations had unavailable per-request GPU attribution, not measured
zero use. These tests do not qualify hosted LibreChat or the remaining catalog.

Cosmos passed all four compatibility cases and the first eight-case video matrix
on release 145, including HTTP/MCP, HTTPS/upload inputs, V2V and transfer, replay,
artifact SHA/size and full decoding. The first matrix's cold activation was
290.032357 seconds, including a 170.519-second image pull on an **existing**
preemptible H100 node; it was not new-node provisioning. Its other seven calls
were hot. Cached-node compatibility T2I activation was 37.839581 seconds.
Neither figure is an all-input latency guarantee or proof of robot-action fidelity.

The second matrix stopped before admitting any generation: its input upload
unexpectedly activated Cosmos from zero. Read-only correlation proved that
`scientific-artifact-upload-v1` was included in `queue_counts()` and therefore
the GPU-demand gauge, even though uploads use CPU transport and serving workers
already exclude them. The upload ran 15:43:32.949–15:43:33.869 UTC; the overlapping
15:43:33.418 scrape produced a demand pulse, followed by two restoring Pods.
The exact intermediate HPA decisions were not reconstructed. No startup-retention
feedback-loop claim is made, and its formula/settings remain unchanged.

Source `bce48ba03` excludes only upload operations from that demand path in
PostgreSQL and memory. Upload operation views, debugging records, terminal
outcomes and usage are preserved; actual native/chat/batch demand still counts.
27 focused tests passed, including real PostgreSQL and unchanged startup-retention
tests. Published image is
`sha256:25438d07ec2caae07ae30209f6267d215b6a037b453aa9f0a3a5a76cb5b6e37e`.
The rendered 146 delta changes only 12 image references across the same 84
resources. It changes no model, schema, admin image, scaling policy or quota.
Live held-upload verification and fresh bounded cohorts are required on that
digest; successful 145 results are retained, not relabeled as 146 results.

## What changed

| Area | Implementation | Remaining live proof |
| --- | --- | --- |
| Stockholm MCP | Normalize misplaced generic gateway controls before SDK defaults; reject conflicting copies before admission; keep controls out of runtime payloads | Exact public named/generic OpenFold2 and Boltz2 results through the installed LibreChat/skills client |
| Cosmos media | Five distinct typed tools, mode-specific validation, HTTPS/artifact inputs, binary artifact outputs, V2V/transfer adapter and corrected gateway deployed | Complete public URL/upload HTTP/MCP matrix with decoded output; distinguish mechanics from robot-action fidelity |
| LeRobot | Pinned v3 reader/writer, published CPU image, action/state preservation, deployed scoped delegation/migration 0032 and cancellation/attempt fencing | Catalog/execution binding; actual public parent/child runs for two augmentation dimensions and published dataset reload |
| Outcomes | Durable semantic failure metrics, shared-replica deduplication, admin request logs and Prometheus rule selection deployed; actual scrapes/evaluation verified | Customer-run correlation and complete supported-client/model coverage; existing unrelated alerts remain open |
| Usage | Conservative admission budget explicitly named; occupancy/startup/unknown separated; historical and live read-only exports | Exact per-request serving GPU attribution with multiple Ready replicas is unavailable; historical event records are absent and cannot be reconstructed |
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

## Access restored and deployment resumed

The user selected `sandbox2` on 17 September. It authenticates and has authorized
access to `project-e00rene` and the intended cluster. The global default profile
is unchanged. The isolated kubeconfig is
`/home/tux/secure-handoff/cosmos-stockholm-sandbox2-20260917.kubeconfig`, context
`fs2-remediation-sandbox2`; do not commit its contents.

Source `b33d73149d5f8be96cac70711b17d039927cea2a` was built and published with
provenance. Migration 32 and the new gateway, admin and controller were deployed
through the existing Helm release, preserving its complete live configuration.
The nine supported public readiness/admin read APIs returned HTTP 200; the UI
lists 34 Apps. These checks do not qualify model inference.

Live validation found that the new terminal-operation metrics need five additional
read-only column grants on `fs2_usage_facts`. Source `d586613f1` corrected those
grants without granting table-wide reads or ledger writes. Helm revision 140
completed successfully with CP image `sha256:f96d890a19fb917322f5986d1ee81b1d5911643e35b62d76a46503c5f35654f8`.
All three gateway replicas returned `/metrics` HTTP 200 and Prometheus scraped
them successfully. The first image's HTTP 500 is retained as a failed release
attempt, not a passing observability check. The PrometheusRule also lacked the
installed Prometheus release selector; source `471f3fcf6` fixes chart/Terraform
labels. The live label was corrected and all 13 rules loaded with healthy
evaluation; Helm 142 now persists that correction.

On the corrected image, the first bounded Stockholm cohort passed named,
generic, legacy-nested and HTTP OpenFold2/Boltz2 results, idempotent replay and
five outstanding mixed operations. Its ESMFold2 batch reached a successful
terminal result, but the runner then failed during output-artifact readback.
This is not yet a complete clean cohort or a LibreChat qualification.

The GPU-observer rollouts were blocked by five stale node registrations across
two batches. All five corresponding Nebius VMs were independently confirmed `STOPPED`, their
kubelets were unresponsive, and only node/DaemonSet system pods remained.
Their Kubernetes registrations were removed on 17 September; no VM, node group,
quota, capacity setting, or customer application was deleted. The stopped VMs
remain intact and kubelets can re-register if restarted. Exact metadata is
retained privately in `cosmos-stockholm-rollout-20260917`.

The last canonical Terraform state predates the live storage/speech changes.
This scoped application release uses the repository Helm chart and a tracked
[release delta](../acceptance/cosmos-stockholm-deployment-20260917/release.values.yaml)
over retained live values. H100 `terraform.tfvars` image/provenance pins were
updated, but **no Terraform apply or zero-drift claim is made**. A later broad
Terraform apply must first reconcile the previously documented storage provisioner
state and retained overlays; do not create duplicate IAM resources.

Rollback caveat: the old 31-migration image rejects a 32-entry schema at startup.
The additive migration does not make a blind Helm rollback safe. Recover forward
with a verified 32-compatible image; do not remove migration ledger entries or
customer data.

## Historical deployment block and acceptance sequence

Before the explicit `sandbox2` selection, the authorized kubeconfig for `project-e00rene`, cluster
`mk8scluster-e00j5z9te7x5dd9g6a`, region `eu-north1`, context `fs2-storage-h100`,
was rejected with `JwtKeyNotExists`. The configured public key ID was
`publickey-i00xt9sw5qagz124ja`. This historical authentication block is resolved
through the user-selected profile. No quota increase, replacement identity, unrelated project
credentials, or production deployment was attempted to bypass this.

The pre-resume CP release was Helm 138 / image
`sha256:d0b3b02e201d8dc34ca7368e8ad34fece02e15841595de8bb970748c3dc300d6`
from source `1f745bf18`. **Re-read the actual deployment before changing it**;
this historical receipt cannot prove that no later release occurred.

Access, image publication, migration 32, coordinated CP/admin rollout, Cosmos
template promotion and real metrics scraping are complete. Do not repeat those
steps merely because they appear in the historical cards. Remaining work:

1. Finish and record the final-image public compatibility and bounded cohorts
   using the [Cosmos runner](../acceptance/cosmos3-customer-20260917/README.md)
   and [Stockholm runner](../acceptance/stockholm-customer-20260917/README.md).
   Retain every negative/interrupted intermediate attempt. Scope these results
   to the actual modes/models/clients tested, not the entire catalog.
2. Bind the published LeRobot CPU image and complete its scientific profile,
   execution identity and execution map through the existing promotion path.
   Then test real public parent-to-child delegation, both dataset augmentations,
   published artifacts, reader reopening, cancellation/priority and usage. The
   isolated reader round-trips do not establish this public workflow or physical
   action/video alignment; do not mark the candidate qualified from its build.
3. Complete actual hosted-LibreChat plus installed-skill testing with a same-policy
   caller. The current client uses a global server-managed MCP key and exposes no
   per-user canary binding. Do not replace that shared key or silently change all
   users' client configuration. The raw MCP SDK is not a substitute for this test.
4. Supply representative fixtures and public/client evidence for the remaining
   advertised Apps. The bounded Stockholm driver covers OpenFold2, Boltz2 and
   ESMFold2, not all non-Cosmos Apps. See
   [CUSTOMER_RELEASE_POLICY.md](../CUSTOMER_RELEASE_POLICY.md).
5. Preserve truthful usage limitations: missing historical event data cannot be
   reconstructed, and multiple Ready serving replicas do not give an exact
   request-to-GPU join. Keep unavailable measurements separate from zero, budgets
   and billable occupancy. Existing lifecycle/certificate alerts also remain open.
6. Reconcile the older Terraform state and retained live storage/speech overlays
   before a broad apply. The protected tfvars and release delta pin the new image,
   but no Terraform apply or zero-drift claim was made.
7. Revoke only explicitly created canaries after admitted work settles; preserve
   customer keys, jobs, artifacts and diagnostic evidence. Update the task cards
   and this record with precise results rather than declaring the parent done.

## Local resources and evidence

Before the sandbox2 resume, no cloud capacity, tenant keys, models or workloads
had been changed. The live resume changes and limitations are documented above;
only disposable acceptance principals may be created, not changes to real team keys.
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
Task cards remain running/review,
not done, while deployment and customer-shaped evidence are missing.

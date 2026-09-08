# Aging Apps and CPU-managed deployments — 2026-09-08

This extends the [Apps-first admin release](../admin-apps-20260908/RELEASE.md).
Clinical PhenoAge and AltumAge are independent Apps. The
[supplied Slack message](https://nebius.slack.com/archives/C0B0MSHSU8M/p1787878309466189?thread_ts=1787794882.336889&cid=C0B0MSHSU8M)
confirms blood-biomarker clinical PhenoAge, not DNAm PhenoAge. Their input domains
and prediction targets differ; this demo compares compute and latency, not
interchangeable accuracy.

## Target and immutable artifacts

Repository `rene-tech/nebius-solutions-library`, branch `main`. Target:
`project-e00rene`, `eu-north1`, cluster `mk8scluster-e00j5z9te7x5dd9g6a`, context
`k8s-inference-h100`. Public admin: <https://89.169.99.188/admin/>.

| Artifact | Source commit | SHA-256 digest |
| --- | --- | --- |
| Control plane | `c188ddaada3d433ecc6792a0fdadbd24b705408e` | `25c6b54cd803d4647f8909ed4f398d5326db8bcbf5cb696a354d167891f12c6c` |
| Admin console | `d00976744bd7cefb93896cac3cac8585ed8d6b0b` | `d474e818258ca0e78a3ec6765d3a035f7ded616a5f94d89ad4252aa23aa4988a` |
| AltumAge CUDA worker | `3a3dbe0fdb258579f9d4eb1b35a2d1525f5fc0d3` | `ca6352f79ecd78928e13ecda55afaf01a5df1923291b7a56a7ba9a9770c484e1` |
| Clinical PhenoAge CPU worker | `3a3dbe0fdb258579f9d4eb1b35a2d1525f5fc0d3` | `b6c28820576b62972437787bd06fa37f8c10b6f51de31b0f4da3cc3873bb34f1` |

Regional registry: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh`.
Platform images use `fs2-platform/fs2-serve-{control-plane,admin-console}`;
workers use `fs2-models/aging/{altumage-cuda,phenoage-cpu}`. All were built from
committed source, with retained build/provenance/publication receipts. The admin
package lock is unchanged; its CycloneDX SBOM hash is
`e789c9cbe6ec0c2d921c7ab8b76487e6aa68d207f8f41afd454af2887c00f8f3`.

## Architecture and scope

The existing Catalog, deployment controller, KEDA queue, durable operations,
Apps, HTTP/MCP and accounting are reused. CPU Apps declare zero accelerators and
actual CPU/RAM requests; capacity derives from both pool CPU and memory. No fake
GPU allocation, second controller, node limit increase or new permission scheme
is introduced. Existing GPU declaration identities remain unchanged.

Terraform owns resources and bootstrap. Later App Settings edits own live
minimum/maximum replicas and grace/cooldown periods. These two image-owned model
artifacts use ordinary loading. PhenoAge has no GPU state. AltumAge snapshot
restore and GPUs other than the actually tested H100 remain unqualified.

The admin adds CPU-appropriate settings, configured per-worker resources,
an editable autoscaler cooldown, an explicit min-zero benchmark warning,
binary memory units and UTC dates on multi-day plots. Catalog Apps created before
their ModelDeployment now acquire the exact missing deployment binding on normal
reads. The atomic registration preserves existing App UUIDs, metadata and history;
it cannot rebind another deployment or a user-created copy.

## Verification before corrected live acceptance

- Model-local reference suite: 25 tests, including actual original AltumAge
  Keras/PyTorch parity and published clinical formula precision.
- CPU/native backend integration: initial broad suite 1,772 passed, 4 skipped,
  88 deselected. Final source `c188ddaa`: 1,806 passed, 4 skipped,
  91 deselected in 267.72 seconds; one existing Starlette warning retained.
- Initial admin suite: 197 passed; TypeScript/production build passed.
- Root deployment contract: 75 passed. Existing/general-CPU Terraform mock plans:
  11 passed. Native/runtime packaging checks validate actual allowed build bytes.
- Late-bootstrap correction: 37 focused regressions and 11 actual PostgreSQL
  regressions, including concurrent atomic binding, passed. Owned strict typing,
  Ruff and unchanged scientific recipe checks passed.
- Replay correction: 25 existing admission/ledger tests and 10 isolated tests
  covering six trace-change cases plus actual PostgreSQL replay/admission/bridge
  cases passed. The exact owned loopback-only disposable database was removed.
  Strict typing and Ruff passed for the changed admission source.
- Final Apps Settings parity: 200 UI tests passed; after aligning cooldown's HTML
  bounds with the existing backend 5–86,400-second contract, all 13 Apps tests and
  typechecking passed. Save regressions preserve unrelated settings exactly.
- Scale-transition correction: 51 publication/bridge/dynamic-route tests passed,
  including HTTP/MCP discovery and durable queued admission through the same
  observed revision's Cold → Desired → Ready → Desired → Cold transitions.
  Desired remains activatable, not Ready; all 21 negative observation/policy
  checks still withdraw invalid routes. Ruff and diff checks passed.

Counts overlap and are not an additive total. Optional skipped suites are not
represented as exercised. Direct CPU/H100 runtime and image-pull measurements
are in [README.md](README.md), with separate exact machine-readable receipts.

## Retained failures and corrections

The first integration build omitted native runtime paths from both independent
build-context allowlists; commit `faef58e1` corrected packaging. A proposed edit
to the archived runtime pyproject was reverted rather than rewriting immutable
helper identities. Actual planning then exposed root bootstrap filtering that
wrongly omitted managed CPU Apps; `aecbe75a` now excludes only the existing static
CPU MSA service and retains both native Apps.

The initial live integration deployed both workers successfully, but public Apps
acceptance failed before any inference: Settings retained an unbound deployment
created earlier during catalog seeding. [PUBLIC-R01.md](PUBLIC-R01.md) and
[BROWSER-R01-R02.md](BROWSER-R01-R02.md) preserve that real failure. The corrective
control-plane source is `1f4015bc`; no manual production database repair was used.

The corrected Terraform rollout completed at approximately 14:26 UTC. The API,
controller and admin each had two Ready replicas. Public Settings returned the
same App UUIDs with `serving` present and `live_settings=true`; the default binding
advanced metadata revision once, to two. The root's admin session logged out204.

Public r02 then saved min0/max1 successfully, but stopped before inference because
the test harness issued scientific `tenant-academic` keys against default-tenant
Apps. An empty catalog was the correct tenant-isolation result, not a model or
cold-routing failure. Both test keys were revoked and returned401. The harness
will use the existing matching inference owner, verify tenant identity before
key creation, and explicitly observe Cold plus zero containers for r03. No live
authorization policy is changed, and r02 receipts remain unmodified.

Public r03 used the matching tenant and confirmed Cold plus zero containers;
both HTTP/MCP discovery checks passed. The first HTTP requests were durably
accepted, but immediate exact replay produced HTTP500. The API traceback ended
at `admission.py:287` / `lifecycle.py:1076`: the retry attempted to register the
same telemetry subject using its new request trace instead of the operation's
persisted original trace. The narrow correction uses `operation.traceparent`;
the existing immutable-subject identity check is retained.

| r03 operation | Accepted UTC | Cleanup outcome |
| --- | --- | --- |
| PhenoAge `fe10fdcf-1eaf-49dc-a1b3-4fa3d3297a2f` | 14:31:27.301519 | Cancelled at 14:31:28.031191 |
| AltumAge `aab0c233-029f-4a6c-8cac-0a7835fbeaa0` | 14:31:28.095593 | Cancelled at 14:31:29.044026 |

These cancellations are the existing effect of test-key revocation in failure
cleanup, not completed predictions or model performance measurements. The client
initially failed to retain a non-JSON response before decoding it; API logs supply
the real500 and traceback. The harness now records status/content type/body hash
before JSON parsing, with a regression for that evidence boundary. Original r03
receipts are unchanged. Read-only admin recovery observes eventual worker idle
cleanup without reactivating keys, resubmitting or changing grants.

The replay/UI release `55c8744f` deployed successfully. Public r04 then proved
PhenoAge's first cold request, result/reference parity and exact replay. The
following MCP request hit a separate real `route_unavailable` response: both API
replicas withdrew the same revision2 while the controller reported ordinary
Desired/Progressing during autoscaler handoff, then republished it as Ready.
The first replica withdrew at14:46:51.886 and republished at14:47:02.279; the MCP
failure was at14:46:57.135. Source `c188ddaa` permits durable admission in this
observed transition, retaining freshness, identity, endpoint, pool and condition
checks. It does not turn desired state into a Ready claim.

AltumAge's two r04 HTTP/MCP requests and exact replays succeeded. The configured
preemptible H100 node group naturally scaled0→1 within its existing maximum2.
Actual node creation was14:48:38, Ready14:48:57; its worker scheduled14:49:09,
pulled the regional image for103.936s, started14:50:56 and became Ready14:51:03.
These are real new-node/image-cold measurements, not a snapshot restore.

| r04 measurement | PhenoAge CPU | AltumAge H100 |
| --- | ---: | ---: |
| First request accepted → observed runtime ready | 7.990402s | 260.120673s |
| First request accepted → completed operation | 8.370505s | 260.630723s |
| Complete client checks, including polling and result/MCP verification | 14.133s | 269.605s |
| Second warm request accepted → completed operation | Not accepted: route failure | 0.883802s |

The raw warm AltumAge `cold_start_seconds` field is0.481633s of readiness-check
latency, not another weight load. No missing timings are replaced by zero.
This mixed r04 cohort is not full public qualification; failed attempts and
successful partial timings are retained separately from the next corrected run.

## Live acceptance status

Registration, replay/UI and scale-transition rollouts passed. Terraform applied
`c188ddaa`; both the API and model controller have two updated Ready replicas on
the exact image above, and the admin has two Ready replicas on `d0097674`.
Public r05 completed successfully at **15:18:11 UTC**. All four original
predictions succeeded on attempt one; HTTP/MCP exact replay retained the same
operation IDs and results matched the original reference fixtures. Both Apps
showed exactly two new logical successes, returned naturally to Cold with zero
workers twice, and retained min0/max1 and their original 300-second idle/cooldown
policies. Temporary keys were revoked and returned401; the workload client and
its admin session closed. See [PUBLIC-R05.md](PUBLIC-R05.md).

| Final public r05 measurement | Clinical PhenoAge CPU | AltumAge H100 |
| --- | ---: | ---: |
| First request accepted → observed runtime ready | 8.611769s | 343.318074s |
| First request accepted → completed operation | 8.994396s | 343.755471s |
| Second warm request accepted → completed operation | 0.500330s | 0.541401s |
| Full cold client checks, including polling/replay/results | 14.125396s | 348.707353s |

PhenoAge used an existing CPU node with a cached image. AltumAge used a newly
autoscaled preemptible H100 node: trigger15:07:23, node created15:10:02,
Ready15:10:21, worker scheduled15:10:42, image pull15:10:43–15:12:19
(96.335s), container start15:12:23 and Pod Ready15:12:31. Actual H100/SM90,
driver580.159.04, Pod/node/GPU IDs and exact image are retained in the witness.
The longer r05 versus r04 startup is mainly before node registration: 159s
versus80s after the autoscaler trigger. Its provider-side cause is not proven.
No manual node/limit change or snapshot restore occurred.

The loaded browser verified Settings, actual Runs/Usage, resource charts and
container identity/cleanup, then signed out and closed at15:18:49 UTC.
[BROWSER-R05.md](BROWSER-R05.md) retains one actual background-context503
among259 observed admin responses. Envoy recorded
`upstream_reset_before_response_started{connection_termination}`, flag `UC`,
at15:11:22.953, request `9be9dc3f-1651-444c-9e5c-9dd32c0ac8af`.
The target API Pod remained Ready with zero restarts; its handler did not record
that503. The next normal browser read succeeded1.103s later without a reload,
and the page remained usable. The underlying connection-termination cause is
not proven. This is not a blanket zero-error browser or availability claim.

These receipts establish exact CPU/H100 HTTP/MCP and single-replica0→1→0
behavior, not multi-replica throughput, p95 guarantees, other GPUs or snapshots.
Publishing the corresponding selected-runtime qualification metadata and the
final post-Terraform settings-preservation check remain the release steps.

The three completed bounded public samplers recorded 224, 278 and 278
admin/discovery samples, each with zero failures. The third spans
14:43:36–15:08:34 UTC and includes the final control-plane rollout and start of
r05. This is sampled availability, not an uninterrupted SLA or inference-load
measurement; it does not erase the retained r03/r04 model-invocation failures.

## Use and performance expectations

Open [the admin console](https://89.169.99.188/admin/) and select **Apps**.
AltumAge is `197fa990-6f65-539d-a1d8-240877ce861b`; Clinical PhenoAge is
`38aad847-9010-51d0-bed7-bcb404bc755d`. The existing HTTP invocation API and MCP
tools are documented with exact synthetic input schemas in
[the model guide](../../models/aging/README.md).

For a latency-sensitive live demo, set AltumAge's **minimum ready workers to 1**
and wait for actual readiness before participants arrive. The tested cold
policy remains min0/max1; it can include minutes of node provisioning and image
transfer even though the model's weights are only about 3 MB. Warm operation
completion was about 0.54s, not the multi-second verification-client clock.
GPU snapshot restore is neither qualified nor presumed beneficial for this
small network; it would not remove node provisioning or the container image.

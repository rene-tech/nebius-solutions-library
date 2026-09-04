# Live admin model-configuration acceptance — 2026-09-03

## Decision

The live admin console proves that an operator can change ordinary desired-state
policy without Terraform: minimum hot replicas, replica ceiling, idle policy and
the requested fast-start level all persisted through the control plane and the
Kubernetes `ModelDeployment`, and the same console rolled the change back to the
exact prior spec. Scoped API-key issue, use and revoke also passed.

The broader product goal is not fully proven. Placement and low-level cache
material cannot be changed on an enabled deployment: the server requires a
drain-to-cold cutover. The console renders every HTTP 409 as a stale-revision
conflict, hiding that actionable reason. The customer-facing L1 choice persisted
but was truthfully downgraded to unqualified `Off`. Enable/disable was validated
and rendered but deliberately not applied because Cosmos and Qwen were the only
qualified live choices and publication withdrawal would violate the acceptance
safety boundary.

Qwen also recycled repeatedly during the observation window without a Qwen spec
change. It recovered and had a ready EndpointSlice at the final check, but
continuous existing-model service is therefore not proven.

The causal diagnosis was completed on 2026-09-04. A concurrent fast-start task
explicitly deleted Qwen pods and launched overlapping deletion loops; the Cosmos
edit did not trigger a Qwen rollout. Exact correlation and the scoped fix are in
[`QWEN_POD_RECYCLE_ROOT_CAUSE_20260904.md`](QWEN_POD_RECYCLE_ROOT_CAUSE_20260904.md).

This is a scoped live finding, not cancer-platform readiness. No scientific
model workflow, mixed-load wave, Terraform convergence check or GPU accounting
claim was attempted.

## Scope and safety boundary

- URL: `https://89.169.99.188/admin/`
- Kubernetes context/namespace: `k8s-inference-h100` / `fs2-models`
- Project/region: `project-e00rene` / `eu-north1`
- Source contract cross-check: read-only `origin/main` at `90428fad`; this task
  branch was not merged or rebased.
- Authentication: `admin_bootstrap_token` was read from the private workloads
  state and exchanged for a short-lived same-origin operator session. The value
  was never printed, logged or stored in Git. The session was explicitly signed
  out. The later raw-problem probe used a mode-0600 temporary cookie jar, logged
  out with HTTP 204, then zeroed and unlinked the jar.
- Live images observed, not changed:
  - control plane and model controller:
    `sha256:65fe465bfea246472c77f6c41ee39e714c194dd9cefc71f9a481d43380ca6b37`
  - admin console:
    `sha256:75bf54476e194a3a26732620b59a850e37921fa7ada3c7080b0008dfecdf8d6a`
- Qwen and Cosmos identities were UID-fenced before mutation. No B300 resource,
  quota, queue, Terraform state, Helm release or control-plane resource was
  changed. The existing workload names containing the historical string
  `b300` were observed only; all placement used H100 nodes and pools.

## Control results

| Control | Result | Exact live evidence |
| --- | --- | --- |
| Enable/disable | **UNPROVEN LIVE — safety-constrained** | Changing Cosmos from `Enabled` to `Disabled` validated as `accepted` and rendered preview `f2cf9657-b953-4bcd-b231-365bbc4ea135`, digest `sha256:55c2099928556de9e7153caa7150e9222be0949c3be6eeebdc77499affcaae59`. The disabled plan had four resources and omitted `fs2-model-publication-cosmos3-nano`, which is present in the enabled five-resource plan. Apply was intentionally skipped because it would withdraw the protected Cosmos MCP publication. |
| Minimum hot replicas | **PASS** | Cosmos revision r6 changed `minReplicas` from `0` to `1`. Kubernetes generation 6 reached one desired/admitted/ready/available replica. Rollback r7 restored `0`. |
| Maximum replicas | **PASS** | r6 changed `maxReplicas` from `2` to `1`; Kubernetes matched. r7 restored `2`. A separate preview of `3` on only `h100-1x` correctly failed closed with `pool_capacity_infrastructure_required` at `$.spec.availability.maxReplicas`, naming Terraform prerequisite `accelerator_pools.h100-1x.max_nodes`; no quota or infrastructure change was attempted. |
| Idle policy | **PASS (persistence)** | r6 changed `idleSeconds` from `30` to `137`; Kubernetes matched. r7 restored `30`. The 137-second scale-to-zero behavior was not exercised because r6 intentionally held a hot floor of one. |
| Placement / pool selection | **REQUIRES DISRUPTIVE CUTOVER** | An enabled Cosmos proposal selecting only `h100-1x` validated and rendered, but apply returned HTTP 409. A fresh-session raw probe reproduced exact problem `cold_cutover_required`: `drain the current revision and wait for observed zero replicas before changing runtime material`, request `4246c7bb-820f-4bc0-a562-cc53599cb8c8`. The UI instead displayed `This desired revision changed on the server. Refresh it before retrying the request.` even after refresh/revalidate/replan. Drain was not exercised because it would withdraw protected Cosmos. |
| Cache / start level | **PARTIAL** | Customer-facing `Fixed / L1 / AllowLowerLevel` persisted in r6. The controller assigned and qualified only `Off`, with `RequestedLevelUnqualified`: `requested L1 lacks compatible benchmark evidence; Off assigned instead (h100-1x: no compatible model-start p95)`. The low-level `Cache tier` remained preset-locked/read-only at `SharedFilesystem`, so an operator cannot change the actual cache tier for this installed qualified tuple. Such a cache-material change would also require cold cutover. |
| API keys | **PASS** | The UI issued one key named `acceptance-ui-catalog-20260903-1328`, tenant `tenant-e00f3wdfzwfjgbcyfv`, scope `catalog.read`, allowed model `qwen3-8b`, request/rate limit `2`, window `60s`, concurrency `1`. Its one-time credential was used only in browser memory; authenticated `/v1/models` returned HTTP 200 with only `qwen3-8b`. The UI revoked it and active-key count returned from four to three. Audit events 296/297 record `token.issue`/`token.revoke`, both `succeeded`, for token `8a32f5db-8b92-42a8-8905-0d7d432277b5` and non-secret prefix `fs2_pat_8a32f5db8b92`. The secret was never printed, saved or committed. |
| Rollback | **PASS** | Audit event 298 records successful r6 update at `2026-09-03T13:33:46Z`; event 299 records successful rollback to r5 as new r7 at `13:40:35Z`. The Kubernetes UID stayed `6cdbf9c3-77b6-41f4-a931-30672611063c`; generation/observedGeneration became `7/7`; exact baseline digest `sha256:75828a96698fd6c756be732fc25b3c0563182fc143d2e9165165470de179d711` was restored. Cosmos returned `Cold`, 0/0, and its temporary hot Deployment/Pod disappeared. |

## Successful reversible mutation

The sole applied model change used protected cold-by-default Cosmos because it
had no ready replica at baseline. Placement and publication were held constant:

| Field | Baseline r5 | Applied r6 | Rolled back r7 |
| --- | --- | --- | --- |
| Desired state | Enabled | Enabled | Enabled |
| Hot floor | 0 | 1 | 0 |
| Replica ceiling | 2 | 1 | 2 |
| Idle seconds | 30 | 137 | 30 |
| Eligible pools | `h100-1x`, `h100-reserved-8x` | unchanged | unchanged |
| Fast start | Automatic, Off–L4 | Fixed L1, allow lower | Automatic, Off–L4 |
| MCP / OpenAI | true / false | unchanged | unchanged |
| Spec digest | `sha256:75828a96698fd6c756be732fc25b3c0563182fc143d2e9165165470de179d711` | `sha256:c73fdff0e6658d6516fa3bb35ab4e21a459e33b5860fc6eb8cfc01fe28fae0f1` | exact baseline digest |

The UI reported r6 `Applied`, revision history committed, Kubernetes projection
`Applied and verified`, non-idempotent replay, generation 6 and the unchanged
resource UID. The hot Pod scheduled on capacity-block H100 node
`computeinstance-e00m0hsph76ajt9sdb` at `13:33:47Z`; the 9,186,624,472-byte
image pull completed in 186.397 seconds at `13:36:54Z`; the model observation
became ready at `13:38:28Z`. The observed schedule-to-ready interval was 281
seconds, within the selected nominal L1 boundary of 300 seconds, but it is not
valid L1 qualification because the controller explicitly assigned `Off` and
published no compatible benchmark evidence.

## Exact failures and product gaps

### Placement error is hidden by the UI

The live server's exact response for an enabled placement change was:

```json
{
  "status": 409,
  "code": "cold_cutover_required",
  "detail": "drain the current revision and wait for observed zero replicas before changing runtime material",
  "request_id": "4246c7bb-820f-4bc0-a562-cc53599cb8c8"
}
```

The current UI maps every 409 to a stale-revision message. Therefore an operator
is told to refresh even when refresh cannot fix the problem. It should surface
the server problem code/detail and guide the explicit drain → wait for observed
Cold/zero → apply → enable sequence.

### A qualified duplicate is not an isolated test deployment

A disabled draft named `acceptance-cosmos3-nano-20260903` validated and planned,
but its renderer ignored that desired-deployment name for workload identity. The
preview targeted the protected `cosmos3-nano` Deployment, Service,
ServiceAccount and `cosmos3-nano-adapter` ConfigMap at endpoint
`fs2-models/cosmos3-nano:8080`. Applying it could have taken controller ownership
of the protected resources. Hard delete is disabled in v1. The draft was never
applied, and the corresponding `ModelDeployment` is confirmed absent.

This prevents a safe same-model sandbox from being used to prove enable/disable
or cold-cutover placement behavior on a shared cluster.

### Cache semantics are two different controls

The editable fast-start selector expresses a customer readiness target. It does
not directly select the material cache tier and it does not guarantee the target:
r6 stored L1 but the live controller fell back to `Off`. The mechanism-level
cache tier is disabled when a qualified configuration preset is selected. The
admin surface should make this distinction explicit when the product question is
“which cache/start level is served where.”

### Incidental access-form defect

Key creation succeeded, but Chromium logged an HTML-pattern error for
`[A-Za-z0-9][A-Za-z0-9_.-]*` under UnicodeSets `/v`: `Invalid character in
character class`. This did not block the tested flow, but the form pattern should
be escaped or replaced with application validation.

## Protected-service continuity

Cosmos was deliberately activated for the reversible policy test and then
restored to its original cold state. Its ModelDeployment UID never changed, its
baseline digest is restored, no acceptance-named ModelDeployment exists, and
the transient hot workload is gone. Final MCP `tools/list` returned HTTP 200 and
advertised Cosmos at revision
`dynamic:sha256:75828a96698fd6c756be732fc25b3c0563182fc143d2e9165165470de179d711`.

Qwen was never edited: its ModelDeployment remained UID
`a7ca4c91-973b-467f-aaba-3fa566ff9546`, generation/observedGeneration `4/4`,
and digest
`sha256:d1da450072bb7002d46b234ba5842671e001e10f36eefc382c1c0195c6876add`.
Nevertheless, Kubernetes events show repeated Pod replacement beginning at
`13:23:58Z`, before the r6 Cosmos apply, and continuing through `13:45:17Z`.
Several replacements logged startup-probe connection refusals. At `13:44:45Z`
the current Pod was killed and the Deployment temporarily had no ready replica.
The final replacement became available at `13:47:08Z`; the final EndpointSlice
had one ready/serving endpoint on H100 node
`computeinstance-e00p3acr87k9k4mckj`. Authenticated `/v1/models` returned HTTP
200 with `qwen3-8b`, and MCP `tools/list` returned Qwen at its unchanged dynamic
revision.

Because that churn occurred with an unchanged Qwen desired generation and
started before the model mutation, it is not evidence that the Cosmos update
caused the disruption. It is still direct evidence that the “Qwen keeps
serving” safety condition did not hold continuously during this acceptance
window and needs separate reliability diagnosis before a disruptive control is
tested.

## Cleanup and residual state

- Rollback r7 restored the exact Cosmos baseline spec and digest.
- Temporary key `8a32f5db-8b92-42a8-8905-0d7d432277b5` is revoked; three
  pre-existing keys remain active. The revoked audit/history record is retained
  by design.
- The browser operator sessions were signed out; final session deletion returned
  HTTP 204.
- Session-bearing Playwright trace data and temporary screenshots were deleted
  after the non-secret evidence above was transcribed. No browser state file,
  cookie, API credential or Terraform output was committed.
- No Terraform plan/apply, GPU job submission, quota change, B300 operation,
  Helm rollout, control-plane change, queue change or manual Kubernetes mutation
  occurred.
- Durable expected residue is limited to Cosmos revision history r6/r7 and the
  revoked API-key/audit rows. Both are append-only operator records.

## Required follow-up before declaring the product goal proven

1. Surface `cold_cutover_required` instead of rewriting it as an ETag conflict.
2. Provide a non-colliding qualified deployment identity, or a task-owned model,
   so enable/disable and drain-to-cold placement/cache changes can be exercised
   without withdrawing Qwen or Cosmos.
3. Decide whether the product promises a fast-start target or direct cache-tier
   selection. If it promises L1, publish compatible benchmark evidence so the
   controller does not assign `Off`.
4. Diagnose the unchanged-generation Qwen Pod churn and repeat a continuity
   watch before any protected-model cutover test.

# Model inventory

Use **Full model inventory** in the admin navigation (`/admin/model-inventory`)
or authenticated `GET /admin/api/v1/model-inventory`. This joins the entire
installed serving catalog with scientific profiles, including undeployed
models. It returns counts, per-model reasons, replica counts, batch readiness,
runtime image identity when configured, and the published GPU snapshot status.

The inventory is a read-only operator view, not an inference-access grant.
Sign in with the existing admin session flow. It is not necessary to inspect
Kubernetes to discover the model list.

| Availability | Meaning |
| --- | --- |
| `hot` | Existing serving projection reports a healthy ready replica. |
| `cold` | Configured serving route, currently zero ready replicas. |
| `batch-ready` | Qualified scientific profile; GPU Jobs start on dispatch, not permanently hot workers. |
| `not-deployed` | Catalog entry without a configured service or published scientific profile. This is not scale-to-zero. |
| `disabled` | Configured serving model with inference disabled. |
| `candidate` / `blocked` | Scientific runtime is not qualified; the reason is included. |
| `unknown` | Required observation is unavailable. It is not evidence the model is absent. |

Other serving states (`loading`, `queued`, `unhealthy`, `unsupported`) retain
the existing live serving projection and its explanation. Snapshot status is
separate from readiness: a working model may still use normal loading.
`not-reported` means no capability is published here, not proof snapshotting is
intrinsically impossible. Image/weight/JIT caches are not GPU snapshots.

Existing specialized endpoints remain:

- `/admin/api/v1/models`: configured serving deployments and metrics.
- `/admin/api/v1/scientific-models`: scientific runtime readiness and evidence.
- `/v1/models`: authorized serving models, including native scientific/media
  HTTP runtimes as well as OpenAI-compatible runtimes. A discovery entry does
  not change its model-specific inference request schema.
- `/v1/scientific-models` and MCP `list_scientific_models`: authorized batch
  profiles, operations and request schemas.

For complete-fleet acceptance use the deployment's explicit expectations,
never a denominator derived from whatever happens to be enabled. The retained
H100 restoration scope is [24 model/profile IDs](../acceptance/h100-fleet/expected-models.json),
with GLM explicitly excluded. The [campaign record](../acceptance/h100-fleet/README.md)
explains the earlier fleet migration gap and current work; it does not claim
completion before public inference and startup tests pass.

## Deployment settings

Select serving IDs in `deployment.models.enabled` with `selection = "explicit"`.
Use `image_overrides` for a qualified runtime and `pool_overrides` for placement.
An optional `runtime_overrides` entry can set `gpu_count` (for example, a model
requiring multiple smaller GPUs) and `compile_cache_abi` for an exactly measured
compiler cache. GPU count propagates to requests, limits, placement, scaling
and accounting; it does not claim that an untested GPU/runtime combination works.
Otherwise compiler cache paths use the selected accelerator/driver profile.

For controller-owned deployments, add the model to
`deployment.dynamic_models.bootstrap_model_ids`, or create it through live
model configuration after its runtime is qualified. A completed ownership
handoff is retained across later catalog additions and template updates;
operators should not release working deployments again for each new model.

## GPU snapshot options

The inventory publishes `snapshot_selectable`, `snapshot_bundle_ids`,
`snapshot_evidence_scope`, and measured native/restore startup clocks. The
evidence must match the configured immutable runtime, not merely a similarly
named catalog entry. Dynamic route revisions and upstream artifact revisions
are different identities; the API resolves the validated publication before
checking snapshot compatibility.

Install tested bundles with the Terraform
`deployment.dynamic_models.gpu_snapshots.bundle_files` or
`deployment.scientific_batch.gpu_snapshots.bundle_files` settings. Files carry
the exact runtime/source/storage identity and qualification evidence. Their
paths are relative to `k8s-inference`, or may be absolute operator-owned paths.
Do not copy an H100 qualification to a different GPU/driver and call it tested.

After installation, select a serving snapshot in **Model deployments → model →
Model startup path**. Use **Scientific runs → Scientific model dispatch policy**
for a batch-stage startup option. These live choices do not require editing
Terraform for each customer workload. A serving change requiring a zero-replica
cutover uses the explicit drain action; afterward restore the intended hot
floor and enabled state. Scientific choices apply to newly admitted runs;
existing operations retain their selected policy and bundle.

Selectable means the configured bundle has passed qualification, not that
every operation used it. Check operation/runtime evidence for an actual
`cuda-criu-restored` event, versus normal loading or fallback. Successful Job
logs remain in the existing observability system after Pod cleanup. The
[production option receipts](../acceptance/h100-fleet/snapshots/production-options-h100-20260907.json)
demonstrate this correlation without requiring the original Pod to still exist.

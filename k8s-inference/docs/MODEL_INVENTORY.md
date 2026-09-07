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
- `/v1/models`: authorized OpenAI-compatible models only.
- `/v1/scientific-models` and MCP `list_scientific_models`: authorized batch
  profiles, operations and request schemas.

For complete-fleet acceptance use the deployment's explicit expectations,
never a denominator derived from whatever happens to be enabled. The retained
H100 restoration scope is [24 model/profile IDs](../acceptance/h100-fleet/expected-models.json),
with GLM explicitly excluded. The [campaign record](../acceptance/h100-fleet/README.md)
explains the earlier fleet migration gap and current work; it does not claim
completion before public inference and startup tests pass.

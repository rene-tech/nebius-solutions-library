# Kubernetes inference on Nebius

`k8s-inference` is the customer-facing inference solution in the Nebius
Solutions Library. A single
`terraform.tfvars` describes the target, capacity envelope, qualified
accelerator pools, models, scaling, edge, observability, and acceptance probe.
`inference-stack` normalizes that file and operates the three Terraform states in
dependency order:

1. Nebius infrastructure and Managed Kubernetes
2. cluster foundation, queueing, and observability
3. model workloads, autoscaling, control plane, MCP, and admin services

The cluster remains running after `apply` so it can be tested. An explicit
`destroy` removes it only for a fully disposable contract. The portable
reference-data defaults are disposable and deletable; retained reference
storage is an explicit opt-in that turns the command into a downstream-stage
release, not a full stack destroy. Protected infrastructure remains managed
until it is adopted by a separate durable state.

Routine model lifecycle is implemented through the authenticated admin API and
the Kubernetes-native `ModelDeployment` reconciler. Operators can add, edit,
or drain a qualified model and change its hot floor, ceiling, placement, cache
policy, and exposure without running Terraform. Terraform continues to own the cluster
and its capacity, storage, registry, operator, and policy envelopes. See
[Dynamic model configuration](DYNAMIC_MODEL_CONFIGURATION.md) for the exact
ownership boundary, current renderer, and optional extension points. See
[Model fast-start levels](FAST_START_LEVELS.md) for the customer timing classes,
clock boundaries, automatic policy, and evidence requirements.
[NVIDIA ModelExpress](docs/MODELEXPRESS.md) documents the optional P2P loader,
its exact runtime qualification, Terraform switch, metrics, and rollback.
[Shared GPU scheduling and lifecycle telemetry](docs/QUEUE_AND_GPU_TELEMETRY.md)
documents Kueue floors/borrowing, customer service classes, exact correlation
labels, DCGM cadence, and the OTel/Loki/Tempo data path.
[Scientific batch API quick start](docs/SCIENTIFIC_BATCH_API.md) walks a
researcher or hackathon team through discovering, uploading, submitting,
following, and downloading a scientific run over HTTPS or MCP.

## Accelerator and qualification boundary

`deployment.accelerator_pools` is an open map. Each entry declares the Nebius
platform and preset, GPU resource name, host architecture, capacity mode,
driver owner, topology, node floor/ceiling, optional local storage, and an
optional Nebius capacity-block reservation policy. Those provider identifiers
are intentionally not hard-coded in HCL, so the same solution can describe
H100, H200, B200, B300, GB300, RTX PRO 6000 Blackwell, heterogeneous clusters,
and future Nebius shapes.

`inference-stack preflight` checks the selected platform, preset, GPU count,
preemptible support, OS, driver preset, and Kubernetes version against the live
target project before apply. `models.pool_overrides` explicitly binds a model
to one of those pools and Terraform verifies its GPU count and scheduling
contract. That explicit binding is an operator compatibility assertion; live
provider validation cannot prove that an arbitrary model runtime supports a
new GPU architecture. Keep model/runtime qualification evidence with any new
binding.

Capacity blocks are regular GPU capacity, not a third capacity type. Declare a
fixed pool (`min_nodes == max_nodes`) and pass the ordered capacity-block group
IDs through the same shape used by the Nebius node-group provider:

```hcl
"h100-reserved-8x" = {
  platform          = "gpu-h100-sxm"
  preset            = "8gpu-128vcpu-1600gb"
  accelerator_class = "nvidia-h100-sxm5-80gb"
  gpus_per_node     = 8
  capacity_type     = "regular"
  min_nodes         = 2
  max_nodes         = 2
  reservation_policy = {
    policy          = "STRICT"
    reservation_ids = ["capacityblockgroup-yourblockid"]
  }
  driver = {
    mode   = "managed"
    preset = "cuda13.0"
  }
}
```

`STRICT` prevents fallback to shared PAYG capacity. Terraform renders a fixed
node group, omits the preemptible block, and uses a zero-surge/one-unavailable
rollout so a reservation that is exactly full is still maintainable. Pools
without `reservation_policy` retain the existing autoscaling behavior.

The mounted `lean-routes.json` intentionally uses the exact two-field v2
runtime contract (`schema` and `routes`) accepted by the pinned control-plane
image. The reviewed qualification projection is retained in the same immutable
ConfigMap under `qualification-projection.json`, but is not mounted into the
runtime route parser. The ConfigMap name includes a digest of its complete data
map, so route changes create a new object and roll the Helm workload without
reusing kubelet-cached immutable content.

The checked-in accelerator catalog retains reviewed B300 fixtures and a
current Nebius GPU inventory for examples and tests. It is not an allowlist for
custom pools. A small regular CPU system pool hosts Kubernetes and platform
services independently of GPU capacity.

The solution never requests or raises service quotas or limits. It creates
only run-scoped identities and registry-viewer access required by its worker
nodes. Capacity, quota, permission, or compatibility failures are returned to
the operator instead of changing limits or broad project roles.

## Quick start

Prerequisites are Terraform 1.11 or newer (but older than 2.0), `kubectl`,
`helm` 3.9 or newer for the digest-pinned OCI charts, `jq`,
[`crane`](https://github.com/google/go-containerregistry/tree/main/cmd/crane),
Git, and authenticated Nebius CLI access to the target project. The optional
local Kueue and JobSet server tests additionally need
[`kind`](https://kind.sigs.k8s.io/). Authentication is
runtime context, not desired state, so select it with `NEBIUS_PROFILE` or
`--nebius-profile` rather than putting credentials in Terraform variables.

```bash
cd k8s-inference
install -m 0600 terraform.tfvars.example terraform.tfvars

# The full_catalog profile needs Docker config JSON for its NVCR-hosted DCGM exporter.
# NGC_API_KEY is additionally required only when the selected set contains an
# NGC-backed NIM (currently MSA Search PDB70, OpenFold2, or OpenFold3).
export FS2_NVCR_DOCKERCONFIGJSON='{"auths":{...}}'
export FS2_NGC_API_KEY='...'

# Required for a fresh foundation: Terraform creates the referenced Secret.
export FS2_GRAFANA_ADMIN_USERNAME='...'
export FS2_GRAFANA_ADMIN_PASSWORD='...'

NEBIUS_PROFILE=sandbox ./inference-stack validate --var-file terraform.tfvars
NEBIUS_PROFILE=sandbox ./inference-stack plan --var-file terraform.tfvars
NEBIUS_PROFILE=sandbox ./inference-stack apply --var-file terraform.tfvars
NEBIUS_PROFILE=sandbox ./inference-stack status --var-file terraform.tfvars
NEBIUS_PROFILE=sandbox ./inference-stack output --var-file terraform.tfvars
```

After a deployment, `apply` and `status` print all non-secret customer entry
points as top-level JSON fields:

```json
{
  "mcp_endpoint_url": "https://<allocated-public-ip>/mcp",
  "admin_web_interface_url": "https://<allocated-public-ip>/admin/",
  "inference_base_url": "https://<allocated-public-ip>/v1",
  "grafana_url": "https://<allocated-public-ip>/admin/observability/grafana",
  "reference_data": {"filesystem_id": "computefilesystem-...", "bucket_id": "storagebucket-...", "cpu_pool_id": "mk8snodegroup-...", "status_service": "fs2-reference-data-status.fs2-reference-data.svc.cluster.local:8080", "pipeline": {"job_name": "fs2-stage-af3-..."}},
  "reference_data_contract": {"storage": {"schema": "fs2-serve.nebius.ai/reference-data-storage/v1"}, "plane": {"schema": "fs2-serve.nebius.ai/reference-data-configuration/v1"}}
}
```

`grafana_url` is `null` when external Grafana publication is disabled.
`reference_data` and `reference_data_contract` are `null` when that plane is
disabled. During the reviewed infrastructure-only transition, `status` still
reports the retained storage/bucket/CPU identities and uses `plane: null` until
the workloads state owns a live status service and immutable pipeline Job.

These values come from exact named Terraform outputs in the workloads state.
They are resolved only after the infrastructure stage has allocated the edge
address. An `internal-only` deployment emits loopback URLs instead; those are
usable only while the run-scoped operator proxy from the workloads
`port_forward_contract` is active.

The explicit `inference-stack output` command is the credential handoff. It
prints the sensitive `access_bundle`, including the admin URL and bootstrap
token, MCP and `/v1` URLs plus their scoped PAT, Grafana URL and native-login
credentials, cluster/project/region identity, and the kubeconfig command:

```json
{
  "schema": "fs2-serve.nebius.ai/access-bundle/v1",
  "cluster": {"project_id": "project-...", "region": "...", "cluster_id": "mk8scluster-..."},
  "endpoints": {"admin_portal_url": "https://.../admin/", "mcp_url": "https://.../mcp", "inference_base_url": "https://.../v1", "grafana_url": "https://.../admin/observability/grafana"},
  "credentials": {"admin_bootstrap_token": "<redacted>", "mcp_inference_token": "<redacted>", "inference_access_token": "<same scoped PAT>", "scientific_access_token": "<separate academic PAT or null>", "grafana": {"username": "<redacted>", "password": "<redacted>"}},
  "mcp_access": {"tenant_id": "<cluster tenant>", "models": ["*"]},
  "scientific_access": {"tenant_id": "<academic tenant>", "models": ["*"]},
  "reference_data": {"filesystem_id": "computefilesystem-...", "bucket_id": "storagebucket-...", "bucket_name": "...", "cpu_pool_id": "mk8snodegroup-...", "status_service": "...", "pipeline": {"job_name": "..."}},
  "reference_data_contract": {"storage": {"schema": "fs2-serve.nebius.ai/reference-data-storage/v1"}, "plane": {"schema": "fs2-serve.nebius.ai/reference-data-configuration/v1"}}
}
```

`mcp_inference_token` remains for compatibility. `inference_access_token` is a
clear alias of that same scoped PAT for OpenAI-compatible `/v1` clients. It
always remains bound to the cluster tenant used by general serving. When
`academic_assets.enabled` is true, Terraform additionally emits
`scientific_access_token`, a distinct PAT bound to the configured academic
tenant. Use that credential for academic scientific submissions; tenant
enforcement is not weakened or shared between the two credentials. Other
tenant credentials remain a live admin-console operation.

Open the emitted `admin_portal_url` and paste
`credentials.admin_bootstrap_token` into the operator sign-in form. MCP clients
use `credentials.mcp_inference_token` as a Bearer token, OpenAI-compatible
clients use `credentials.inference_access_token`, academic scientific clients
use `credentials.scientific_access_token`, and Grafana uses the emitted
`credentials.grafana.username` and `credentials.grafana.password`. To print
one value directly from the bundle:

```bash
NEBIUS_PROFILE=sandbox ./inference-stack output --var-file terraform.tfvars \
  | jq -r '.credentials.admin_bootstrap_token'
```

Run it only in a private terminal and do not pipe its output to logs, CI
artifacts, tickets, or shell history. The credentials necessarily live in the
protected run-owned Terraform state; automatic `apply` and `status` output
remain non-secret.

The shipped example selects a shared public endpoint, so no foreground process
or client-side port forwarding is required:

```hcl
edge = {
  mode             = "public"
  source_cidrs     = ["0.0.0.0/0"] # Restrict this for your production access policy.
  acme_email       = "operator@example.com"
  acme_environment = "production"
}
```

For an explicitly internal-only development deployment, reserve a loopback
tuple in the same customer tfvars file. The defaults remain `18080`, `18081`,
and `18082`; use a different tuple for another concurrently operated cluster:

```hcl
edge = {
  mode = "internal-only"
  port_forward_ports = {
    control_plane = 28080
    admin_console = 28081
    operator_proxy = 28082
  }
}
```

The three values must be distinct whole non-privileged TCP ports. Terraform
copies them into `port_forward_contract`; `mcp_endpoint_url` and
`admin_web_interface_url` then use the configured same-origin operator-proxy
port. No HCL or script edits are required to give another deployment a
non-conflicting local endpoint tuple.

Public edge uses the Let's Encrypt production IP-ACME directory by default, so
successful acceptance requires a browser-trusted certificate. Set
`acme_environment = "staging"` explicitly only for issuance testing;
staging certificates intentionally fail the trusted-TLS acceptance probe.
`internal-only` does not expose a public listener. Run
`inference-stack proxy` only for `internal-only` mode after `apply` to start the
two run-scoped Kubernetes port-forwards and the same-origin loopback proxy
described by `port_forward_contract`. Keep that foreground process running
while using the emitted MCP and admin links; stop it with `Ctrl-C`. Separate
deployments can run concurrently when their `edge.port_forward_ports` tuples
differ. These loopback URLs are reachable only from the machine running the
proxy; use an SSH tunnel for remote testing or select `edge.mode = "public"` for
a shared endpoint.

`validate` creates no cloud resources. On a new run, `plan` stops after the
infrastructure plan because foundation providers cannot safely plan until the
new API server exists. Once upstream state exists, `plan` first checks that
state for changes before it plans the first missing downstream stage. With all
three states present, it plans them through the same convergence barriers.
`apply` plans and applies infrastructure, foundation, and workloads in that
order, so a normal wrapper `apply` converges the complete stack in one command.

Existing states also converge in dependency order. If the infrastructure plan
changes a managed resource or output, `plan` stops and asks the operator to
apply infrastructure before it plans foundation. It uses the same barrier
between foundation and workloads. This prevents a downstream provider from
planning against an old remote-state or in-cluster contract; rerun `plan` after
each requested upstream apply. Planning never mirrors registry content.

The default `artifacts.registry_policy.mode = "regional-mirror"` makes the
Terraform-created registry useful: after infrastructure apply, the wrapper
copies the complete selected application/model image closure with `crane
copy --no-clobber`, verifies each full OCI digest, rewrites workload references
to the regional registry, and only then plans foundation and workloads. Model
sources resolve from `models.image_overrides` first and otherwise from the
checked-in runtime catalog. Every effective source must be digest-pinned and
deployable; unresolved `.invalid` catalog sources fail before cloud mutation.
The non-secret receipt is stored as `registry-mirror.receipt.json` in the
private run root. Registry credentials are read from the selected Nebius
profile and optional configured Docker-config environment reference; they are
materialized only in a temporary directory and never written to Terraform
inputs or state.

The control plane currently uses an in-cluster CloudNativePG deployment. It
does not create Nebius Managed PostgreSQL. Its database, admin bootstrap
credential, encryption material, and PAT verifier state are therefore part of
the cluster lifecycle and protected Terraform/Kubernetes state boundary.

Terraform creates a separate, scoped bootstrap PAT and a Helm post-install /
post-upgrade job idempotently provisions its digest and policy in the durable
control-plane token store. It has a wildcard model policy so later live-catalog
additions work without credential rotation, but remains bounded to one tenant
and the MCP, inference, catalog, operation-lifecycle, and declared-use scopes.
When academic assets are enabled, a second Terraform-owned Secret and hook job
provision the distinct academic scientific PAT; its tenant and principal never
replace the general serving credential.
The admin token is deliberately not valid for `/mcp` or `/v1`.
An intentionally revoked or expired Terraform bootstrap PAT stays inactive:
the next Helm upgrade fails closed instead of silently reactivating it. Rotate
the Terraform-owned token material before that upgrade by applying with both
`-replace=random_id.bootstrap_access_token_id` and
`-replace=random_password.bootstrap_access_token_secret` through the same
protected workloads-stage workflow.
Rotate the optional academic credential independently with
`-replace=random_id.scientific_access_token_id[0]` and
`-replace=random_password.scientific_access_token_secret[0]`.

For ongoing users, use the admin interface's **Access / API keys** area to issue
revocable PATs with only the required models, scopes, concurrency, request, and
GPU-time budgets. UI-created and rotated key values retain one-time disclosure:
store each in an owner-only (`0600`) file when shown. Clients send a PAT as
`Authorization: Bearer <PAT>`. Never put any bootstrap credential or PAT in
tfvars, source, shell history, Task Deck cards, or acceptance receipts.

The default run directory is a private XDG state path derived from the absolute
tfvars path. Reusing the same path resumes the same staged deployment. To make
the location explicit:

```bash
./inference-stack apply \
  --var-file terraform.tfvars \
  --run-root /a/private/k8s-inference-state-directory
```

The wrapper creates the run directory with mode `0700` and generated contract,
plan, state-input, and kubeconfig files with private permissions where it owns
them. Secret values are passed through process environment only and never
written to generated stage tfvars. Terraform state can still contain sensitive
provider or resource data, so the whole run directory must be protected and
backed up according to the operator's policy.

## Configuration

The top-level variable is `deployment`:

| Field | Purpose |
| --- | --- |
| `schema_version` | Interface version; this release accepts `1`. |
| `name` | Stable cluster/deployment name. The run ID also includes project and region. |
| `target` | Project and region; new projects also require an explicit network/subnet/CIDR and system update strategy. |
| `profiles.capacity` | CPU-system, cache, and maximum-capacity envelope: `minimal` or `full_catalog`. |
| `profiles.accelerators` | Qualified accelerator-pool topology. If omitted, it follows the capacity profile. |
| `profiles.models` | Model catalog: `minimal` or `full_catalog`. |
| `cluster` | Kubernetes version, API CIDR allowlist, and optional regular CPU system-pool shape and inotify ceiling. |
| `accelerator_pools` | Open map of GPU platform/preset, capacity, optional capacity-block reservation, topology, driver, local-storage, and node-floor/ceiling settings. |
| `models` | Profile or explicit selection, KEDA/static scaling, hot-model floor, and per-model scaling overrides. |
| `dynamic_models` | Optional live controller gate, exclusive workload owner, and initial model IDs. Internal envelope and renderer JSON is derived, not customer-authored. |
| `scheduling` | Optional GPU-neutral Kueue Cohort, queue floors, borrowing/preemption, fair-sharing weights, model lanes, and five customer service classes. |
| `scientific_batch` | Optional staged scientific controller plus a content-addressed execution map; enabling it also installs and qualifies pinned JobSet 0.12.0 for true-gang work. |
| `storage.shared_cache` | Optional shared model-cache size/type/block-size override. |
| `storage.scientific_artifacts` | Optional dedicated same-region versioned result store: lifecycle, derived or explicit bucket name, capacity, application retention days, signed-handle TTL, maximum artifact size, object-storage egress CIDRs and the approved media types. Separate bucket, identity and key from `storage.reference_data`. |
| `scientific_batch` | Staged scientific batch gates. `enabled` requires `storage.scientific_artifacts.enabled`; `writes_enabled` requires `enabled`. |
| `artifacts.external_registry_ids` | Same-tenant registries whose immutable images need run-scoped node-pull viewer access. Terraform creates a project-scoped reader group beside each registry, including registries in another project or region. |
| `artifacts.registry_policy` | Defaults to `regional-mirror`; optional prefix controls the target repository namespace. `direct-source` is an explicit opt-out that leaves runtime pulls pointed at upstream registries. |
| `edge` | `internal-only` or bounded public ingress configuration, including an optional per-cluster loopback port tuple. |
| `observability` | DCGM cold-start campaign, optional external Grafana publication, and persistent Alertmanager enablement/retention/storage. |
| `secrets` | Environment-variable names and Kubernetes Secret key references, never secret values. |
| `acceptance` | Optional post-deployment probe job. |

For a checked-in target, `target.project_id` and `target.region` are sufficient.
For another project, add `project_name`, `network.network_name`,
`network.subnet_name`, `network.private_subnet_cidr`, and
`system_update_strategy`. The infrastructure stage verifies those facts through
the provider. Custom accelerator pools additionally go through the live
platform and Managed Kubernetes compatibility preflight.

The system pool defaults `inotify_max_user_instances` to `8192`, preventing
registry credential helpers and platform controllers from exhausting the
Ubuntu default as their file watches grow. Terraform persists the setting as
`/etc/sysctl.d/99-fs2-system-inotify.conf` and loads that exact file during the
system node's first boot. The accepted `256`-`65536` range keeps the override
intentional and bounded. This cloud-init fragment belongs only to the regular
system node group; changing it does not mutate accelerator, general CPU, or
reference-data node templates.

Alertmanager and operator observability access are described in
[`docs/ADMIN_OBSERVABILITY_ACCESS.md`](docs/ADMIN_OBSERVABILITY_ACCESS.md).
Alertmanager enablement, retained storage, and retention are tfvars settings;
authenticated Grafana remains the only public observability application and
Tempo launches through a datasource-selected Grafana Explore link.

### Scientific result artifact store

`storage.scientific_artifacts` provisions a dedicated same-region versioned
bucket, a dedicated service account and group, a bucket policy granting only
`storage.object-editor` on `scientific/v1/*`, and an S3 access key delivered
exclusively through Nebius MysteryBox. It is a separate store from
`storage.reference_data`: results and immutable public science inputs never
share a bucket, a policy, a key or a retention decision, and the facade refuses
a configuration in which the two bucket names collide.

Committed artifacts are addressed as

```
scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>
```

so tenant prefixes are disjoint and a retry that reproduces identical bytes
writes the identical key.

Terraform propagates only the access-key ID, an opaque MysteryBox reference and
a revision. The workloads stage resolves the secret ephemerally and writes it
with the provider's write-only argument into `fs2-system/fs2-serve-artifact-store`
under `credentials.json`; the secret is absent from state, plans, generated
tfvars, Helm values, outputs and receipts. A rotated key moves the revision,
which rewrites the Secret and the non-secret
`fs2.nebius.ai/artifact-store-credential-revision` pod annotation so the control
plane restarts. Workers receive short-lived signed handles, never a static
credential.

Storage-side lifecycle rules abort incomplete multipart uploads and expire
noncurrent versions after one day and remove expired delete markers. Nothing
expires a current object: `retention_days` reaches the control plane as an
application retention window instead.

`egress_cidrs` accepts only exact `/32` or `/128` object-storage addresses, so
the allowlist opens the control plane's default-deny egress no wider than the
endpoint it must reach.

The store is independently deployable. `scientific_batch` stays false until the
batch controller successors are integrated, and enabling the store neither
requires nor enables academic execution. See
[scientific-artifacts/README.md](scientific-artifacts/README.md) for the layout
module, the chart seam and the live signed-handle smoke test.

### Scientific execution map

Enabling `scientific_batch` does not require copying generated stage bindings
into `terraform.tfvars`. The root automatically loads
[`catalog/runtime/contracts/scientific-execution-map.json`](catalog/runtime/contracts/scientific-execution-map.json)
and carries that object unchanged through the generated workloads input to the
control-plane chart. `effective_configuration.scientific_batch` reports the
source and exact Helm-byte SHA-256 for review.

`scientific_batch.execution_map` remains available only as an advanced
object override. When batch is enabled, Terraform refuses an empty or malformed
map, duplicate model IDs, a map digest that differs from any included committed
workload profile's `qualification.execution_map_sha256`, or an execution
identity that differs from that profile. Regenerate and review the execution map
and profiles together before using the override; hand-editing generated fields
is intentionally not a supported activation path.

`scientific_batch.runtime_cache` optionally provisions a Terraform-owned
ReadWriteMany claim named `fs2-scientific-runtime-cache` in every workload
namespace whose generated execution-map stages mount the cache. It contains only
disposable runtime data such as JAX/XLA compiled kernels; immutable model
artifacts and customer inputs remain on their dedicated read-only and
content-addressed planes. A stage can consume the cache only through the
generated, writable `/cache` binding. Terraform rejects enabling the claim with
no declared consumer, using a different claim/path, or declaring a consumer
while the feature is disabled. The claim is not assigned a pod `fsGroup`, so a
large cache does not trigger a recursive ownership walk on every cold start.
Each namespace-local claim receives the same bounded ownership bootstrap with
`CAP_FSETID` and exact mode `02770`; the bootstrap prepares only the first-level
cache directories used by models in that namespace.

### Live model ownership and bootstrap

For a new cluster, `dynamic_models.workload_owner = "controller"` makes
Terraform omit qualified per-model serving objects and static routes; the
controller creates them from the initial desired revisions instead. Models
missing any exact artifact, retained runtime, base catalog, accelerator, or
renderer evidence remain statically Terraform-owned and are reported in
`dynamic_model_contract.ineligible_models`. The solution derives
immutable GPU-neutral pool envelopes and renderer bundles from the selected
models and accelerator pools, then seeds only
`bootstrap_model_ids` through the authenticated admin preview/apply API. Each
seed therefore has a PostgreSQL revision, ETag, audit event, and Kubernetes
projection; Terraform does not write a bare `ModelDeployment` CR. A completed
bootstrap Job preserves an existing desired revision instead of overwriting a
later admin change.

```hcl
models = {
  selection = "explicit"
  enabled   = ["cosmos3-nano", "qwen3-8b"]
  scaling   = { mode = "keda", hot = ["qwen3-8b"] }
  pool_overrides = {
    "cosmos3-nano" = "h100-reserved-8x"
    "qwen3-8b"     = "h100-reserved-8x"
  }
}

dynamic_models = {
  enabled             = true
  writes_enabled      = true
  workload_owner      = "controller"
  bootstrap_model_ids = ["cosmos3-nano", "qwen3-8b"]
  fresh_install       = true
}
```

The checked-in H100 qualification for this example is
[`h100-qwen-cosmos-elasticity-qualification-20260902.json`](catalog/profiles/evidence/h100-qwen-cosmos-elasticity-qualification-20260902.json).
It records passing shared-cache scale-to-zero cycles for the exact Qwen3-8B and
Cosmos 3 Nano revisions on `nvidia-h100-sxm5-80gb` in the
`h100-reserved-8x` pool. Activation to model Ready was 134.26 seconds for Qwen
and 91.169 seconds for Cosmos. These are shared-cache results, not GPU-snapshot
results, and another model, image, accelerator, cache tier, or pool tuple needs
its own qualification.

For an existing Terraform-owned model deployment, do not set
`fresh_install = true`. Use the explicit three-stage handoff, applying each
stage before continuing:

1. Enable the controller with `workload_owner = "terraform"` and
   `writes_enabled = false` to install and review the derived contracts while
   current workloads remain unchanged.
2. Drain model traffic, set `workload_owner = "released"`, and apply. This
   removes only Deployment, Service, ServiceAccount, and ConfigMap objects for
   the exact qualified controller subset plus their Terraform-owned
   ScaledObjects. Shared-cache PVCs remain Terraform-owned and are retained, so
   already localized weights survive the bounded serving-resource cutover. It
   also retains unqualified models and Terraform-owned NetworkPolicies, and
   keeps controller writes off.
3. Read the non-secret workloads output `dynamic_model_handoff_receipt`. Set
   `workload_owner = "controller"`, `writes_enabled = true`, the desired
   `bootstrap_model_ids`, and `handoff_receipt` to that exact value, then apply.

The release step is deliberately explicit and may cause a bounded service
interruption. The current controller does not claim live Terraform objects: its
adoption verifier remains fail-closed, so the solution never enables two SSA
writers for one resource. A future receipt-backed, UID-preserving Claim flow
can replace this conservative cutover without changing the tfvars model.
If the controller apply has not created a desired revision, rollback is simply
`workload_owner = "terraform"`, writes/bootstrap/fresh/receipt cleared, then an
apply to recreate the static objects. After a desired revision exists, returning
that model identity to Terraform ownership is not automated in this release;
retain controller ownership rather than creating a second writer. Before
removing a model from the selected catalog, drain and disable its live desired
revision. Terraform intentionally does not interpret catalog removal as
authorization to mutate durable live configuration. Hard deletion remains
disabled.

`models.selection = "profile"` enables every route in `profiles.models` and
requires an empty `models.enabled` set. `models.selection = "explicit"` enables
only the listed routes, all of which must belong to that profile. A hot model
also needs positive maximum capacity in a compatible selected pool.

Model placement is catalog-derived by default. For a custom or heterogeneous
pool, `models.pool_overrides` selects the exact pool without changing HCL; the
workload stage rewrites the Deployment selector, architecture, and toleration
from that pool contract. Per-model KEDA maxima may be greater than one, but
cannot exceed the GPUs available across compatible pools at their configured
`max_nodes` ceilings.

After controller ownership is enabled, an admin may select multiple compatible
Terraform-declared `poolRefs` for one model. The renderer keeps the hot floor on
exact regular/reserved pool selectors (and rejects an oversized regular floor),
then creates independently bounded
KEDA segments on exact preemptible pool selectors; regular overflow is a
separate segment only when the requested global ceiling needs it. The Service
selects all segments, while controller status aggregates them. No two writers
own the same Deployment scale subresource, and the sum of the fixed floor and
all segment maxima is exactly `maxReplicas`. Single-pool configurations retain
the conventional one-Deployment behavior. See
[Dynamic model configuration](DYNAMIC_MODEL_CONFIGURATION.md#implemented-heterogeneous-elasticity).

`local_nvme = true` requests host-local disks. With
`local_nvme_mode = "kubelet-ephemeral"`, Managed Kubernetes combines them into
kubelet ephemeral storage, so model `emptyDir` and runtime caches consume the
local devices. `local_nvme_mode = "raw"` leaves the NVMe devices unformatted
for an explicit snapshot or cache owner. Local disks are ephemeral and are
never the durable source of truth; models must still be recoverable from a
pinned object store, shared filesystem, OCI artifact, or upstream model
revision.

For a pool with `min_nodes = 0`, the Nebius managed autoscaler evaluates a
synthetic node before host-local NVMe exists and derives its schedulable
ephemeral capacity from `boot_disk.size_gib`. The root Terraform plan derives
each selected Deployment's cataloged effective `ephemeral-storage` request and
rejects a pool whose conservative synthetic budget cannot fit it. A regression
test derives that catalog value from the Pod spec, including init containers
and restartable sidecars, so onboarding cannot leave it stale. The budget is
80% of the configured boot disk minus 32 GiB for filesystem and system-workload
headroom. This does not replace local NVMe or reduce the workload reservation:
size the boot disk for the scale-from-zero decision and keep the full `emptyDir`
request for the real node. The GLM-5.2 example uses a 2048 GiB boot disk for its
768 GiB localization request and additional image/runtime margin.

Kueue, the MCP route, and the admin console are bundled platform components in
interface version 1. `terraform.tfvars` supplies immutable control-plane,
admin-console, and selected model image digests; admin provenance is a typed
source-commit, source-tree, and CycloneDX SBOM identity.

`inference-stack` also derives the admin configuration baseline directly from
the selected models, scaling settings, and live accelerator-pool contract. On
every deployment the control plane durably adopts a changed baseline as a new
configuration revision with actor `terraform-baseline`; changing tfvars after
the first deployment does not require a separate admin plan or apply receipt.
Repeated starts with the same baseline are idempotent. The authenticated admin
plan/reconcile path remains available when an operator wants a reviewed
Terraform handoff, reconciliation status, history, or rollback target; only
that optional path supplies `admin_configuration_*` receipt fields.

The `full_catalog` model profile currently contains these 16 canonical routes:

- `boltz2`
- `cosmos3-nano`
- `diffdock`
- `evo2-40b`
- `genmol`
- `glm-5-2-fp8`
- `molmim`
- `msa-search-pdb70`
- `nv-reason-cxr-3b`
- `nv-segment-ct`
- `openfold2`
- `openfold3`
- `proteinmpnn`
- `qwen3-8b`
- `rfdiffusion`
- `sdxl`

See [the zero-hot B300 example](examples/b300-zero-hot.tfvars) for the complete
catalog with no always-on GPUs. The supported
[heterogeneous example](examples/heterogeneous.tfvars) binds GLM to an 8-GPU
B300 pool and Qwen to an H100 pool entirely through tfvars; adapt its provider
identifiers to the target region and run the live preflight. The separate
[negative fixture](examples/heterogeneous-unqualified.tfvars) proves that a
legacy catalog profile with no concrete pool declarations is still rejected.

The secret-free [live deployment acceptance history](LIVE_ACCEPTANCE.md)
records the dated B300/H100 exercises, the current retained H100 topology, and
the measured qualification boundaries.

## Destroy

Destroy uses the reverse dependency order: workloads, foundation, then
infrastructure. It skips absent states and retains local evidence under the run
directory. Destroy never invokes `crane` or changes registry contents outside
Terraform's removal of the run-owned target registry.

```bash
NEBIUS_PROFILE=sandbox ./inference-stack destroy --var-file terraform.tfvars
```

Review the output and verify `status` before separately removing retained local
evidence. The wrapper never deletes the run directory automatically.

The portable default is
`storage.reference_data.lifecycle.retention_mode = "disposable"` with
`filesystem.forbid_deletion = false`. It selects the normal reverse-order
workloads, foundation and infrastructure teardown and reports
`completion: "complete"` only after all three stages succeed. A versioned bucket
containing any object or old version is not promised deletable and must fail
provider teardown rather than be reported destroyed.

With the explicit `retention_mode = "retain"` and `forbid_deletion = true`
opt-in, destroy is a downstream-stage release, not a full stack destroy. It
removes only workloads and foundation, leaves the entire infrastructure state
untouched, and emits
`status: "infrastructure-retained"` with
`completion: "full-stack-destroy-incomplete-infrastructure-retained"` plus exact
filesystem, bucket, CPU-pool and last-applied status/pipeline identities. The
same non-secret receipt is written to
`<run-root>/reference-data-retention.json`. Its adoption section is the gate:
move the protected filesystem and versioned bucket to a separately owned durable
state before attempting infrastructure teardown. The wrapper never reports a
complete destroy while those protected resources remain.

`storage.scientific_artifacts.lifecycle.retention_mode` behaves the same way and
is reported independently. Retained results also hold the infrastructure stage
back, and the receipt is written to
`<run-root>/scientific-artifacts-retention.json` when the result store is the
only retained storage.

## Acceptance checks

These tests are offline: they exercise the provider-free facade and mock cloud
and Kubernetes commands in the staged wrapper.

```bash
terraform fmt -check -recursive .
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

They gate one-file normalization, full-catalog B300 scale-to-zero, custom
heterogeneous pools, rejection of unqualified legacy pool profiles,
deterministic private stage files without literal secrets,
apply/plan-resume/destroy ordering, shipped examples, and the absence of quota
or limit-raising code paths.

`inference-stack validate` also initializes and validates all three stage roots. The
legacy exact-address plan verifiers remain maintainer acceptance gates for the
two B300 fixtures. Custom and heterogeneous pools use the generic v2 contract,
live provider preflight, exact per-model pool binding, and staged Terraform
preconditions during every plan.

After a deployment, [`acceptance/live-surface`](acceptance/live-surface/README.md)
re-accepts the public surface from the outside with a value-suppressed
receipt: TLS trust, public pages, exact release digests, Kueue, the admin
backend, both MCP catalogs, the OpenAI and HTTP scientific catalogs, and one
real chat completion. [`acceptance/scientific-fleet`](acceptance/scientific-fleet/README.md)
runs the ten scientific profiles end to end and the cold-start campaign that
[Live acceptance](LIVE_ACCEPTANCE.md) records.

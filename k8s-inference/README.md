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

The cluster remains running after `apply` so it can be tested. It is removed
only by an explicit `destroy`.

## Accelerator and qualification boundary

`deployment.accelerator_pools` is an open map. Each entry declares the Nebius
platform and preset, GPU resource name, host architecture, capacity mode,
driver owner, topology, node floor/ceiling, and optional local storage. Those
provider identifiers are intentionally not hard-coded in HCL, so the same
solution can describe H100, H200, B200, B300, GB300, RTX PRO 6000 Blackwell,
heterogeneous clusters, and future Nebius shapes.

`inference-stack preflight` checks the selected platform, preset, GPU count,
preemptible support, OS, driver preset, and Kubernetes version against the live
target project before apply. `models.pool_overrides` explicitly binds a model
to one of those pools and Terraform verifies its GPU count and scheduling
contract. That explicit binding is an operator compatibility assertion; live
provider validation cannot prove that an arbitrary model runtime supports a
new GPU architecture. Keep model/runtime qualification evidence with any new
binding.

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

Prerequisites are Terraform 1.10 or newer (but older than 2.0), `kubectl`,
[`crane`](https://github.com/google/go-containerregistry/tree/main/cmd/crane),
Git, and authenticated Nebius CLI access to the target project. Authentication is
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
NEBIUS_PROFILE=sandbox ./inference-stack proxy --var-file terraform.tfvars
```

After a deployment, `apply`, `status`, and `output` print the two
customer entry points as top-level JSON fields. `output` is the narrowest
machine-readable interface because it emits only these fields:

```json
{
  "mcp_endpoint_url": "https://<allocated-public-ip>/mcp",
  "admin_web_interface_url": "https://<allocated-public-ip>/admin/"
}
```

These values come from the exact non-secret Terraform outputs in the workloads
state. They are resolved only after the infrastructure stage has allocated the
edge address. An `internal-only` deployment emits loopback URLs instead; those
are usable only while the run-scoped operator proxy from the workloads
`port_forward_contract` is active. The wrapper reads only these two named
outputs; it does not enumerate or print sensitive workload outputs.

Each internal-only deployment may reserve its own loopback tuple in the same
customer tfvars file. The defaults remain `18080`, `18081`, and `18082`; use a
different tuple for another concurrently operated cluster:

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

The current public-edge fixture uses the disposable staging IP-ACME issuer, so
its URL is an acceptance endpoint rather than a browser-trusted production
hostname. `internal-only` does not expose a public listener. Run
`inference-stack proxy` after `apply` to start the two run-scoped Kubernetes
port-forwards and the same-origin loopback proxy described by
`port_forward_contract`. Keep that foreground process running while using the
emitted MCP and admin links; stop it with `Ctrl-C`. Separate deployments can run
concurrently when their `edge.port_forward_ports` tuples differ. These loopback
URLs are reachable only from the machine running the proxy; use an SSH tunnel
for remote testing or select `edge.mode = "public"` for a shared endpoint.

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

Use the admin interface's **Access / API keys** area to issue a revocable PAT
with only the required models, scopes, concurrency, request, and GPU-time
budgets. The bootstrap admin credential is a sensitive workloads Terraform
output and is intentionally excluded from `inference-stack output`; retrieve it
only from the protected run state for the initial operator handoff. Store a new
PAT in an owner-only (`0600`) file because its value is returned once. Clients
send it as `Authorization: Bearer <PAT>` to the printed OpenAI-compatible and
MCP endpoint. Never put the bootstrap credential or PAT in tfvars, source,
shell history, Task Deck cards, or acceptance receipts.

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
| `cluster` | Kubernetes version, API CIDR allowlist, and optional regular CPU system-pool shape. |
| `accelerator_pools` | Open map of GPU platform/preset, capacity, topology, driver, local-storage, and node-floor/ceiling settings. |
| `models` | Profile or explicit selection, KEDA/static scaling, hot-model floor, and per-model scaling overrides. |
| `storage.shared_cache` | Optional shared model-cache size/type/block-size override. |
| `artifacts.external_registry_ids` | Same-tenant registries whose immutable images need run-scoped node-pull viewer access. Terraform creates a project-scoped reader group beside each registry, including registries in another project or region. |
| `artifacts.registry_policy` | Defaults to `regional-mirror`; optional prefix controls the target repository namespace. `direct-source` is an explicit opt-out that leaves runtime pulls pointed at upstream registries. |
| `edge` | `internal-only` or bounded public ingress configuration, including an optional per-cluster loopback port tuple. |
| `observability` | DCGM cold-start campaign and optional external Grafana publication. |
| `secrets` | Environment-variable names and Kubernetes Secret key references, never secret values. |
| `acceptance` | Optional post-deployment probe job. |

For a checked-in target, `target.project_id` and `target.region` are sufficient.
For another project, add `project_name`, `network.network_name`,
`network.subnet_name`, `network.private_subnet_cidr`, and
`system_update_strategy`. The infrastructure stage verifies those facts through
the provider. Custom accelerator pools additionally go through the live
platform and Managed Kubernetes compatibility preflight.

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

The `full_catalog` model profile currently contains these 15 canonical routes:

- `boltz2`
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

The secret-free [dual preemptible-cluster acceptance](LIVE_ACCEPTANCE.md)
records the tfvars-only B300/H100 deployment exercise and its current provider
limitations.

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

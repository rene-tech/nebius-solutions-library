# Kubernetes inference infrastructure stage

This Terraform root provisions the Nebius resources required by the
`k8s-inference` solution. It is an implementation stage, not the customer
entrypoint. Run it through [`inference-stack`](../../inference-stack), which
generates its inputs, gives it an isolated backend and Terraform data
directory, and sequences the later foundation and workload stages.

## Ownership

The stage discovers the selected project, VPC, and subnet, then creates
run-owned resources:

- a Nebius Managed Kubernetes control plane and regular CPU system pool;
- one node group for each selected accelerator pool or NVLink rack;
- the node-pull service account and narrowly scoped, project-local
  registry-reader groups;
- a cluster-regional runtime artifact mirror, shared model-cache filesystem, and worker security
  group;
- when enabled, exactly seven reference-data resources: one lifecycle-selected
  filesystem, one lifecycle-selected versioned bucket, a writer service account,
  writer group and membership, a MysteryBox-backed access key, and a dedicated
  tainted regular-CPU node group; and
- an optional public IPv4 allocation when public edge mode is selected.

It does not raise quotas, change service limits, or adopt resources from
another deployment. Capacity and quota failures are returned to the operator.
Each external registry is resolved by ID and receives a reader group in its
own project. This permits same-tenant, cross-project image pulls without a
tenant-scoped group or tenant-level IAM write.

## Inputs and accelerator pools

The wrapper derives this stage's variables from the top-level
`deployment` object. Target project, region, network, subnet, system-pool,
cache, edge, and accelerator-pool values are written to a private generated
tfvars file under the selected run root.

For internal-only deployments, `edge.port_forward_ports` selects three
distinct non-privileged loopback ports for the control plane, admin console,
and same-origin operator proxy. Its defaults are `18080`, `18081`, and `18082`.
The wrapper carries an alternate tuple from the customer tfvars into the edge
handoff, allowing multiple clusters to be operated concurrently without local
listener collisions.

Accelerator pools carry the Nebius platform and preset unchanged to the
provider. The facade catalogs every current Nebius GPU platform and also
permits future platform and preset identifiers. A live preflight verifies that
the requested platform, preset, capacity type, Kubernetes version, OS, and
driver preset are compatible in the target region before apply. Model
placement stays explicit: catalog defaults may be replaced per model with a
named pool override, including in heterogeneous clusters.

`local_nvme = true` requests host-local disks. Set `local_nvme_mode` to
`kubelet-ephemeral` when kubelet `emptyDir` and runtime caches should consume
the devices, or to `raw` when a snapshot or cache component will own and mount
them itself. These devices are ephemeral and never replace the configured
durable model source.

The Nebius provider uses the same source identity as the rest of the Solutions
Library:

```text
terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius
```

## Handoff

The stage emits the cluster identity, accelerator node-group IDs, the resolved
accelerator-pool contract and digest, canonical registry-delivery contract,
edge contract, capacity contract, and run-owned resource IDs. The delivery
contract distinguishes upstream sources and promotion traffic from the
regional target registry and node runtime pulls. `inference-stack` writes only the required non-secret
values into the next stage's private tfvars file. Later stages must match the
accelerator contract digest before treating capacity as effective.

## Validation

From the repository root, source validation requires no backend or cloud
credentials:

```bash
terraform -chdir=k8s-inference/stages/infrastructure fmt -check -recursive
export TF_DATA_DIR="$(mktemp -d)"
terraform -chdir=k8s-inference/stages/infrastructure init -backend=false
terraform -chdir=k8s-inference/stages/infrastructure validate
```

To validate the complete provider-free facade and all three stage roots with
an example configuration:

```bash
./k8s-inference/inference-stack validate \
  --var-file k8s-inference/terraform.tfvars.example \
  --run-root "${XDG_STATE_HOME:-$HOME/.local/state}/nebius-k8s-inference/validation"
```

`validate` initializes providers and evaluates configuration, but creates no
cloud resources. Use a new, protected run root for each deployment and keep it
for the entire `plan`/`apply`/`status`/`destroy` lifecycle. Terraform state can
contain provider and resource data even when output values are marked
sensitive.

The acceptance verifiers fail closed on the exact managed-resource address,
Terraform type, action, and count for disabled, retained, and disposable
reference-data modes. `tests/verify_state.py` accepts either `terraform state
list` text or provider state JSON from `terraform show -json`; JSON is preferred
because it verifies the provider-reported resource types rather than inferring
them from addresses. Sanitized enabled-mode plan/state inventories live in
`tests/fixtures/reference-data-*.provider-{plan,state}.json`, and
`tests/gpu_contract.tftest.hcl` exercises the same seven-resource graph through
the mocked Nebius provider, including an empty disposable apply/teardown.

## Lifecycle

The supported lifecycle is:

```bash
./k8s-inference/inference-stack plan --var-file k8s-inference/terraform.tfvars
./k8s-inference/inference-stack apply --var-file k8s-inference/terraform.tfvars
./k8s-inference/inference-stack status --var-file k8s-inference/terraform.tfvars
./k8s-inference/inference-stack destroy --var-file k8s-inference/terraform.tfvars
```

Destroy runs workloads, foundation, and infrastructure in reverse dependency
order. It never removes the run directory automatically; retain or dispose of
that evidence according to the operator's state-handling policy.

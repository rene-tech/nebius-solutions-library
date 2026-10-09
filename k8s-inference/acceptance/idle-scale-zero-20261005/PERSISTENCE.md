# Read-only root persistence check, 2026-10-05

Later bounded follow-up: [ADMIN_RELEASE_INPUTS.md](ADMIN_RELEASE_INPUTS.md)
verifies the current admin repository/digest and actual source/tree/CycloneDX
provenance and prepares an exact nested-block correction. Parent has separately
updated the API/zero-floor/RDMA inputs. The older capture below remains dated
evidence, not the current values for those fields. No private input was changed
by the admin follow-up; retained-App/H200 ownership remains a distinct residual.

## Outcome

The retention seam is source-tested in `84dd01ac0`, but the **current saved root
inputs are not safe to replay wholesale**. They lack both the retained-App input
and H200 infrastructure ownership, and still pin September21 application images.
No active tfvars, state, pool, quota or workload was changed by this check.

The review-only, non-executable candidate/diff is retained privately at:
`/home/tux/secure-handoff/fs2-idle-scale-zero-20261005/persistence-review-candidate.json`.
It includes whitelisted live group attributes, image/map identities, content
hashes, input hashes and exact proposed import identity. It is **not** a tfvars
file and must not be passed to Terraform. No credentials or full state copies
are embedded.

Authoritative root input:
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/terraform.tfvars`.
Generated stage input `run/workloads.tfvars.json` is dated2026-09-21 and must be
regenerated, not treated as current intent. The root's approved October5
`h100-reserved-8x` GPU-cluster/fabric-2 edit remains untouched.

## Exact existing H200 group and missing ownership

The live group is `mk8snodegroup-e00prf74rw5vnbqcxq`, name
`fs2-wan2-h200-1x-20260920`, in cluster `mk8scluster-e00j5z9te7x5dd9g6a`.
Both existing nodes are retained. Read-only searches found **no matching resource
ID in any of the four authoritative states**: infrastructure `run/terraform.tfstate`
(serial249), foundation810, workloads2840, configuration156. This is a statement
about these states, not an assertion that no historical task state exists elsewhere.

| Field | Actual live group | Current generic-root discrepancy |
| --- | --- | --- |
| Size and reservations | Fixed2; reservation policy FORBID | Generic GPU resource ties FORBID to autoscaling, not fixed sizing |
| Name | `fs2-wan2-h200-1x-20260920` | Derived root name would be `k8s-inference-h100-wan2-h200-1x` |
| Boot disk | 512GiB NETWORK_SSD | Root/example default is not the observed512GiB |
| Host storage/init | No filesystem attachments or cloud-init | Shared/reference filesystem defaults would add attachments/init |
| Template labels | Existing exact class/pool/type/taint selectors | Root adds its normal generated labels; preserve/review every difference |
| Other attributes | H2001GPU, CUDA13, Ubuntu24.04, Kubernetes1.35; surge1/unavailable0/drain1800s | Must compare exact subnet, service account and template, not merely GPU SKU |

The existing schedulable capacity is15900mCPU/190072MiB per node, two GPUs total.
The already-installed Kueue declaration and content source are
`models/general-media/wan2-h200-existing-pool.json`; no capacity is added by
retaining this source. Wan's PVC access does not imply this group has host-level
filesystem attachments.

The proposed declarative import identity, **not executed**, is:

```hcl
import {
  to = nebius_mk8s_v1_node_group.gpu["wan2-h200-1x"]
  id = "mk8snodegroup-e00prf74rw5vnbqcxq"
}
```

Target root: `k8s-inference/stages/infrastructure`; existing backend file:
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/terraform.tfstate`.
The checked-in provider lock is0.5.276. Import support/normalization and the
complete resulting plan must be checked with that exact provider before any
authorized state mutation; no no-op plan is claimed here.

**Import alone is insufficient.** First make the existing generic configuration
able to represent this group's fixed/FORBID sizing and exact name/template
without replacement. Do not select AUTO reservations to sidestep that mismatch,
silently convert count mode, use broad ignore-changes, or create another H200
group. Provider documentation warns that ordinary template updates can roll
nodes; matching GPU count alone is not a no-disruption check.
[Nebius node-group provider reference](https://github.com/nebius/terraform-provider-nebius/blob/main/docs/resources/mk8s_v1_node_group.md)

## Release inputs that must remain current

The root and September21 stage input pin API digest`ab2f8027…` and old admin
digest`6428b350…`. At this capture the live source-compatible fleet used:

- API and scientific-tools image`64c5c77d…`; model controller`fb6d3209…`.
- Admin image`de4df56d…` from the `fs2-platform/admin-console` repository,
  not the old root repository. Its matching provenance must be preserved, not
  paired with the old September21 SBOM/source metadata.
- MindEval worker`ee6eea4a…`, outside this retention file's ownership.
- Envelope`fs2-idle-envelope-21ef2f1eac8f`;
  bundles`fs2-idle-bundles-37bb372fe37d`;
  lean routes`fs2-idle-routes-1a1ea92b8529`.
- Scientific scheduling`fs2-scientific-scheduling-8c76afc02ad5`;
  execution`fs2-r927c465c6d-scientific-execution-64d5416c8f6d`.

Full digests, current generations and map-body hashes are in the private
candidate. Re-read these immediately before a later integration because sibling
releases can advance them. The retention file contains the eight reproducible
bundle bodies and hash-bound source runtime records; the execution map must
likewise come from the parent release's retained source, not just an ephemeral
ConfigMap name. Keep sibling route-attestor/bindings/admin maps unchanged.

After existing H200 ownership is explicitly reconciled, add the absolute
`deployment.dynamic_models.retained_registration_file` path, regenerate stage
contracts, and review a **non-mutating** plan/render. The retained extension must
keep all eight exact references, all current sibling Apps, the existing H200
pool and zero floors, with no new bootstrap/static owner. Activation needs its
own coordinated exact-release verification. This check prepared that handoff;
it did not run import, apply, state migration, a provider plan, or new inference.

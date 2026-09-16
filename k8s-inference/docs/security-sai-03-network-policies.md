# Model-runtime network isolation

Model runtime isolation is owned by Terraform rather than by the dynamic model
controller. This keeps policy names and contents finite even when customer Apps
use arbitrary UUIDs.

## Policy profiles

The `fs2-models` namespace has a Terraform-owned ingress-and-egress default
deny. Runtime Pods select one immutable finite profile with the
`fs2-serve.nebius.ai/network-profile` label:

- Mounted-content runtimes accept gateway traffic on their declared service
  port and have no egress rules.
- Standard runtimes additionally reach cluster DNS.
- ModelExpress runtimes additionally reach same-profile transfer peers and an
  exact qualified coordinator host and port.

Profile names are derived from canonical configuration, never an App name or
UUID. ModelExpress coordinator allowlists accept only IPv4 `/32` and IPv6
`/128` routes. Broad prefixes, default routes, and split-default aggregates are
rejected in both Python and Terraform validation.

The academic scientific namespace has its own default deny and a bounded
workload policy. It permits only cluster DNS, the internal API, and configured
exact object-store hosts. Reference-data policies remain independently owned by
the reference-data module.

## Controller boundary

The model controller renders no `NetworkPolicy`, exposes no NetworkPolicy API
endpoint, and receives no NetworkPolicy RBAC rule. It also does not create
ServiceAccounts or DaemonSets. Dynamic runtime Deployments use the dedicated,
non-token-mounted ServiceAccount provisioned by Terraform.

Arbitrary App create, update, stale-resource deletion, and finalizer cleanup use
only Deployment, Service, and ScaledObject permissions. The HTTP integration
test makes every NetworkPolicy request fail and verifies that the complete App
lifecycle still succeeds without attempting one.

## Offline content acquisition

Runtime Pods do not acquire model content from the Internet. An operator-owned,
separately reviewed acquisition workflow must place the exact locked revision
on durable storage before a runtime starts. Runtime init containers set the
offline Hugging Face and Transformers modes and fail closed when the pinned
snapshot is absent.

KServe and NIM Operator adapters remain disabled until there is evidence that
each operator propagates the immutable profile label to every child Pod. The
native adapter is the supported path.

## Verification

From `k8s-inference`, run:

```bash
uv run --project components/control-plane --frozen \
  pytest components/control-plane/tests -q

uv run --project components/control-plane --frozen pytest \
  catalog/runtime/tests/test_kubernetes_adapters.py \
  models/general-media/tests/test_shared_cache_localization.py -q

terraform -chdir=modules/academic-assets init -backend=false -input=false
terraform -chdir=modules/academic-assets validate -no-color
terraform -chdir=modules/academic-assets test -no-color

terraform -chdir=stages/workloads init -backend=false -input=false
terraform -chdir=stages/workloads validate -no-color
terraform -chdir=stages/workloads test -no-color
```

A shared rollout must use a reconciled integration branch and a serialized
deployment slot. Capture the then-current stable revision and image digests,
verify request debugging is disabled, and use only that slot-time state as the
rollback target. Apply finite allow profiles before the namespace default deny;
remove the default deny first if rollback is required.

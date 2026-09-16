# SAI-03 model-runtime network isolation

Status: corrective source successor prepared after independent review rejected
`692a22ccb0cf56be61ca0227646bcd4d4a896046`; production rollout remains
intentionally gated. The rejected commit is preserved as negative evidence and
must not be deployed.

This change closes the source-side causes of SAI-03 without relying on runtime
pods to carry the historical `app.kubernetes.io/instance` label:

- `fs2-models` is Terraform-owned with an ingress-and-egress `default-deny`.
  Terraform first creates a bootstrap allow policy for every selected catalog
  route, so applying the namespace boundary cannot race ahead of model access.
- The model controller emits one owner-referenced `NetworkPolicy` for every
  rendered `Deployment`. Serving ingress is limited to the canonical
  `fs2-system` gateway pods and the deployment's service port. Ordinary egress
  is limited to cluster DNS. A qualified ModelExpress deployment adds only its
  exact transfer group and coordinator peer/port.
- Native catalog workloads carry the same gateway/DNS boundary. Mounted-content
  workloads keep their stricter zero-egress startup contract.
- The two legacy static policies select stable model-runtime/model-id labels
  that are now present on the actual Pod templates. Their init containers are
  explicitly offline and fail a cold-cache preflight when the pinned snapshot
  is absent; they never fall back to an Internet download.
- `fs2-academic-poc` gains a Terraform-owned ingress-and-egress `default-deny`.
  Terraform creates `academic-scientific-workloads` first. It selects the
  `fs2.nebius.ai/workload-id` copied to Job and every JobSet child Pod and allows
  only cluster DNS, the exact internal control-plane API, and configured exact
  `/32` or `/128` object-store destinations needed by materialize/collect.
- `fs2-reference-data` already has Terraform-owned `default-deny` and `allow-dns`
  policies; its existing focused test remains part of this evidence packet.
- KServe and NIM Operator adapters now reject rendering because no reviewed
  source contract proves that either operator propagates the controller's
  NetworkPolicy selector to every child Pod. The native adapter remains the
  supported path until that evidence exists.
- ModelExpress external coordinator configuration rejects IPv4 and IPv6 default
  routes, even though they are syntactically canonical CIDRs.

## Offline cold-cache acquisition contract

Runtime Pods never acquire model content from the Internet. Before either
legacy static Deployment is created, an operator-owned acquisition step must
place the exact revision from its committed `model.lock.json` into that
Deployment's PVC/Hugging Face cache. Acquisition runs outside the default-denied
runtime identity, uses a reviewed digest-pinned plan, and must leave the cache
durable before handing the volume to the runtime.

The runtime init container sets `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`, calls `snapshot_download` exactly once with
`local_files_only=True`, and exits non-zero with `offline cold-cache
preflight failed` if the snapshot is absent. The Pod template annotation
`fs2.nebius/cold-cache-preflight: offline-prestaged-required` makes the rollout
gate machine-readable. Empty-cache startup therefore fails closed instead of
silently regaining Internet egress. The catalog acquisition pipeline remains a
separate privileged workflow; this change does not claim it can populate an
arbitrary legacy RWO PVC without a reviewed storage handoff.

## SAI-07 policy-name coordination

SAI-03 emits exactly `fs2-runtime-<workload-name>`, using the Kubernetes
253-character bound and a twelve-character SHA-256 suffix when truncation is
required. An AppDeploymentIdentity always has a segmented workload identity,
including a single-pool App, so its policies are named
`fs2-runtime-<deployment>-<hot|burst>-<pool>` rather than the base name.

Independent integration review found that SAI-07 candidate
`1351cb2c55b7bc607b55775ae8003a29acfa84c3` special-cased a single qualified
pool to only the base name. That candidate is therefore not compatible with
App policy update/delete and must not be integrated. The required SAI-07
successor allowlist is the bounded union of the base name and every hot/burst
name for every qualified pool for every dynamic model. The coordinator relayed
that contract to the active SAI-07 worker; independent integration review will
reconcile the two exact successors. SAI-03 does not integrate or modify the
SAI-07 branch.

## Pre-mutation live evidence

Read-only inspection used kubeconfig
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig`, context
`k8s-inference-h100`. The target is the retained `eu-north1` cluster
`mk8scluster-e00j5z9te7x5dd9g6a`. Namespace identities were:

| Namespace | UID | Manager |
| --- | --- | --- |
| `fs2-system` | `3cbaa3a3-d9ab-4d86-81ff-4db860ef3259` | Terraform |
| `fs2-models` | `4bef01b7-524f-4a2b-906d-54ab9a399f9c` | Terraform |
| `fs2-academic-poc` | `76de8795-bf56-44be-966c-46b8b1ded1b8` | Terraform |
| `fs2-reference-data` | `fef06d0a-c360-401f-bb5f-c4adc41ca360` | Terraform |

Before any mutation, `fs2-models` had only `cosmos3-nano`,
`qwen3-8b-b300`, and `fs2-serve-control-plane-scientific-workloads` policies.
The first two selected obsolete instance labels and the third selected batch
workload IDs; no namespace default deny existed. `fs2-academic-poc` had only
the offline-validation deny-egress policy. `fs2-reference-data` already had
`default-deny`, `allow-dns`, public staging opt-in, and status ingress policies.

The running control plane and model controller used image digest
`sha256:d41ffd9c0281058b7c7c5bd63d168d409fac259ea5cab2ccc690961ea831a386`.
Its recorded source revision is
`7e5e682d2b6981e1f978ab07bd8c09a6609838ad`; that revision is not an ancestor
of this task branch (task branch/deployed-source divergence was 5/31 commits at
inspection). Active MindGuard and voice Deployments also lacked the complete
model-runtime ownership labels. A shared rollout from this branch would
therefore overwrite deployed-only work and isolate active sibling workloads.

During final read-only inspection, Helm release `fs2-serve-control-plane` was
at revision 134 in `pending-rollback` to last successful revision 132 after
revision 133 failed on the GPU observer DaemonSet. The gateway Deployment was
still converging while the model-controller Deployment was available. No
resource was created, patched, deleted, or restarted by this task.

## Verification

Run from `k8s-inference` unless a command changes directory:

```bash
uv run --project components/control-plane --frozen \
  pytest components/control-plane/tests -q

uv run --project components/control-plane --frozen pytest \
  components/control-plane/tests/test_api_mcp.py \
  components/control-plane/tests/test_dynamic_routes.py \
  components/control-plane/tests/test_model_deployment_bridge.py \
  components/control-plane/tests/test_runtime_and_schema.py -q

cd catalog/runtime
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_kubernetes_adapters
cd ../..

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  models.general-media.tests.test_shared_cache_localization

terraform -chdir=modules/academic-assets init -backend=false -input=false
terraform -chdir=modules/academic-assets validate -no-color
terraform -chdir=modules/academic-assets test -no-color

terraform -chdir=stages/workloads init -backend=false -input=false
terraform -chdir=stages/workloads validate -no-color
terraform -chdir=stages/workloads test \
  -filter=tests/academic_assets_render.tftest.hcl -no-color

terraform -chdir=reference-data/terraform init -backend=false -input=false
terraform -chdir=reference-data/terraform validate -no-color
terraform -chdir=reference-data/terraform test \
  -filter=tests/bootstrap.tftest.hcl -no-color
```

Observed results for the corrective successor:

- complete control-plane suite: 1,962 passed, 98 skipped;
- changed controller/scientific focus: 156 passed, including the single- and
  multi-pool AppDeploymentIdentity create, update, stale-policy deletion, and
  finalizer-cleanup lifecycle;
- controller-specific renderer suite: 45 passed;
- catalog Kubernetes adapters: 19 passed;
- integrated gateway/model/MCP suite: 118 passed;
- academic-assets module: 15 passed;
- reference-data bootstrap: 6 passed;
- general-media focused suite: 5 passed;
- Terraform formatting/validation, Ruff lint/format, YAML policy assertions,
  and `git diff --check`: passed;
- Trivy 0.70.0 found zero High/Critical issues in all three changed Terraform
  files.
  Full legacy manifest scans still report the pre-existing read-only-root-filesystem
  findings on model containers; those are outside SAI-03 and were not hidden.

A server-side dry-run against the retained API had accepted both hardened
legacy policies and the namespace default deny at the rejected parent. It was
not repeated for this successor because shared-live reconciliation is still
required and no rollout or live mutation is authorized from this branch.

The complete catalog suite ran 163 tests and retained one unrelated baseline
error: `model-variants.json` currently contains 13 fallback candidates while
its schema fixes the count at 12. The changed Kubernetes-adapter suite is green.
The workloads test file's new policy run is green; a later pre-existing academic
chart run still fails the Kueue LocalQueue/fair-share fixture and skips its final
run. Neither failure touches a file changed for this remediation.

## Safe rollout and rollback

Do not deploy this task commit directly to the retained shared service. First,
an integration branch must contain the currently deployed source, this change,
and the completed MindGuard/voice sibling changes. Wait for the in-progress Helm
rollback to settle before planning anything.

The safe order is:

1. Record the settled Helm revision and both control-plane image digests. Build,
   scan, and sign the integrated controller image.
2. Upgrade the controller while leaving `fs2-models` without the default deny.
   Wait until every managed runtime Deployment has its exact controller policy
   and reconcile any static/sibling workload labels and policies.
3. Apply the Terraform bootstrap policies; verify their selectors and ports.
   Only then apply the namespace default deny and the academic default deny.
4. Prove positive gateway PAT/model-grant sync and streaming inference plus MCP
   model calls. Prove from a scratch pod that direct model `:8000` access fails,
   and from an isolated model pod that public HTTPS egress fails while DNS and
   gateway inference continue to work. Recheck operations, observability, and
   current sibling models.

If a customer flow regresses, remove only the affected namespace `default-deny`
first to reopen the prior path while retaining the additive allow policies and
evidence. Then roll Helm back to the recorded pre-rollout revision/image digest
and revert the Terraform resources using the reviewed saved plan. Never delete
all allow policies before removing the default deny.

No GPU/model behavior changed, so a new GPU inference campaign is not meaningful
before the integrated live rollout. No temporary cloud, Kubernetes, registry, or
GPU resources were created, and there is nothing for this task to clean up.

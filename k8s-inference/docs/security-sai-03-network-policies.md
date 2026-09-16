# SAI-03 model-runtime network isolation

Status: **NO-GO for integration and deployment.** Conflict-resolution commit
`92f9394cb3eb76b9b02f7682c96c056ae600e9ed` contains exact SAI-07 source
`385168566a74adf48f9624da2f7574d48e4f6ace`, which independent review rejected
on seven PSA, storage, transition, and cleanup blockers. Preserve `92f9394cb`
only as evidence that the four SAI-03/07 textual conflicts can be resolved; it
is not an accepted integrated candidate. Earlier rejected commits
`692a22ccb0cf56be61ca0227646bcd4d4a896046`,
`b7e5b12e8b31b9d055ec746c3c6cd691f3519874`, and
`8a81670f954960390744191271a030cb6e47ab23` also remain negative evidence. No
commit in this lineage has been deployed.

The separately reviewable SAI-03 root correction is
`4ea4b1260e6e682a2e4f40ee251860e3cfc7b679`, tree
`9d8c9169e5e6ba0c612205811eaa86cea11a11df`. It is the clean direct child of
rejected `8a81670f` and an ancestor of the conflict-resolution merge. Final
integration must start from `4ea4b126`, merge SAI-07's independently accepted
corrected successor as the second parent, re-resolve the contracts below, and
rerun both complete suites. Do not use `92f9394cb` as the integration base.

This change closes the source-side causes of SAI-03 without relying on runtime
pods to carry the historical `app.kubernetes.io/instance` label:

- `fs2-models` is Terraform-owned with an ingress-and-egress `default-deny`.
  Terraform first creates a finite set of allow profiles, so applying the
  namespace boundary cannot race ahead of model access. Base profiles are
  bounded by egress mode and service port. ModelExpress profiles are bounded by
  the exact qualification digest, accelerator class/count, NIXL backend and
  service port.
- Runtime Pods select those profiles with the immutable
  `fs2-serve.nebius.ai/network-profile` label. Arbitrarily named customer Apps,
  including `app-<uuid>` single- and multi-pool clones, reuse a canonical finite
  profile; no App name or UUID becomes policy authority.
- The model controller renders no `NetworkPolicy`, has no NetworkPolicy HTTP
  endpoint, and receives no NetworkPolicy RBAC verbs. A compromised controller
  therefore cannot create an allow-all policy. Serving ingress is limited to
  the canonical `fs2-system` gateway pods and the profile's service port.
  Ordinary egress is limited to cluster DNS. A qualified ModelExpress profile
  adds only same-profile transfer peers and its exact coordinator host/port.
- Native catalog workloads carry the same gateway/DNS boundary. Mounted-content
  workloads keep their stricter zero-egress startup contract.
- The two legacy static policies select labels that are present on the actual
  Pod templates; the same templates carry their finite Terraform profile.
  Qwen is in the zero-egress profile and its checked-in policy has `egress: []`.
  Cosmos uses the DNS profile bound to its real gateway port, `8080`. Their init
  containers are explicitly offline and fail a cold-cache preflight when the
  pinned snapshot is absent; they never fall back to an Internet download.
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
- ModelExpress external coordinator configuration accepts only canonical IPv4
  `/32` or IPv6 `/128` hosts at both Python and Terraform boundaries. Default
  routes, split-default IPv4 `/1` pairs, and broad IPv6 prefixes are rejected.
  The academic and scientific-artifact object-store inputs use the same
  address-family-aware exact-host rule.

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

## SAI-07 authorization coordination

Per-deployment policy names are no longer part of the controller contract.
SAI-07 must remove the entire NetworkPolicy resource rule from the controller
Role: no `create`, `patch`, `update`, or `delete`, and no allowlist of
`fs2-runtime-*` or unused `fs2-modelexpress-*` names. The real HTTP-client App
lifecycle test makes every NetworkPolicy request return `403` and proves that
arbitrary UUID creation, update, owned stale-resource deletion, and finalizer
cleanup still succeed without making such a request. It also proves an existing
App owned by another identity remains byte-for-byte unchanged.

SAI-03 did not modify the SAI-07 branch or task files. Instead, the task branch
merged its exact reviewed source and resolved the combined contract below.

### Reusable SAI-03/SAI-07 conflict-resolution contract

Rejected conflict-resolution merge `92f9394cb` has these immutable parents:

- SAI-03 root-validation correction:
  `4ea4b1260e6e682a2e4f40ee251860e3cfc7b679`;
- SAI-07 source: `385168566a74adf48f9624da2f7574d48e4f6ace`.

The four textual conflicts were resolved as a bounded union, not by choosing
one branch wholesale:

| Conflict | Resolution that must remain true |
| --- | --- |
| `model_deployment.py` | Keep the finite Terraform-owned network-profile label derivation and SAI-07's hardened runtime service-account injection; render neither `NetworkPolicy` nor `ServiceAccount`. |
| `test_model_deployment.py` | Assert both finite profile labels and `fs2-model-runtime` with token automount disabled for single-/multi-pool renders. |
| `test_model_deployment_controller.py` | Keep the arbitrary UUID App lifecycle through the real HTTP client and the independent pre-I/O rejection of any attempted NetworkPolicy write. |
| `stages/workloads/academic_assets.tf` | Pass both the exact DNS/API/object-store policy contract and SAI-07's staged Pod Security enforcement flag into the module. |

This resolution shape requires the model controller to have no
NetworkPolicy endpoint or RBAC verbs and no authority to create ServiceAccounts, while
Terraform owns the finite policies and the single `fs2-model-runtime` identity.
Arbitrary App IDs therefore do not expand Kubernetes write authority. A merge
that drops either the finite profile label, hardened service account, academic
network inputs, or staged Pod Security input violates this contract.

Passing tests on `92f9394cb` prove only that this conflict resolution is
internally executable. They do not waive or close any independent SAI-07
finding. At the final read-only check, the SAI-07 remote still pointed to exact
rejected `385168566`; no corrected successor was available to integrate.

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

The independent final review of rejected `8a81670f` subsequently reported that
the live controller Role still has NetworkPolicy verbs and all 37 live runtime
Deployments still lack a finite profile label. That is authoritative evidence
that the live finding remains open; it is not promotion evidence for this
source successor. This task did not re-query or mutate the shared cluster.

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
  -filter=tests/modelexpress.tftest.hcl -no-color

terraform -chdir=reference-data/terraform init -backend=false -input=false
terraform -chdir=reference-data/terraform validate -no-color
terraform -chdir=reference-data/terraform test \
  -filter=tests/bootstrap.tftest.hcl -no-color
```

Observed mechanical results for rejected conflict-resolution tree
`92f9394cb` (not acceptance or promotion evidence):

- complete control-plane suite: 1,970 passed, 98 skipped;
- changed controller/renderer/Helm focus: 227 passed, including finite-profile
  derivation, real HTTP arbitrary-App lifecycle, and absence of NetworkPolicy
  RBAC;
- complete root deployment-contract suite: 77 passed with 78 subtests,
  including real root-plan rejection of the IPv4 `/1` equivalent-default pair
  and broad IPv6 prefixes while preserving IPv4 `/32` and IPv6 `/128` hosts;
- integrated gateway/model/MCP suite: 118 passed;
- ModelExpress Terraform contract: 11 passed, including exact cross-layer
  profile inputs, IPv4 `/1`-pair rejection, IPv6 `/32` and `/64` rejection, and
  IPv6 `/128` acceptance;
- academic-assets module: 18 passed, including separate IPv6 `/32` and `/64`
  rejection and `/128` acceptance;
- catalog Kubernetes adapters: 19 passed, including native selector matching
  and KServe/NIM fail-closed behavior;
- reference-data bootstrap: 10 passed;
- general-media offline-preflight/static-policy suite: 5 passed;
- Terraform validation and `git diff --check`: passed. Rejected `8a81670f`
  failed `terraform fmt -check -recursive` on four `server_image = null`
  alignments in the ModelExpress test; the exact integration tree formats those
  four lines and passes the recursive check. No formatting success is claimed
  for `8a81670f`.

Trivy 0.70.0 reported zero High/Critical findings in each changed Terraform
file. The two legacy model manifests retain two pre-existing High findings each
for writable root filesystems; this NetworkPolicy change neither introduced nor
concealed them.

A server-side dry-run against the retained API had accepted both hardened
legacy policies and the namespace default deny at the rejected parent. It was
not repeated for this successor because shared-live reconciliation is still
required and no rollout or live mutation is authorized from this branch.

The complete catalog suite ran 163 tests and retained one unrelated baseline
error: `model-variants.json` currently contains 13 fallback candidates while
its schema fixes the count at 12. The changed Kubernetes-adapter suite is green.
The changed scientific-artifact exact-host runs pass. A later pre-existing
same-bucket/Kueue fixture in that test file remains independently tracked and
does not invalidate the exact-host assertions.

## Safe rollout and rollback

Do not deploy this task commit directly to the retained shared service. First,
an integration branch must contain the currently deployed source, this change,
and the completed MindGuard/voice sibling changes. Wait for the in-progress Helm
rollback to settle before planning anything.

The safe order is:

1. Record the settled Helm revision and both control-plane image digests. Build,
   scan, and sign the integrated controller image.
2. Upgrade the controller while leaving `fs2-models` without the default deny.
   Wait until every managed runtime Deployment carries a recognized finite
   network-profile label; reject any unknown profile before continuing.
3. Apply the Terraform profile policies; verify their selectors and ports.
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

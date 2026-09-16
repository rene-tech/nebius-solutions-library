# SAI-03 model-runtime network isolation

Status: additive corrective successor whose direct parent is rejected source
commit `093798f53cb4249887e59513a3b0114246f7e94c`; that exact commit remains
preserved as negative evidence. Rejected integration commits
`92f9394cb3eb76b9b02f7682c96c056ae600e9ed` and
`89b5cfe17cffd0a1924fcb4f1af52c8a449c4d1e` are evidence only and are not
ancestors of this successor. No commit in this lineage has been deployed;
production rollout remains gated on a future independently accepted SAI-07/KEDA
successor and a new integration review.

This change closes the source-side causes of SAI-03 without relying on runtime
pods to carry the historical `app.kubernetes.io/instance` label:

- `fs2-models` has Terraform-owned finite allow profiles, but its ingress-and-
  egress `default-deny` exists only in the explicit `enforce` phase. A permanent
  deny-mode ValidatingAdmissionPolicy fence first rejects any Deployment,
  StatefulSet, DaemonSet, ReplicaSet, Job, JobSet, or Pod that lacks one of the
  finite profiles. Enforcement then runs a fresh live verifier during apply,
  after Helm and every known workload producer, before Terraform may create the
  deny. The content-addressed census includes all of those workload kinds,
  every extant Pod and controller UID, old ReplicaSets, rollout convergence,
  admission-binding UIDs, and the actual Ready model-controller Pod image ID.
  Base profiles are bounded by egress
  mode and service port. ModelExpress profiles are bounded by the exact
  qualification digest, accelerator class/count, NIXL backend and service port.
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
  workloads and cache keepers retain true zero egress. Batch/evaluation Jobs
  select a finite internal profile with DNS, the exact control-plane endpoint,
  and configured exact-host object-store routes. Public Hugging Face cache
  acquisition is isolated in a separate finite profile that permits only TCP
  443 to public addresses and excludes private, loopback, link-local,
  documentation, multicast, and reserved ranges. The Kueue acceptance Job uses
  a zero-egress profile.
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

This common finite-profile/no-NetworkPolicy-authority design was relayed to the
active SAI-07 worker. SAI-03 does not modify the SAI-07 branch or task files;
independent integration review must verify that both exact successors remove
the rule before either can integrate.

## Enforced transition contract

`deployment.models.network_policy.phase` is a closed state machine:

1. `prepare` creates or updates finite profiles, the inert admission-policy
   definitions, and every label-producing controller/manifest while both the
   admission bindings and `fs2-models/default-deny` remain absent.
2. After those rollouts converge, apply `inventory`. Terraform installs four
   deny-mode workload-profile bindings only after Helm, static models, keepers,
   and acceptance producers, plus a fifth binding that forbids update or
   deletion of the exact boundary marker. It then creates that immutable,
   Terraform-owned marker. Every admission resource has `prevent_destroy`, the
   namespaced model controller has no admission-policy authority, and the live
   marker is protected even though the controller retains ordinary ConfigMap
   access. A return to `prepare` is structurally impossible after the fence is
   armed.
3. Export `model_runtime_network_policy_transition` from the applied inventory
   state and run the read-only receipt tool. It lists **all** Pod-producing
   workload kinds and Pods in `fs2-models`, rejects an empty workload inventory,
   naked/orphaned Pods, unknown profiles, and incomplete rollouts, and captures
   live controller and admission-binding identities rather than desired values.
4. Set phase `enforce` and supply that receipt. A `local-exec` apply fence
   re-runs the read-only census after Helm, static models, keepers, acceptance,
   finite policies, and admission bindings have converged. Only a byte-equivalent
   live census and exact running controller digest unlock `default-deny`.
   Concurrent arbitrary-App changes remain safe because admission permits them
   only with a finite immutable profile. The receipt can be refreshed while the
   phase remains `enforce`, so normal App additions and recreations do not force
   rollback.
5. To roll back, set `rollback-remove-deny` with the same inventory receipt and
   without changing the enforced image. The supported `inference-stack`
   workflow rejects the saved plan unless every managed change is either
   deletion of `default-deny` or the transition-state update. It accepts zero,
   either one, or both allowed changes so a crash between them is resumable,
   while still rejecting Helm or unrelated mutation.
6. After that exact plan is applied, generate a `deny-absent` receipt. The tool
   refuses it while the deny exists or any finite allow policy is missing. Set
   `rollback-helm` with both receipts. Terraform independently re-reads the live
   policies and enforcement marker; only then may the Helm release change.

Receipt generation is read-only and emits no credentials or workload payloads:

```bash
terraform -chdir=stages/workloads output -json \
  model_runtime_network_policy_transition > /secure/path/network-transition.json

python3 stages/workloads/scripts/model_network_policy_transition.py inventory \
  --contract /secure/path/network-transition.json \
  --kubeconfig /secure/path/kubeconfig \
  --context <exact-context> > /secure/path/network-inventory-receipt.json

# Terraform runs this same check during the enforce apply, immediately before
# default-deny. Operators may also reproduce it read-only:
FS2_NETWORK_TRANSITION_JSON="$(cat /secure/path/network-transition.json)" \
FS2_NETWORK_RECEIPT_JSON="$(cat /secure/path/network-inventory-receipt.json)" \
python3 stages/workloads/scripts/model_network_policy_transition.py verify-enforce \
  --contract-json-env FS2_NETWORK_TRANSITION_JSON \
  --receipt-json-env FS2_NETWORK_RECEIPT_JSON \
  --kubeconfig /secure/path/kubeconfig --context <exact-context>

# Run only after the isolated rollback-remove-deny saved plan was applied.
python3 stages/workloads/scripts/model_network_policy_transition.py deny-absent \
  --contract /secure/path/network-transition.json \
  --kubeconfig /secure/path/kubeconfig \
  --context <exact-context> > /secure/path/deny-absent-receipt.json
```

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
  -filter=tests/modelexpress.tftest.hcl -no-color

terraform -chdir=stages/workloads test \
  -filter=tests/academic_assets_render.tftest.hcl \
  -filter=tests/scientific_artifacts.tftest.hcl -no-color

python3 -m pytest -q \
  tests/test_deployment_contract.py \
  tests/test_inference_stack.py \
  tests/test_model_network_policy_transition.py \
  models/general-media/tests/test_shared_cache_localization.py

terraform -chdir=reference-data/terraform init -backend=false -input=false
terraform -chdir=reference-data/terraform validate -no-color
terraform -chdir=reference-data/terraform test \
  -filter=tests/bootstrap.tftest.hcl -no-color
```

Observed results for this corrective successor:

- complete control-plane suite: 1,969 passed, 98 skipped;
- root/wrapper/receipt/offline-preflight focus: 148 passed and 109 subtests
  passed;
- SAI-03 academic, ModelExpress, and scientific-artifact workload fixtures:
  30 passed (8 + 11 + 11), including prepare/inventory/enforce/rollback gates,
  exact-host route validation, and the previously masked scientific-artifact
  same-bucket rejection;
- complete workloads Terraform suite: 52 passed, 1 failed, 7 skipped. The sole
  failure is the pre-existing general-CPU fixture
  `an_exact_cpu_runtime_renders_one_static_service_without_a_gpu`, where
  `local.selected_queue_pools[pool_id]` addresses a CPU pool outside the
  selected GPU queue-pool map; no SAI-03 transition, academic, or scientific
  test failed;
- the full control-plane run includes finite-profile derivation, real HTTP
  arbitrary-App lifecycle, gateway/model/MCP behavior, and absence of
  NetworkPolicy requests from the App controller;
- ModelExpress Terraform contract: 11 passed, including exact cross-layer
  profile inputs, IPv4 `/1`-pair rejection, IPv6 `/32` and `/64` rejection, and
  IPv6 `/128` acceptance;
- academic-assets module: 18 passed, including separate IPv6 `/32` and `/64`
  rejection and `/128` acceptance;
- catalog Kubernetes adapters: 19 passed, including native selector matching
  and KServe/NIM fail-closed behavior;
- reference-data bootstrap: 6 passed;
- general-media offline-preflight/static-policy suite: 5 passed;
- Helm lint, Terraform formatting/validation, focused Ruff lint/format, focused
  mypy, and `git diff --check`: passed.

Trivy 0.70.0 reported zero High/Critical findings in each changed Terraform
file. The two legacy model manifests retain two pre-existing High findings each
for writable root filesystems; this NetworkPolicy change neither introduced nor
concealed them.

A server-side dry-run against the retained API had accepted both hardened
legacy policies and the namespace default deny at the rejected parent. It was
not repeated for this successor because shared-live reconciliation is still
required and no rollout or live mutation is authorized from this branch.

The complete catalog suite at the accepted `4ea4b126` base ran 163 tests and
retained one unrelated baseline error: `model-variants.json` currently
contains 13 fallback candidates while its schema fixes the count at 12. The
changed Kubernetes-adapter suite is green. The academic and scientific
integration fixtures changed by this successor are now green; no fixture
failure is being waived as SAI-03 evidence.

## Deferred rollout and rollback design

The 2026-09-16 user constraint forbids live mutation, deletion, replacement, or
creation of disposable external resources, so this successor is source-only:
no prepare/enforce/rollback apply or live negative probe is authorized. Do not
deploy it directly to the retained shared service. First,
an integration branch must contain the currently deployed source, this change,
and the completed MindGuard/voice sibling changes. Wait for the in-progress Helm
rollback to settle before planning anything.

The safe order is:

1. Record the settled Helm revision and both control-plane image digests. Build,
   scan, and sign the integrated controller image.
2. Apply `prepare`, which upgrades the integrated controller and finite policy
   profiles while structurally keeping admission bindings and
   `fs2-models/default-deny` absent.
3. Apply `inventory` to arm the admission fence after label convergence, then
   generate the inventory receipt and plan `enforce`. Review that the live
   workload and Pod census covers the full retained fleet and apply its exact
   saved plan. Never author or copy a receipt by hand.
4. Prove positive gateway PAT/model-grant sync and streaming inference plus MCP
   model calls. Prove from a scratch pod that direct model `:8000` access fails,
   and from an isolated model pod that public HTTPS egress fails while DNS and
   gateway inference continue to work. Recheck operations, observability, and
   current sibling models.

The designed failure recovery enters `rollback-remove-deny` first; the wrapper
proves that every remaining change is one of the two allowed crash-resumable
changes and contains no Helm or unrelated mutation. After a deny-absent receipt,
`rollback-helm` may return to the recorded pre-rollout revision/image digest.
Never remove allow policies before the deny and never combine deny removal with
Helm rollback. This design is documented and tested only; executing its delete
step is blocked by the current no-delete constraint.

No GPU/model behavior changed, so a new GPU inference campaign is not meaningful
before the integrated live rollout. No temporary cloud, Kubernetes, registry, or
GPU resources were created, and there is nothing for this task to clean up.

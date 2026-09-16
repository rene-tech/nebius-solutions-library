# Model-runtime network isolation

Model runtime isolation is owned by Terraform rather than by the dynamic model
controller. This keeps policy names and contents finite even when customer Apps
use arbitrary UUIDs. Deployment remains gated on independent review and a
serialized integration with the Pod Security rollout.

## Policy profiles

The `fs2-models` namespace has Terraform-owned finite allow profiles. Its
default deny exists only in the explicit enforcement phase, after a permanent
admission fence and a content-addressed live census prove every Pod-producing
workload carries a known profile. The census binds workload and Pod UIDs,
resource versions, rendered content, rollout convergence, admission bindings,
and the running controller image identity.

Runtime Pods select one immutable `fs2-serve.nebius.ai/network-profile`:

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
   marker is protected while the controller has zero ConfigMap authority. A
   return to `prepare` is structurally impossible after the fence is armed.
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

Preserved historical test results for the exact SAI-03 `6dc670386` corrective
source lineage. That lineage remains SOURCE NO-GO/correcting and is not an
accepted integration or deployment baseline:

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

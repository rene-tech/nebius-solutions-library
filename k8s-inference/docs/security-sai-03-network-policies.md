# SAI-03 model-runtime network isolation

Status: unreviewed additive corrective successor whose direct parent is rejected
source commit `518c60c34439e4a2f7dafc6c218de58af6a0c2a9` / tree
`946ff08ee1cb753892b30a39c379dfb6bac02c2e`; that exact commit remains
preserved as negative evidence. Rejected commits
`6dc67038698ed4d0412873e02baa1d50b179ff3c` and
`093798f53cb4249887e59513a3b0114246f7e94c`, plus final-NO-GO
`9b71b8a58b1e23a1d5f9d9ac11243dbad9a4652f`, are ancestors of this successor and
remain negative evidence; `92f9394cb3eb76b9b02f7682c96c056ae600e9ed` and
`89b5cfe17cffd0a1924fcb4f1af52c8a449c4d1e` remain separate rejected evidence.
No commit in this lineage has been deployed. Production rollout remains gated
on independent exact-commit source review, the future accepted SAI-07/KEDA
successor, a clean integration review, and execution of the real saved rollback
plan gate when deletion-capable testing is authorized.

This change closes the source-side causes of SAI-03 without relying on runtime
pods to carry the historical `app.kubernetes.io/instance` label:

- `fs2-models` has Terraform-owned finite allow profiles, but its ingress-and-
  egress `default-deny` exists only in the explicit `enforce` phase. A permanent
  deny-mode ValidatingAdmissionPolicy fence first rejects any Deployment,
  StatefulSet, DaemonSet, ReplicaSet, ReplicationController, Job, CronJob,
  JobSet, or Pod that lacks one of the finite profiles. Enforcement then runs a
  fresh live verifier during apply,
  after Helm and every known workload producer, before Terraform may create the
  deny. The content-addressed census includes all of those workload kinds,
  every extant Pod and controller UID, old ReplicaSets, rollout convergence,
  exact admission-policy and binding UIDs/specifications, the retained
  transition Lease UID, and the actual Ready model-controller Pod image ID.
  Base profiles are bounded by egress
  mode and service port. ModelExpress profiles are bounded by the exact
  qualification digest, accelerator class/count, NIXL backend and service port.
- Runtime Pods select those profiles with the immutable
  `fs2-serve.nebius.ai/network-profile` label. Arbitrarily named customer Apps,
  including `app-<uuid>` single- and multi-pool clones, reuse a canonical finite
  profile; no App name or UUID becomes policy authority.
- Every finite policy also selects an immutable workload class. Serving
  Deployments/Pods can select only canonical serving profiles; cache,
  acceptance, internal-job, and public-acquisition profiles are disjoint.
  Public acquisition additionally requires catalog ownership labels, an exact
  acquisition-plan annotation on Job and Pod template, and the dedicated cache
  service account. A runtime Deployment cannot regain public TCP/443 by copying
  the support profile label.
- Network-profile admission also binds **who may create** each parent workload.
  Arbitrary namespace Job writers cannot self-select public acquisition or
  support egress: scientific Job/JobSet creation is limited to the exact
  control-plane runtime service account; App/Deployment creation is limited to
  the exact model-controller service account; JobSet and Kubernetes child
  objects are limited to their exact controller identities; cache, acceptance,
  CronJob, ReplicationController, and other transition-owned parents require
  the exact authenticated writer while it holds the retained transition Lease.
- CEL performs the finite object-shape check, while an independent fail-closed
  TLS admission service dereferences each ReplicaSet, Job, and Pod's single
  controller owner. It requires the live parent's exact apiVersion, kind, name,
  UID, non-deleting state, workload class, profile, and child-template profile;
  a spoofed owner reference or copied public-acquisition label is rejected. The
  service runs under a projected, read-only ServiceAccount and cannot create or
  change workloads, policies, RBAC, or the transition Lease.
- Direct public-acquisition Jobs use the normal catalog control-plane runtime
  writer and remain available after the transition Lease is released. They do
  not grant the transition authorizer a second acquisition path.
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

1. Platform Security supplies two mode-0600 kubeconfigs with different
   server-authenticated usernames: `fs2-model-network-authorizer` for prepare
   and `fs2-model-network-transition` for the retained Lease and later phases.
   The wrapper rejects the shared deployment kubeconfig, symlinks,
   group/world-readable files, a mismatched server-side username, and all
   impersonation-based substitutes. Both credentials are external prerequisites;
   this task does not mint or commit them.
2. Before acquiring the Lease, the wrapper reads the latest
   `fs2-serve-control-plane` Helm release record and rejects every `pending-*`
   state. Once the random `run:pid:128-bit-token` holder is active, an admission
   binding installed during prepare freezes that Helm release's Secret/ConfigMap
   writes. A second preflight immediately after lock acquisition catches an
   operation that became pending before the API fence armed; another check runs
   immediately before and after the saved apply.
3. `prepare` creates or updates finite profiles, the inert admission-policy
   definitions, and every label-producing controller/manifest while both the
   admission bindings and `fs2-models/default-deny` remain absent.
4. After those rollouts converge, apply `inventory`. The supported wrapper
   resolves the separate credential's exact Kubernetes username with
   `kubectl auth whoami`, then
   acquires the retained `fs2-system/fs2-model-network-transition` Lease. A
   deny-mode Lease policy permits only that recorded identity to acquire, renew,
   or release the Lease; it rejects holder theft and deletion.
   Terraform installs six deny-mode workload-profile bindings only after Helm,
   static models, keepers, and acceptance producers, plus a binding that
   forbids update or deletion of the exact boundary marker and a separate
   `fs2-system` binding that freezes updates/deletion of the exact live
   model-controller. A second `fs2-system` binding rejects creation, update,
   or deletion of the Helm release-storage Secret/ConfigMap for
   `fs2-serve-control-plane`, so an out-of-band Helm operation is rejected
   before it can partially mutate chart resources. It then creates that immutable,
   Terraform-owned marker. Policy definitions and the profile/marker bindings
   have `prevent_destroy`; both freeze bindings are intentionally removable
   only in the deny-absent `rollback-helm` phase. The namespaced
   model controller has no admission-policy authority, and the live marker is
   protected even though the controller retains ordinary ConfigMap access. A
   separate parameterized admission guard covers the exact finite
   NetworkPolicies (including any newly named policy), the marker,
   ValidatingAdmissionPolicies, and
   ValidatingAdmissionPolicyBindings. The API server accepts their mutation
   only from the recorded writer while the Lease has a non-empty holder; the
   Terraform-owned objects also carry that writer as non-authoritative
   provenance. Thus Helm or
   another Terraform client with unrelated credentials cannot race or weaken
   the verified boundary merely by ignoring the wrapper. The independently
   served webhook additionally binds each protected create/update to the random
   live Lease-holder annotation; delete is accepted only from the separate
   transition identity while that random live holder is active, because a
   deleted object cannot first persist a new holder annotation. A
   return to `prepare` is structurally impossible after the fence is armed.
5. Export `model_runtime_network_policy_transition` from the applied inventory
   state and run the read-only receipt tool. It lists **all** Pod-producing
   workload kinds and Pods in `fs2-models`, rejects an empty workload inventory,
   naked/orphaned Pods, unknown profiles, and incomplete rollouts. It includes
   old ReplicaSets, core ReplicationControllers, CronJobs, workload class,
   exact admission-policy/binding UIDs, resourceVersions, and complete stored
   specs, plus the webhook configuration UID, resourceVersion, exact two-hook
   fail-closed semantics and complete live spec hash, the live controller, and the
   retained transition-Lease UID rather than desired values. Receipt capture is
   refused while another transition owns the Lease.
6. Set phase `enforce` and supply that receipt. A `local-exec` apply fence
   re-runs the read-only census after Helm, static models, keepers, acceptance,
   finite policies, and admission bindings have converged. Only a byte-equivalent
   live census, exact full admission resources, an unexpired matching Lease holder,
   and the exact running controller digest unlock `default-deny`. The admission
   freezes reject an external Helm release write or direct model-controller
   mutation after verification; the Lease serializes every supported
   post-prepare transition.
   Concurrent arbitrary-App changes remain safe because admission permits them
   only with a finite immutable profile. The receipt can be refreshed while the
   phase remains `enforce`, so normal App additions and recreations do not force
   rollback.
7. To roll back, set `rollback-remove-deny` with the same inventory receipt and
   without changing the enforced image. The supported `inference-stack`
   workflow rejects the saved plan unless every managed change is deletion of
   `default-deny`, deletion of the one-shot apply fence, the transition-state
   update, or a bounded update whose only semantic difference is the new random
   holder annotation on an exact Terraform-owned boundary object. It accepts
   any subset of the deletion/state changes so a crash between them
   is resumable, while still rejecting Helm or unrelated mutation. The
   controller and Helm release-storage freezes remain active through this phase.
   The source allowlist now accepts crash-resume subsets, but the required real
   saved-plan proof has not been run under the current no-test/no-delete
   constraint; rollback remains an integration gate rather than accepted
   evidence.
8. After that exact plan is applied, generate a `deny-absent` receipt. The tool
   refuses it while the deny exists or any finite allow policy is missing. Set
   `rollback-helm` with both receipts. Terraform independently re-reads the live
   policies and enforcement marker; only then are both freeze bindings removed
   and the Helm release allowed to change.

Receipt generation is read-only and emits no credentials or workload payloads:

```bash
terraform -chdir=stages/workloads output -json \
  model_runtime_network_policy_transition > /secure/path/network-transition.json

python3 stages/workloads/scripts/model_network_policy_transition.py inventory \
  --contract /secure/path/network-transition.json \
  --kubeconfig /secure/path/kubeconfig \
  --context <exact-context> > /secure/path/network-inventory-receipt.json

# During enforce, inference-stack supplies the same contract and receipt to the
# read-only verifier while holding the transition Lease. A standalone verifier
# without that exact live holder identity fails closed.

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

Independent review of exact rejected parent
`518c60c34439e4a2f7dafc6c218de58af6a0c2a9` / tree
`946ff08ee1cb753892b30a39c379dfb6bac02c2e` was final
**SOURCE/INTEGRATION/LIVE NO-GO**. It confirmed the exact UID/resourceVersion
and full-spec receipt, complete census, refresh/imageID checks, rollback graph,
and clean `4ea4b126` controls. It rejected spoofable child owner/profile
authorization, transition-only public acquisition, a shared/cooperative
transition identity without a random holder boundary, and the lack of a
pending-Helm preflight. This unreviewed successor adds source controls for all
four findings. No claim is made that independent review has accepted them, and
the real saved rollback plan remains deliberately unexecuted under the
no-test/no-delete constraint.

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

Historical results for rejected parent `6dc67038698ed4d0412873e02baa1d50b179ff3c`
are preserved below. They do not qualify this additive successor:

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

No test, build, mutating formatter, Terraform plan/test/init, Helm, pytest, or
cleanup-capable command was run for this additive successor. The deletion-free
static checks were `git diff --check`, `terraform fmt -check -diff` on every
changed HCL file, `jq empty` on the chart schema, and focused Ruff lint plus
format-check with `--no-cache`; all passed. The user's hard no-delete constraint
permits only read-only inspection, additive source edits, and a normal versioned
commit. Independent exact-commit review must run the executable suites in an
environment where their temp/cache cleanup behavior is explicitly authorized.

For rejected parent `6dc67038698ed4d0412873e02baa1d50b179ff3c`, Trivy
0.70.0 reported zero High/Critical findings in each then-changed Terraform
file. The two legacy model manifests retained two pre-existing High findings
each for writable root filesystems; this successor neither resolves nor
conceals them. No Trivy scan was run on this successor.

A server-side dry-run against the retained API had accepted both hardened
legacy policies and the namespace default deny at the rejected parent. It was
not repeated for this successor because shared-live reconciliation is still
required and no rollout or live mutation is authorized from this branch.

The complete catalog suite at the clean `4ea4b126` base ran 163 tests and
retained one unrelated baseline error: `model-variants.json` currently
contains 13 fallback candidates while its schema fixes the count at 12.
Historical focused adapter and Terraform fixture results belong to the rejected
parent only; the files changed by this successor have not been executed under
the no-delete constraint, and no historical result is promoted as evidence for
them.

## Deferred rollout and rollback design

The 2026-09-16 user constraint forbids live mutation, deletion, replacement, or
creation of disposable external resources, so this successor is source-only:
no prepare/enforce/rollback apply or live negative probe is authorized. Do not
deploy it directly to the retained shared service. First,
an integration branch must contain the currently deployed source, this change,
and the completed MindGuard/voice sibling changes. Wait for the in-progress Helm
rollback to settle before planning anything.

No pre-existing user, product, or live file/resource was successfully deleted
after that constraint. The only observed cleanup activity before the stricter
reminder was pytest attempting and failing to remove its own scratch paths;
there was no live/cloud/cluster/database/registry mutation. All remaining
ignored and untracked artifacts are preserved.

The safe order is:

1. Record the settled Helm revision and both control-plane image digests. Build,
   scan, and sign the integrated controller image.
2. Supply the two independently issued boundary kubeconfigs, verify both exact
   usernames, and apply `prepare` while the random transition Lease is held.
   This upgrades the integrated controller and finite profiles while keeping
   workload-profile/deny bindings and `fs2-models/default-deny` absent; only the
   Lease-conditional Helm freeze is armed.
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
proves that every remaining change is one of the three allowed crash-resumable
changes and contains no Helm or unrelated mutation. After a deny-absent receipt,
`rollback-helm` may remove the controller and Helm release-storage freezes and
return to the recorded pre-rollout revision/image digest.
Never remove allow policies before the deny and never combine deny removal with
Helm rollback. This design is documented and tested only; executing its delete
step is blocked by the current no-delete constraint.

No GPU/model behavior changed, so a new GPU inference campaign is not meaningful
before the integrated live rollout. No temporary cloud, Kubernetes, registry, or
GPU resources were created, and there is nothing for this task to clean up.

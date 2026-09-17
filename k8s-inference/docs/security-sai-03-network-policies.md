# Model-runtime network isolation

Status: unreviewed additive corrective successor whose direct parent is rejected
source commit `6eb6d7afb3365658da5d7ab51b7b152e66b8237a` / tree
`1ead86c4b43b684bf3c4fc2574316517eb8df893`; that exact commit remains
preserved as negative evidence. Rejected commits
`6dc67038698ed4d0412873e02baa1d50b179ff3c` and
`093798f53cb4249887e59513a3b0114246f7e94c`, plus final-NO-GO
`9b71b8a58b1e23a1d5f9d9ac11243dbad9a4652f` and
`518c60c34439e4a2f7dafc6c218de58af6a0c2a9`, are ancestors of this successor
and remain negative evidence; `92f9394cb3eb76b9b02f7682c96c056ae600e9ed` and
`89b5cfe17cffd0a1924fcb4f1af52c8a449c4d1e` remain separate rejected evidence.
No commit in this lineage has been deployed. Production rollout remains gated
on independent exact-commit source review, the future accepted SAI-07/KEDA
successor, a clean integration review, and execution of the real saved rollback
plan gate when deletion-capable testing is authorized.

## Policy profiles

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
- Direct public-acquisition Jobs use the dedicated
  `fs2-system/fs2-catalog-acquisition` ServiceAccount. The separately released
  authority owns its narrow Job writer RoleBinding; neither the general
  control-plane runtime nor either transition identity is a subject. Its UID,
  resourceVersion, exact authenticated username, and binding semantics are
  included in the signed custody receipt. Acquisition remains available after
  the transition Lease is released without granting the transition authorizer
  a second acquisition path.
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

1. Platform Security first installs the independent
   `fs2-model-network-boundary` chart in `fs2-network-security`. The active
   control-plane chart cannot render an embedded copy. The authority uses a
   dedicated digest-pinned `*/network-boundary-authority` image whose repository
   and digest differ from the control-plane image. That artifact has its own
   `Dockerfile.network-boundary` and `fs2-network-boundary` entrypoint and never
   starts the general control-plane CLI. It also has a separate ServiceAccount,
   certificate, RBAC, Service, and two-replica Deployment. Every authority
   object is selected by the custody hook; the exact cert-manager identity may
   rotate only the complete TLS Secret. Platform Security signs a short-lived
   receipt containing every live UID, resourceVersion and full semantic hash.
   The root deployment contract pins the offline signing public-key digest.
2. Platform Security supplies two mode-0600 kubeconfigs with different
   server-authenticated usernames: `fs2-model-network-authorizer` for prepare
   and `fs2-model-network-transition` for the retained Lease and later phases.
   The wrapper rejects the shared deployment kubeconfig, symlinks,
   group/world-readable files, a mismatched server-side username, and any
   credential not bound to the signed X.509 certificate and kubeconfig digests.
   The receipt declares impersonation forbidden. Both credentials are external
   prerequisites; this task does not mint or commit them.
3. The external webhook permanently freezes the exact Helm release-storage
   records and every labeled `fs2-serve-control-plane` object, including its
   image, RBAC and controller Deployments, even while the transition Lease is
   idle. It is installed before any SAI-03 phase, not by `helm_release.control_plane`.
   A normal shared kubeconfig therefore cannot start a release in the old
   precheck-to-fence window. Release mutation is admitted only for the dedicated
   transition identity while its unexpired random holder is active, the immutable
   enforcement marker exists, and `fs2-models/default-deny` is live-confirmed
   absent. The wrapper also rejects `pending-*` Helm status and any non-no-op
   control-plane Helm plan during prepare, inventory, enforce, or deny removal.
4. `prepare` creates or updates finite profiles, the inert admission-policy
   definitions, and every label-producing controller/manifest while both the
   admission bindings and `fs2-models/default-deny` remain absent.
5. After those rollouts converge, apply `inventory`. The supported wrapper
   resolves the separate credential's exact Kubernetes username with
   `kubectl auth whoami`, then
   acquires the retained `fs2-system/fs2-model-network-transition` Lease. A
   deny-mode Lease policy permits only that recorded identity to acquire, renew,
   or release the Lease; it rejects holder theft and deletion. If a process
   crashes with a non-empty holder, both the wrapper and external webhook use
   the same bounded `renewTime + leaseDurationSeconds` rule: a resourceVersion
   CAS may replace only an expired holder, so recovery never requires a manual
   clear and an active holder can never be stolen.
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
6. Export `model_runtime_network_policy_transition` from the applied inventory
   state and run the read-only receipt tool. It lists **all** Pod-producing
   workload kinds and Pods in `fs2-models`, rejects an empty workload inventory,
   naked/orphaned Pods, unknown profiles, and incomplete rollouts. It includes
   old ReplicaSets, core ReplicationControllers, CronJobs, workload class,
   exact admission-policy/binding UIDs, resourceVersions, and complete stored
   specs, plus the webhook configuration UID, resourceVersion, exact nine-hook
   bounded fail-closed semantics and complete live spec hash, the live controller, and the
   retained transition-Lease UID rather than desired values. Receipt capture is
   refused while another transition owns the Lease.
7. Set phase `enforce` and supply that receipt. A `local-exec` apply fence
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
8. To roll back, set `rollback-remove-deny` with the same inventory receipt and
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
9. After that exact plan is applied, generate a `deny-absent` receipt. The tool
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

Independent review of exact rejected parent
`6eb6d7afb3365658da5d7ab51b7b152e66b8237a` / tree
`1ead86c4b43b684bf3c4fc2574316517eb8df893` was final
**SOURCE/INTEGRATION/LIVE NO-GO**. It confirmed exact live parent/UID/profile
and child-writer checks, a 128-bit Lease holder with resourceVersion CAS,
comprehensive v5 receipt semantics, bounded rollback and clean SAI-03-only
ancestry. It rejected the stale-holder deadlock; the embedded/same-image
authority and impersonation-capable custody story; lack of a distinct catalog
acquisition identity; the Helm precheck-to-fence race; and a cluster-wide
failure domain for the webhook. This unreviewed additive successor addresses
those source findings with expiry-consistent takeover, an externally released
and signed authority, a dedicated acquisition ServiceAccount, a permanent
full-release freeze, and nine exact namespace/object-scoped hooks. No claim is
made that independent review has accepted this successor, and the real saved
rollback plan remains deliberately unexecuted under the no-test/no-delete
constraint.

Run from `k8s-inference` unless a command changes directory:

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

Historical results for rejected parent `6dc67038698ed4d0412873e02baa1d50b179ff3c`
are preserved below. They do not qualify this additive successor. The current
SAI-03 lineage remains SOURCE NO-GO/correcting and is not an accepted integration
or deployment baseline:

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
cleanup-capable command was run for this additive successor. Deletion-free
static checks passed: Python AST parsing for nine changed executable/test files;
focused Ruff lint and format-check with `--no-cache`;
`terraform fmt -check -diff` for all six changed HCL files; `jq empty` for all three changed JSON
schemas; and `git diff --check`. Historical executable results are not promoted.
The user's hard no-delete constraint permits only read-only inspection,
additive source edits, and a normal versioned commit. Independent exact-commit
review must run the executable suites in an environment where their temp/cache
cleanup behavior is explicitly authorized.

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
   scan, sign and roll out the integrated controller image before the boundary
   authority is installed. Confirm the release is idle and converged.
2. Platform Security installs the independent authority chart with its distinct
   digest-pinned image and credentials, waits for both replicas/certificate/all
   hooks, captures the exact protected-object inventory, and signs the bounded
   receipt. From that point the control-plane release is permanently frozen.
3. Supply the signed receipt and two independently issued boundary kubeconfigs,
   verify both exact usernames and receipt-bound certificate hashes, and apply
   `prepare` while the random transition Lease is held. The Helm release must be
   a no-op; this phase changes only finite profiles and label-producing objects
   while workload-profile/deny bindings and `fs2-models/default-deny` remain
   absent.
4. Apply `inventory` to arm the admission fence after label convergence, then
   generate the inventory receipt and plan `enforce`. Review that the live
   workload and Pod census covers the full retained fleet and apply its exact
   saved plan. Never author or copy a receipt by hand.
5. Prove positive gateway PAT/model-grant sync and streaming inference plus MCP
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

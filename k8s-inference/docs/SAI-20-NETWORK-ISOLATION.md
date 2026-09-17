# SAI-20 PostgreSQL ingress and control-plane API egress

## Candidate boundary

This source-only candidate adds `fs2-control-db-ingress` in `fs2-data`. The
policy selects only CloudNativePG Pods labelled `cnpg.io/cluster=fs2-control-db`
and admits PostgreSQL clients from exact namespace-and-Pod label pairs:

- the control-plane runtime, model controller, maintenance, migration and
  bootstrap components in `fs2-system`;
- the separately reviewed website and customer-storage components, when those
  additive sources are integrated;
- Grafana and run-bound Terraform acceptance Jobs in `fs2-observability`, plus
  run-bound acceptance Jobs in `fs2-system`;
- CloudNativePG peers and the CloudNativePG operator for HA/lifecycle traffic;
- Prometheus on the metrics port only.

The control-plane chart receives a final values overlay whose
`networkPolicy.kubernetesApiCidrs` contains only the `default/kubernetes`
Service host route and ready API endpoint host routes. The target private
subnet CIDR is deliberately excluded from this effective runtime allowlist.
Scientific artifact destinations remain governed by their existing exact
`/32` or `/128` contract and are not broadened here.

CloudNativePG documents that the operator requires TCP 8000 and 5432, that
instances must communicate with each other, and that instance metrics use TCP
9187. Those preservation paths are explicit and do not grant application
access to a namespace as a whole:
<https://cloudnative-pg.io/docs/1.26/networking/> and
<https://cloudnative-pg.io/docs/1.26/security/>.

## Source provenance and integration

- Original task parent: `83bcb2d6c7f4dc112e414e00596e0d6b03e22712`.
- Reconciled tracked `origin/main` parent:
  `0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697`.
- SAI-03 clean successor inspected for overlapping model-network ownership:
  `cea63190aca6548d8be961a9432cc7cc1277721e`.
- SAI-08 candidate inspected for additive website/customer-storage database
  clients: `6eb13e345c8b17420d1217a70d83e4974497b2b0`.

Neither sibling candidate is merged here. An integration owner must reconcile
their accepted descendants and rerun the complete policy contract before any
shared rollout.

The application-peer rules trust protected workload labels. They must not be
rolled out without the accepted SAI-03 admission/ownership boundary that
prevents an arbitrary workload creator from self-assigning those labels.

## Deferred verification and rollback

The coordinator boundary for this authoring pass forbids tests, formatters,
Terraform/Helm commands, cluster inspection and deployment. The regression
suite in `tests/test_sai20_network_isolation.py` is authored but intentionally
not executed. No resource, image, Helm revision, database, provider, registry,
credential or customer workload was inspected or changed.

After independent source acceptance and integration with the then-current
deployed source, the rollout owner must record the previous Helm revision and
run at least:

1. render/validate the workloads Terraform and control-plane chart;
2. confirm `kubectl -n fs2-data get networkpolicy fs2-control-db-ingress`;
3. prove an unlabelled scratch Pod cannot connect to
   `fs2-control-db-rw.fs2-data.svc:5432` within a bounded timeout;
4. prove runtime, migration, maintenance, bootstrap, Grafana, acceptance,
   CloudNativePG HA/status and Prometheus metrics paths remain healthy;
5. rerun customer inference, MCP, admin, operations/results/artifacts/uploads,
   storage, queue/model admission, observability and request-debug smoke tests.

Rollback is the previous reviewed workloads Terraform/Helm revision, or a
normal revert of the eventual integration commit. This candidate does not
authorize a rollout and is not a GO decision.

## Independent-review correction after `07faac62`

Independent review rejected commit
`07faac62c6854a7b7947f97f59b5b7b1030813fd` / tree
`6d56efe1edf36bb60cd272f86e585433673d0229`. Preserve that candidate as
negative evidence; its SAI-03 custody claim and SAI-08 compatibility statement
are not promotion evidence.

The exact SAI-08 source at
`6eb13e345c8b17420d1217a70d83e4974497b2b0` / tree
`bd55519891c3f653f11465bf59e997170ff8bf4a` labels the database-backed
customer-storage Pod `storage-reconciler-v3`, injects `FS2_DATABASE_URL`, and
allows egress to `fs2-data` on TCP 5432. The successor ingress rule now matches
that component only when both its storage egress and rollout generation labels
exist. The retained legacy `storage-reconciler` path is unchanged. The
cross-source fixture records the exact source paths and Git blobs so a review
cannot silently substitute another SAI-08 generation.

The cited SAI-03 source at
`cea63190aca6548d8be961a9432cc7cc1277721e` / tree
`3b16346454d84fe2cd3dad7dc468b850615371a5` is explicitly rejected as a custody
dependency: an object selector on top-level labels does not protect direct
Pods or labels nested in controller templates. The successor therefore adds an
API-server-native fail-closed policy with no object selector. It inspects the
effective Pod labels on Pods, ReplicationControllers, Deployments, StatefulSets,
DaemonSets, ReplicaSets, Jobs and CronJobs in both `fs2-system` and
`fs2-observability`. A controller identity is accepted only for a Pod carrying
a controller owner reference; controller templates require an exact release
writer identity.

The database NetworkPolicy now carries an explicit custody annotation. A
separate exact-name policy denies deletion or unauthorized mutation of that
NetworkPolicy, the two admission policies and bindings, and the namespace-local
writer Roles and RoleBindings. Existing broad RBAC cannot bypass this admission
deny. The workloads stage also requires a non-secret v2 handoff containing an
independently accepted source commit/tree, review receipt, RBAC census receipt,
impersonation-guard receipt, and exact writer/controller/custodian identities.
The rejected SAI-03 commit cannot satisfy that input.

This correction is still a source candidate, not proof that such a handoff has
been accepted. Under the coordinator boundary no test, render, validation,
plan, deployment, live inspection or negative connection probe was executed.
Integration and live status remain **NO-GO** until a distinct reviewer accepts
the exact successor and supplies the custody handoff.

## Independent-review correction after `850c1aeb`

Independent review also rejected commit
`850c1aeb134196b36250b5e8bd20cf7c1aa1c0aa` / tree
`3dc9f5ad9644540081a6c026328d84b1344240b0`. It remains negative evidence:
the v2 handoff was format-only, the admission policy protected future requests
without proving pre-existing objects, its object custody covered only the
canonical NetworkPolicy name, the writer RoleBinding granted an entire group
broad workload mutation, and the retained SAI-08 v2 database client was not an
ingress peer. Source, integration and live status for that commit are NO-GO.

The additive v3 successor makes activation depend on a detached Ed25519-signed
authority packet. `sai20_database_authority.py` validates the security-owner
public-key fingerprint, signature, a maximum one-hour validity window and a
maximum thirty-minute inventory age. The signed source must equal the clean
checkout HEAD and tree, bind every SAI-20 source blob, and bind the exact
SAI-08 `6eb13e34` tree and its protected-lane Terraform and reconciler blobs.
The rejected SAI-03, first SAI-20 and v2 SAI-20 commits are explicitly
inadmissible.

The packet is authoritative only when all of these closures are present:

- a complete, non-paginated `fs2-data` NetworkPolicy list with UID,
  resourceVersion, spec/selector digests and an explicit database-selector
  disposition for every item;
- complete, non-paginated Pod, ReplicationController, Deployment, StatefulSet,
  DaemonSet, ReplicaSet, Job and CronJob lists in both `fs2-system` and
  `fs2-observability`, with every pre-existing database peer approved;
- an explicit present-or-retired disposition for `storage-reconciler`,
  `storage-reconciler-v2` and `storage-reconciler-v3`;
- complete namespace Role/RoleBinding and cluster Role/RoleBinding lists,
  exact User or ServiceAccount principals, an empty membership result for the
  legacy broad group, a digest of effective permissions, and zero unaccounted
  impersonation-capable principals.

The verifier fails if a pre-activation inventory contains any policy selecting
the control database. In steady state it permits exactly one such policy: the
canonical name with the exact signed spec digest. A fail-closed admission
policy then owns the complete `fs2-data` NetworkPolicy set, denies deletes,
requires the exact signed custodian and rejects unlisted policy names. This is
necessary because Kubernetes combines ingress allows from all selecting
NetworkPolicies.

The verifier recomputes each policy selector against every inventoried
`fs2-control-db` Pod rather than trusting a claimed overlap boolean. Every
noncanonical policy must also carry an explicit selector contradiction for
`cnpg.io/cluster=fs2-control-db`; admission enforces that invariant on future
creates and updates, so adding unrelated labels to a database Pod cannot make
a stale policy overlap later.

The v3 workload policy has no object selector. It applies to every direct Pod
and supported controller in both namespaces, including top-level controllers
whose own labels appear innocuous. A mutation must match an exact signed
principal, resource, operation and object name, or be an owned child created
by an exact controller identity. The old group grant is additionally gated on
the signed proof that the group has no members. New task-owned Roles bind only
exact Users or ServiceAccounts, exact existing object names and
`get`/`update`/`patch`; they grant no Pod, create, delete, bind, escalate or
impersonate permission.

The ingress policy now has distinct generation-bound peers for the retained
SAI-08 v2 and v3 reconcilers. Legacy, v2 and v3 remain subject to the signed
inventory, so a generation can disappear only through a later conclusive
retirement receipt; this source change does not delete or strand a database
client.

This is still a source-only candidate. The authority packet, public key and
inventory are deliberately not fabricated or committed here. The coordinator
boundary prohibited tests, parsing/formatting tools, plans, builds, cluster or
credential inspection, deployment and live connection probes. The newly
authored regression module was not executed. A separate security owner and
integration worker must collect and sign the authoritative inventories, and a
distinct reviewer must accept the exact successor before any plan or rollout.

## Independent-review correction after `ffd86740`

Independent review rejected commit
`ffd8674063314f876b1e0b00b73a76fdb4ea27af` / tree
`bdd56f295a629cb79d6f34113c2945fda3a04953`. Preserve it as negative
evidence. Its authority key remained caller-selected; list receipts and review
digests were signed assertions rather than reconstructions from authenticated
raw responses; the planned ingress digest was not enforced against the
Terraform object; verification happened only while planning; the release
executor was not proven to be the custodian; and a controller could spoof an
owner kind without matching a live parent name and UID.

The additive v4 successor changes that trust boundary:

- `security/sai20/authority-roots-v1.json` is the only accepted root registry.
  It is committed in source and initially has status `ENROLLMENT_REQUIRED`
  with no keys, so this candidate cannot activate. A later additive,
  independently reviewed commit must enroll distinct collector and reviewer
  roots with immutable Git-object provenance and a content-derived acceptance
  record. Terraform has no public-key, fingerprint or key-ID input for v4.
- The dual-signed v4 bundle carries raw request and response bytes and hashes,
  endpoint/CA/TLS identities, API audit/request IDs, observation times and the
  collector credential identity. It has an exact request-name closure for all
  workload, NetworkPolicy and RBAC lists; collector and admitted-principal
  SelfSubjectReview/RulesReview/AccessReview requests; and the authoritative
  provider group-membership request. The verifier rebuilds list receipts,
  workload labels, storage generations, NetworkPolicy overlap, RBAC content,
  effective permissions and impersonation decisions from those bytes. The
  RBAC closure includes namespace Roles/RoleBindings in `fs2-system`,
  `fs2-observability` and `fs2-data` plus cluster Roles/RoleBindings; any
  subject granted sensitive workload, NetworkPolicy, admission or RBAC
  mutation through a RoleBinding in those three namespaces is either an exact
  admitted User/ServiceAccount or the provider-proven empty legacy group.
  Cluster RBAC remains fully content-digested and independently reviewed;
  exact admitted principals additionally carry raw SSRR and impersonation
  SSAR evidence.
- `contracts/sai20-control-db-ingress-v4.json` is the normalized ingress spec.
  The verifier recomputes its digest, an immutable ConfigMap publishes it, and
  a fail-closed admission policy requires the canonical NetworkPolicy's entire
  `spec` plus digest annotation to match. This preserves the legacy, v2 and v3
  storage peers, CNPG/operator/status paths, Grafana, Prometheus and run-bound
  acceptance while preventing an admitted custodian from widening ingress.
- Two `timestamp()`-backed unknown nonces defer identity and inventory checks
  to apply. The identity call first rechecks bundle expiry, exact Git source,
  kubectl digest, kubeconfig context/API server/CA and the actual executor
  SelfSubjectReview. That proven executor installs the self-protecting custody
  policy and a transition policy that freezes the complete `fs2-data`
  NetworkPolicy set to the raw, signed specs. The exact-parent workload policy
  is also bound before the second read: non-database controller traffic keeps
  the existing kind checks, while every database-labelled child requires its
  signed live parent and exact controller identity. Only then does the second
  call re-read every safe GET inventory and the executor's
  permission/impersonation decisions. This closes the gap in which an
  alternate additive NetworkPolicy could otherwise appear after planning.
  The signed source closure includes `providers.tf`, `variables.tf`,
  `locals.tf` and `cluster_contract.tf`, proving that the Kubernetes and Helm
  providers consume that same bounded kubeconfig path and context.
  The v3 gate and all protected objects remain inert unless this apply-time
  result is identical to the signed plan identity and exact v3 custodian.
- The v4 authority-object custody policy is installed by that freshly proven
  custodian, its binding is installed next, and every remaining v4 object
  depends on the binding. It protects all v3 and v4 policy/binding names, the
  exact-ingress ConfigMap and exact per-principal Roles/RoleBindings. This is
  the explicit bootstrap transition; no group-wide mutation authority is
  added.
- Controller-created database clients must carry an owner reference matching
  the exact signed live parent API version, kind, name and UID, and the request
  must come from that parent's exact controller username. The signed parent
  set equals every pre-existing database-labelled controller reconstructed
  from the raw lists. A new or replaced parent therefore uses two reviewed
  passes: create it without producing children, collect/sign its assigned UID,
  then activate children with the refreshed bundle.

The current empty root registry is deliberate fail-closed staging, not
acceptance evidence. No root, authority bundle, kubeconfig, identity, resource
or credential was created or inspected. Under the coordinator boundary the v4
regression suite was authored but not executed; no parser, test, formatter,
Terraform, Helm, build, package manager, scanner, cluster command, live probe,
deployment or cleanup ran. This successor is a candidate for independent
static review only and makes no SOURCE GO, integration, deployment or live
claim.

## Independent-review correction after `d5c19b3a`

Independent review rejected exact commit
`d5c19b3a8b3345acbec7b16bd5a2c00a455874d8` / tree
`560942184be49eea74c2ae321e7645ba6b8291dd`. Preserve that source as negative
evidence. The v4 derivation ignored ClusterRoleBinding subjects, the database
and CNPG-operator label peers lacked namespace-complete exact-owner admission,
the first v4 policy was created before any binding protected its name, root
enrollment was self-asserted, and provider group membership was not read again
during apply.

The additive v5 successor gate retains the v4 ingress contract, existing
database clients, source/tree binding and unknown apply nonces, but makes v4
activation depend on all of these additional conditions:

- Every dangerous or sensitive RoleBinding **and ClusterRoleBinding** is
  reconstructed from raw Role/ClusterRole and binding responses. Each record
  includes the binding scope/name/UID, exact role reference, rules digest and
  subject. The dual-signed cluster-authority review digest is recomputed from
  the cluster-wide subset; cluster subjects can no longer disappear behind a
  namespaced-only filter.
- The signed workload closure now lists all Pod-producing resource kinds in
  both `fs2-data` and `cnpg-system`, plus the exact
  `postgresql.cnpg.io/v1` `Cluster/fs2-control-db`. Every existing database Pod
  must point to that live Cluster name and UID. Every labelled CNPG operator
  Pod must resolve to a signed live standard-controller parent, and its
  controller username, groups and authentication extras are taken from an
  authenticated SelfSubjectReview already in the principal closure. A new
  fail-closed policy covers Pods and controller templates in both namespaces,
  so a direct Pod or spoofed label/owner reference is denied.
- The source-owned bootstrap-guard contract is a prerequisite, not a resource
  created in the same apply. Plan evidence lists the active policy/binding;
  the identity nonce re-reads them immediately before mutation; and the final
  nonce re-reads the admission sets, rejects changes to pre-existing objects,
  and permits only the enumerated v4/v5 additions protected by that guard.
- Evidence-root enrollment needs a detached Ed25519 receipt from an external
  Platform Security authority whose exact registry snapshot is a retained
  strict ancestor of the root commit. Both new registries are intentionally
  empty and `ENROLLMENT_REQUIRED`; neither a future provenance document nor a
  packet can authenticate itself.
- The final apply invokes a signed-digest provider observer and accepts only a
  fresh authenticated response for the exact group-list request, endpoint and
  CA. Its response must still be complete, empty and byte-content-equivalent
  to the dual-signed plan response.

No bootstrap guard, enrollment authority, root, receipt, bundle, observer,
credential, cluster object or provider response was created or inspected in
this task. The v5 regressions are authored but deliberately unexecuted under
the coordinator boundary. This remains a fail-closed candidate for independent
static review only: no SOURCE GO, integration, deployment or live claim.

## Independent-review correction after `efb29e68`

Preliminary independent review rejected exact commit
`efb29e684e0c91b06553d76b43c487a8531016f2` / tree
`5d92ba0377f9c5aa4a00ffe9acfd26c488f5017e`. Preserve it as negative
evidence. Its supplemental apply reader treated the singleton CNPG Cluster as
a list; ClusterRoleBinding subjects were reconstructed but not restricted to
the admitted principals; `cnpg-system` namespace RBAC was absent; update
admission ignored protected labels on the old object; exact ReplicaSet UIDs
made ordinary CNPG Deployment rollouts deadlock; authenticated UID was
dropped; token, CSR and extended impersonation escalation edges were omitted;
and the provider observer was authenticated by content but later reopened by
mutable pathname.

The direct additive successor preserves the accepted external-enrollment,
bootstrap-guard, exact source/tree/ingress binding, unknown apply nonces and
fresh provider reread design, while closing those eight boundaries:

- Supplemental re-observation treats `Cluster/fs2-control-db` as a singleton
  and compares its normalized complete object before entering list-only
  pagination and `items` validation.
- Raw RBAC closure now includes Roles and RoleBindings in `cnpg-system`.
  Dangerous and sensitive ClusterRoleBinding subjects must be exact admitted
  User or ServiceAccount subjects; inherited cluster authority cannot be
  satisfied by the retired legacy group or an unlisted group.
- Both the old and new effective Pod-template labels are evaluated on UPDATE.
  A protected database or CNPG peer label can be removed only by the exact
  externally enrolled custodian, while ordinary controller updates must keep
  satisfying the new-object peer validation.
- Every current CNPG operator Deployment is a signed rollout root identified
  by API identity, name and UID. Its source-derived lineage label must be
  inherited by current ReplicaSets and Pods. Admission permits a future
  ReplicaSet from that exact Deployment and a future Pod from the
  authenticated controller only when the Pod carries the same lineage and its
  ReplicaSet owner name is in that Deployment's rollout namespace. This
  permits a new ReplicaSet UID without accepting an unrelated root.
- Collector, executor and every admitted Kubernetes principal now bind the
  non-empty UID returned by SelfSubjectReview. Credential-subject hashes,
  apply re-observation, bootstrap custody, workload custody and CNPG peer
  admission all compare that UID in addition to username, groups and extras.
- The signed SSAR and RBAC closures now include service-account token minting,
  CSR creation and approval, signer approval, and `uids`/`userextras`
  impersonation. Wildcard grants remain dangerous.
- The provider observer is opened once with `O_NOFOLLOW`, checked as a bounded
  executable regular file, hashed from that descriptor, executed through
  `/proc/self/fd` with the descriptor inherited, and checked again for stable
  device, inode, mode, size, modification time and change time before its
  authenticated response is accepted.

Activation remains deliberately fail-closed: a successor evidence packet must
contain the expanded namespace/RBAC/identity/lineage closure, and the empty
source enrollment registries still authorize nothing. The regression source
was authored but not executed. Under the coordinator boundary there was no
test, parser, formatter, Terraform, Helm, build, package-manager, scanner,
cluster, provider, database, registry, credential, deployment, probe or
cleanup action. This is a candidate for independent static review only and
makes no SOURCE GO, integration, deployment or live claim.

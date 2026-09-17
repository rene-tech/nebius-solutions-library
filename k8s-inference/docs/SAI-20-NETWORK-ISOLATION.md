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

## Final independent-review correction after `a51b1d80`

Final independent review rejected exact commit
`a51b1d80738a66774eaef945c6870ba79549a816` / tree
`619749dd238006526eeebfbfb1f151dc136740df`. Preserve it as negative
evidence. Its future-Pod exception trusted a known lineage and ReplicaSet name
prefix without an exact live ReplicaSet UID; any enrolled controller could
use any rollout root. Its authority closure covered only four namespaces,
represented TokenRequest and impersonation without the target namespace/name,
classified namespaced token-mint bindings incompletely, constrained only
ClusterRoleBinding subjects, and refreshed only the executor's authorization
at final apply. Its provider observer closed pathname replacement but still
executed mutable bytes after hashing them.

The additive successor keeps the accepted singleton reread, old/new label
checks, authenticated UID, current exact owner chain, external enrollment and
bootstrap design, while replacing those three rejected boundaries:

- A future CNPG rollout is two-pass. The exact controller named on an exact
  signed Deployment root may create only a zero-replica ReplicaSet carrying
  that root's source-derived lineage and exact Deployment name/UID owner
  reference. No future Pod is accepted by prefix or lineage alone. A fresh
  evidence packet must inventory and sign the API-assigned ReplicaSet name,
  UID, owner chain and controller actor before that exact ReplicaSet can be
  updated and before it can create Pods. Existing signed ReplicaSets and Pods
  continue through their exact live name/UID chain.
- The signed Namespace list now drives Roles, RoleBindings, SSRRs and
  ServiceAccount lists for **every** live namespace, in addition to the
  cluster-scoped Role and binding lists. TokenRequest reviews use the correct
  `serviceaccounts` plus `token` subresource and include the exact namespace
  and every inventoried ServiceAccount name, so resource-name grants cannot
  hide behind a name-less review. Exact custodian user, UID, group, extra-key
  and ServiceAccount impersonation targets are separate reviews. Token, CSR,
  signer and impersonation decisions must be denied for every non-custodian
  admitted principal and for the collector. At final apply the exact
  authenticated subject tuple for **every** admitted principal is submitted
  in fresh SubjectAccessReviews for the complete signed dangerous-action set;
  the response must match the signed decision and remain denied for every
  non-custodian. Raw namespace, ServiceAccount and RBAC lists are re-read in
  the same unknown-nonce apply gate. Dangerous RoleBinding and
  ClusterRoleBinding records may name only exact custodian subjects;
  sensitive mutation records may name only exact admitted subjects.
- The provider observer source must be a root-owned regular executable with
  no group/world write bit. Its authenticated bytes are copied into a new
  anonymous memory file, the digest is checked, all write/grow/shrink and
  further-seal operations are sealed, and only that immutable descriptor is
  executed. Source-descriptor metadata is still compared afterward as
  additional detection, but acceptance no longer depends on detecting a
  mutation after mutable bytes have already run.

The expanded packet shape intentionally invalidates prior evidence. The empty
source-owned enrollment registries still authorize no roots, and no successor
bundle or live prerequisite was manufactured. The regression assertions were
authored but not executed. No test, parser, formatter, Terraform, Helm, build,
package manager, scanner, cluster, provider, database, registry, credential,
deployment, probe, cleanup or deletion action ran. This remains an additive
candidate for independent static review only and makes no SOURCE GO,
integration, deployment or live claim.

## Final independent-review correction after `948e1836`

Independent review rejected exact commit
`948e1836b4058779aff2c0c91c62aa898968da5d` / tree
`6c42121eb240f81dbc169d7beb0d7910e39a6697`. Preserve it as negative
evidence. Although that source completed all-namespace RBAC and all-principal
SAR closure, sealed the provider observer and retained an empty fail-closed
root registry, it omitted stored-credential reads and base ServiceAccount
mutation, reopened `kubectl` by pathname after hashing, could activate its
successor admission set only once, and checked only successor names at the
final reread.

The additive correction keeps the accepted database-client, debugging,
network, observability and fail-closed activation boundaries and adds four
source contracts:

- Every namespace has a signed Kubernetes
  `PartialObjectMetadataList` inventory of Secret names, UIDs and resource
  versions. The transcript is rejected unless its request carries the exact
  metadata-only Accept header and every returned item contains only
  `apiVersion`, `kind` and `metadata`; Secret `data` and `stringData` never
  enter the packet. The unknown-nonce apply stage compares a server-rendered
  non-payload Secret table with the signed name closure. Generic and exact
  resource-name Secret `get`/`list`/`watch` decisions are included for every
  principal.
- Base ServiceAccount `create`/`update`/`patch` authority is now dangerous,
  not merely a generic workload mutation. Generic and resource-name-specific
  reviews are derived from the full Role/ClusterRole inventory and every live
  ServiceAccount name. Dangerous bindings remain limited to exact custodian
  subjects, and the final apply repeats every resulting SAR for every exact
  admitted principal.
- `kubectl` is opened once with `O_NOFOLLOW`, required to be root-owned,
  executable and not group/world writable, and copied from that descriptor
  into a sealed anonymous memory file. The copy must match the signed
  collector digest and be a native static ELF with no `PT_INTERP`; scripts and
  dynamically interpreted executables fail closed. Every kubeconfig,
  identity, inventory and SAR operation executes that same immutable
  descriptor, so no later pathname lookup can substitute evidence.
- The kubeconfig is independently opened with `O_NOFOLLOW`, restricted to a
  stable regular file owned by root or the verifier user, copied to a second
  sealed descriptor, and reused for every invocation. The selected cluster
  must contain only an HTTPS server and inline CA. The selected user must
  contain only an inline token or inline client certificate/key; exec,
  auth-provider, token-file, external CA/key/certificate, proxy and extension
  paths are therefore rejected by exact key closure. `kubectl` and the
  provider observer run with only explicit `HOME`, `LANG` and `LC_ALL`
  variables. The observer is also required to be a static ELF, so neither
  path can acquire a credential helper or executable from ambient state. The
  sealed kubeconfig digest must remain identical across identity and final
  apply gates.
- The dual-signed authorization declares an `INITIAL` or `RENEWAL`
  transition, a digest of the exact old protected admission objects and a
  digest of the exact planned eight-object successor set. Initial activation
  requires those eight names to be absent; renewal requires the complete
  protected predecessor set and stable object UIDs. Terraform supplies the
  actual source-rendered policy and binding manifests to the final verifier.
  The live reread compares every policy/binding spec, label, annotation, kind
  and name with that render, rejects any non-transition change, and exposes
  the observed digest to a Terraform precondition. This permits an ordinary
  reapply or reviewed CNPG second pass without weakening exact old/new
  custody.

The packet-shape additions deliberately invalidate prior evidence. The
source-owned root and enrollment registries remain empty and authorize no
activation. Regression assertions were authored but not executed. No test,
parser, formatter, Terraform, Helm, build, package manager, scanner, cluster,
provider, database, registry, credential, deployment, probe, cleanup or
deletion action ran. This is a source-only candidate for fresh independent
review and makes no SOURCE GO, integration, deployment or live claim.

## Preliminary independent-review correction after `17469ed79`

Preliminary static review found exact commit
`17469ed79eb56ae63327f0ddecb81d21b2170722` / tree
`7ebc5bba71c4dba7fda94a6e6bee46d35b3b7b90` still omitted
credential-equivalent Kubernetes connect subresources. Preserve that commit as
SOURCE NO-GO evidence. A subject with `pods/exec`, `pods/attach`,
`pods/portforward`, `pods/proxy`, `pods/ephemeralcontainers` or applicable
`nodes/proxy` authority could otherwise enter or tunnel to a credential-bearing
admitted workload without appearing in the Secret/ServiceAccount closure.

The additive successor treats each applicable connect verb as dangerous
authority. For every namespace it emits generic SARs for Pod connect and
ephemeral-container operations; `nodes/proxy` remains cluster-scoped. It also
derives every `resourceNames` target from the raw Role and ClusterRole rules
and emits exact name-bearing SARs, including ClusterRole targets across the
complete signed namespace set. Wildcard resource or verb grants expand to the
same source-defined action closure.

RoleBinding and ClusterRoleBinding reconstruction now selects these
subresources as both dangerous and sensitive. Every dangerous binding subject
must be an exact enrolled custodian, while the existing unknown-nonce final
apply repeats every generic and name-exact decision for every authenticated
principal and rejects any non-custodian allow. No Pod, node, credential or
Secret payload is read by this source change.

The authored regression is intentionally unexecuted. No test, parser,
formatter, Terraform, Helm, build, package manager, scanner, cluster,
provider, database, registry, credential, deployment, probe, cleanup or
deletion action ran. This remains a source-only candidate for fresh exact
review and makes no SOURCE GO, integration, deployment or live claim.

## Final independent-review correction after `6e1bf0f00`

Exact `6e1bf0f00d85a80d228a7cea803511391076fb5a` / tree
`9c99de4e4eacf44fc36f7323a8befa16b4bedbb0` remains preserved
SOURCE/INTEGRATION/LIVE NO-GO evidence. The successor closes four distinct
gaps without treating every tenant Pod as a platform credential target.

The Kubernetes and Helm providers no longer receive the durable run-root
kubeconfig pathname. The source-bound `inference-stack` launcher copies that
private file once into a mode-0400 sealed memfd, retains the descriptor across
the complete workloads plan/apply pair, and passes
`/proc/<launcher-pid>/fd/<fd>` as a required Terraform variable. Both providers,
Terraform's kubeconfig parsing and the v4/v5 identity gates consume that same
descriptor. The verifier rejects a non-memfd, a missing seal, a foreign owner,
an unexpected mode or name, and Terraform compares its own file digest with
both verifier phases. The durable pathname remains only an operator recovery
input and must hash to the sealed plan snapshot; it is not provider authority.

`pods/proxy` now closes `create`, `delete`, `get`, `patch` and `update` in the
generic and `resourceNames`-exact Role/ClusterRole, SAR and final-apply paths.
The other Pod-connect, ephemeral-container and node-proxy actions retain their
source-defined verb sets.

The signed evidence now derives a credential workload inventory from exact raw
Pod/controller lists plus the all-namespace ServiceAccount and metadata-only
Secret inventories. Every ServiceAccount and Secret in `fs2-system`,
`fs2-observability`, `fs2-data` and `cnpg-system`, and every live Pod selecting
one, becomes an exact protected target. A new source-exact admission pair
denies selecting those credentials unless the actor is the exact custodian,
an exact signed release writer retaining the existing credential surface, or
an exact kube-system controller creating a source-derived owner child with the
same surface. Deployment ReplicaSets and CronJob Jobs are admitted only inert;
their exact UID must enter a fresh signed renewal before dependent Pods start.
This prevents an innocuously labelled direct Pod or controller from mounting a
platform Secret or selecting a privileged ServiceAccount.

Pod-connect authority is target-classified. Generic authority is rejected for
any namespace containing a protected Pod, while credential-free tenant targets
outside that set are not converted into custodian-only operations. A protected
target exception must be an exact-name Role and RoleBinding, reference the
exact live Pod UID, name an independently authenticated principal, carry tenant,
audit, issued/expiry and reason-digest fields, and expire within 900 seconds.
The exact debug broker is separately authenticated; admission permits it to
manage only the source-defined Pod-subresource lease shape. All active leases,
their exact SAR decisions and their RBAC objects are signed and reobserved at
the final unknown-nonce apply. HTTP request-debug capture is untouched.

The successor activation set now contains twelve exact policy/binding objects,
including workload-credential and debug-access custody. The bootstrap guard,
signed INITIAL/RENEWAL transition and final source/live digest cover the entire
set. Empty external enrollment registries still fail closed. Tests were
authored but not executed, and no Terraform, Helm, provider, cluster, database,
credential, deployment, cleanup or deletion action was performed.

### Superseding dirty-WIP correction: authorization lifetime and release custody

The twelve-object draft above is preserved as negative evidence. The successor
set now has fifteen source-bound objects. It adds a fail-closed, independently
operated validating webhook for every protected Pod connect request and a
cluster-scoped RBAC custody policy/binding. The authorizer contract binds the
exact principal, operation, tenant, Pod UID, lease digest, source policy digest,
issued time and expiry to each decision. Stale, replayed, mismatched or
unavailable authorization is denied, so an expired Role/RoleBinding cannot
continue granting protected access even if the RBAC objects still exist.

Credential custody no longer classifies every ServiceAccount as privileged.
It derives privileged identities from admitted principals and the exact
dangerous/sensitive RBAC closure, keeps metadata-only Secret inventories, and
allows ordinary credential-free creation. A privileged CREATE instead needs a
signed exact principal, namespace, resource, name and complete Pod-spec digest;
native controller children and managed updates remain bound to exact live
owner UID and unchanged credential surface. This preserves normal release,
scientific workload and customer controller paths without reopening arbitrary
ServiceAccount or Secret selection.

One sealed, descriptor-backed kubeconfig snapshot is now retained across both
foundation and workloads plan/apply paths, foundation helper invocations and
destroy planning. Kubernetes and Helm providers receive only that snapshot;
the durable pathname is retained solely as the read-only source whose digest
must match the sealed copy. The pre-existing bootstrap guard also covers
arbitrary ClusterRole and ClusterRoleBinding changes until the source-exact
successor custody policy is active.

The external authorizer and enrollment roots remain explicit integration gates;
this source candidate does not claim they exist. Regression tests were authored
but not executed under the coordinator boundary. No Terraform, Helm, provider,
cluster, database, credential, deployment, cleanup or deletion action was
performed, and this candidate is not a source, integration or live GO claim.

## Final independent-review correction after `e8ac34b7`

Exact `e8ac34b7b9dd670015655d43cb24d14907abf8f1` / tree
`fddc6702a78ad54d3f95d830b880e8e2524edfda` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO evidence. Its static and live credential
classifiers differed, post-attestation Secret names were outside custody,
replacement Pod UIDs could bypass debug leases, and a newly admitted active
controller could be accepted while its still-unattested children were denied.

The successor has one source-owned Pod Secret-reference contract consumed by
both the Python evidence verifier and the generated admission CEL. It covers
container, init-container and ephemeral-container environment references,
image-pull references, projected and direct Secret volumes, CSI
`nodePublishSecretRef`, and the legacy Azure File, CephFS, Cinder, FlexVolume,
iSCSI, RBD, ScaleIO and StorageOS volume fields. Every non-empty reference is
credential-bearing regardless of the point-in-time Secret inventory. A signed
CREATE may name only a Secret present in the fresh metadata-only inventory, so
a later Secret cannot be consumed until a new signed evidence generation.
Secret contents remain excluded.

The external debug-authorizer attestation now binds every current Pod by
namespace, name, UID, credential classification and surface digest. An unknown
or replacement UID is denied until a fresh signed generation classifies it;
only an exact signed credential-free UID may use the no-lease branch. This
keeps controller replacements from inheriting stale debug authorization while
retaining the audited, tenant-scoped, per-request lease path.

The credential-custody policy also matches `pods/ephemeralcontainers` UPDATE,
using the full new and old Pod specs. A protected-Pod debug update must match
the exact signed lease principal, namespace, Pod name and UID and must preserve
the ServiceAccount, token-automount state and every non-empty Secret-reference
group. The separate fail-closed webhook still enforces that same lease's
server-time expiry and replay rules on every request. Thus a credential-free
ephemeral container remains usable for authorized debugging, while `env` or
`envFrom` Secret injection is denied.

A credential-bearing controller CREATE now binds the complete object spec and
must be inert: scalable controllers use zero replicas, Job and CronJob objects
are suspended, and DaemonSets use a source-owned contradictory required node
affinity. Release UPDATE authority is limited to an exact existing object
name and UID in the signed workload inventory. After fresh re-attestation of
the new parent's UID and credential surface, the exact release actor may
activate it and the exact native controller may create owner-bound children.

Regression tests were authored but not executed under the coordinator
boundary. No parser, formatter, Terraform, Helm, build, package manager,
scanner, provider, cluster, database, registry, credential, deployment,
probe, cleanup or deletion action ran. This remains a source-only candidate
for fresh independent review and makes no SOURCE GO, integration, deployment
or live claim.

## Final independent-review correction after `1d00f138`

Exact `1d00f13842ea0287b1aefa628bc0f224c461c65c` / tree
`4f3190b728569477f3b610f8dfa5eab949f312bb` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO evidence. Its leased ephemeral debugger could
mount credential-bearing volumes, explicit projected ServiceAccount tokens
were outside credential classification, dynamically created ReplicaSet and Job
UIDs wedged ordinary multi-level rollouts, and the native-controller identity
was assumed rather than derived from authenticated evidence.

The successor requires every `pods/ephemeralcontainers` lease to contain an
exact full debugger spec and content digest. The image is digest-pinned, the
command is explicit, and the security context is non-root, read-only,
no-privilege-escalation, drop-all and RuntimeDefault. Environment credential
sources, `volumeMounts` and `volumeDevices` are absent. Admission requires the
new ephemeral-container list to equal the old list plus exactly that signed
spec, so the debugger cannot attach Secret, projected-token, CSI, PVC,
hostPath or runtime credential volumes already present on the Pod. The spec
also omits `targetContainerName`, and a Pod using host PID, IPC or network, or
a shared process namespace, cannot receive an ephemeral-debug lease.

The shared credential contract now also names projected
`serviceAccountToken` sources. Static evidence and CEL preserve their exact
audience, expiration and path surface; any such source is credential-bearing
even when the selected ServiceAccount is otherwise unprotected and implicit
automount is disabled. Signed create, update, controller-child and debug paths
all compare the same token-projection surface.

Native parent/child transitions are supplied by the dual-signed authorization
packet and must cover every source-defined controller edge. Each transition is
resolved to the exact SelfSubjectReview identity of one admitted controller;
the source no longer assumes a kube-controller-manager username or groups.
Credential-bearing Deployment and CronJob roots carry a UID- and
credential-surface-derived lineage through both object and Pod templates. The
exact observed controller may create the first exact-owner ReplicaSet or Job,
and only that same identity may carry the lineage and unchanged credential
surface through the dynamic ReplicaSet-to-Pod or Job-to-Pod hop. This permits
ordinary atomic rollouts without pre-inventorying a new intermediate UID while
denying another actor or root lineage.

New top-level privileged controllers remain inert on signed CREATE. Their live
UID and derived lineage must be captured in a fresh signed generation before
activation; after that transition, intermediate controller UIDs may rotate
atomically under the authenticated lineage. Existing and newly observed
lineage chains are part of the signed workload inventory and are compared
across plan, identity and final apply evidence.

The regression tests for these four findings were authored but not executed.
No parser, formatter, Terraform, Helm, build, package manager, provider, live
system, credential, deployment, cleanup or deletion action ran. External root
enrollment and authorizer operation remain fail-closed integration gates. This
is a source-only candidate for independent review, not a SOURCE, integration,
deployment or live GO claim.

## Final independent-review correction after `23aa56e6`

Exact `23aa56e61b5843744636b8112091eaa93ec43407` / tree
`4e01cdb3af39b255f19acc6d211aa0aa2b150ef5` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO evidence. Its ordinary workload UPDATE paths
could admit an ephemeral-container subresource request, API-injected projected
ServiceAccount tokens made controller-template and child-Pod surfaces differ,
certificate signer `sign` authority was outside the closure, and debug
activation had neither an exact model scope nor a durable 90-day decision
record.

The workload custody policy now treats subresources as an exclusive branch.
Every ordinary CREATE or UPDATE path requires an empty subresource. Every
`pods/ephemeralcontainers` request, including one for a credential-free Pod or
from the ordinary custodian identity, must instead match the exact signed
principal, Pod name and UID, operation, and no-volume debugger specification.
The fail-closed request-time authorizer remains a second gate. This prevents an
ordinary writer or native-controller UPDATE allowance from bypassing the
ephemeral debugger constraints.

ServiceAccount-admission normalization is derived from authenticated current
Pod GETs rather than a random volume name or caller assertion. The gate requires
one exact cluster-wide `kube-api-access-*` projection profile, binds it to every
currently inventoried ServiceAccount and its observed automount default, removes
only that exact API-injected volume from the explicit surface, and adds the
profile's audience, expiration and token path as a deterministic effective
surface. Empty Secret-reference groups introduced by the injected volume are
also normalized on both the static and CEL sides. Controller templates and
their API-mutated Pods can therefore compare equal without hiding a different
explicit token projection. A direct signed Pod must set
`automountServiceAccountToken: false`; any needed token must be a stable,
explicit, reviewed projection.

The dangerous-authority matrix now covers the `sign` verb on
`certificates.k8s.io/signers`, including generic and `resourceNames`-exact SARs.
RoleBinding and ClusterRoleBinding derivation classifies the same right as both
dangerous and sensitive, and the final apply-time loop rechecks every admitted
principal's exact decision.

Each debug lease now contains a dual-signed customer activation record binding
one authenticated principal, exact tenant, exact model, Pod name and UID,
reason, audit ID and request digest. Activation lasts at most seven days; an
individual authorization lease remains at most 900 seconds and must fit wholly
inside that activation. The external authorizer attestation must also bind a
source-owned record contract and a separately TLS-pinned append endpoint. Both
allow and deny records must be durably committed before the admission response,
remain append-only for exactly 7,776,000 seconds, and exclude tokens, keys,
Secret data and customer request/response bodies. The existing customer-request
debugging implementation is not modified here; integration must retain its
`/v1/storage/credentials` capture exclusion and all existing redaction/body-cap,
tenant-access and purge controls.

The external enrollment roots, debug authorizer and durable record store remain
explicit integration gates. Tests were updated as authored regression evidence
but were not executed. No parser, formatter, Terraform, Helm, build, package
manager, scanner, provider, cluster, database, registry, credential, deployment,
probe, cleanup or deletion action ran. This is a source-only candidate for fresh
independent review, never a SOURCE GO, integration, deployment or live claim.

## Final independent-review correction after `7dd0f895`

Exact `7dd0f8951e5eb7ab5e728f6b76b93ccced09b039` / tree
`b9da53e85790b1eefc091e0724a1688d07fe5fc4` is preserved as
SOURCE/INTEGRATION/LIVE NO-GO evidence. It truthfully required a durable
allow/deny record append for normal admission decisions, but its webhook
declared `sideEffects: None` and did not specify `AdmissionRequest.dryRun`.
That registration falsely asserted that the webhook never caused an external
side effect.

The successor renders `sideEffects` from the source-owned authorizer contract,
which now requires `NoneOnDryRun`. For an explicit `dryRun: true` request, the
authorizer must deny before evaluating debug authority, consuming or replaying
a lease, or contacting the record store. Only `dryRun: false` or an absent
field enters normal authorization; those requests still require the durable
allow/deny append before the admission response and the exact 90-day retention
contract. A dry-run therefore cannot exercise debug authority or produce a
record while the normal request path retains its required audit side effect.

The dual-signed authorizer attestation now carries the exact side-effect mode
and a digest of the dry-run behavior. The verifier recomputes that digest from
the committed contract, validates the non-dry-run-only record scope, and the
webhook object records the same digest as an annotation. A future integration
packet cannot assert a different dry-run behavior without failing source and
attestation checks.

Exact static regressions were authored for the webhook mode, normal/dry-run
split, no dry-run lease consumption or record write, signed behavior digest,
and rejection of the prior `sideEffects: None` declaration. They were not
executed. No parser, formatter, Terraform, Helm, build, package manager,
scanner, provider, cluster, database, registry, credential, deployment, probe,
cleanup or deletion action ran. This remains a source-only candidate for fresh
independent review, never a SOURCE GO, integration, deployment or live claim.

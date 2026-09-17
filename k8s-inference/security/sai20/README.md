# SAI-20 authority roots

`authority-roots-v1.json` is the sole trust root for SAI-20 database-network
activation. Terraform variables, environment variables, authority packets and
filesystem paths cannot add or replace roots.

The initial registry is deliberately `ENROLLMENT_REQUIRED` with no keys. This
makes the source successor fail closed until Platform Security supplies two
distinct Ed25519 public roots in a later additive commit: one evidence
collector and one independent reviewer. Each root must name an accepted Git
commit/tree, a committed public-key source path/blob and an independent review
receipt whose full normalized content is in that provenance document. The
verifier resolves those Git objects, recomputes the receipt and derives the
key ID from the raw public key; a caller-selected key is never trusted.

Root enrollment is a separate review event. Never replace or remove an enrolled
root in place. Rotation adds a new generation and retains prior provenance
until every packet and saved plan using it has expired. Private keys, tokens,
kubeconfigs and provider credentials must never enter this directory.

The registry's current empty state is not an operational authority packet and
does not authorize a plan, apply, integration or deployment.

## Enrollment protocol

Enrollment is additive and requires two reviewed commits. The first commits a
non-secret root document containing the public key, role, exact principal and
group, plus a content-derived acceptance record for the reviewed source. A
later independently reviewed commit may append that document's immutable Git
commit/tree/path/blob identity to the registry. Collector and reviewer roots
must have different keys, principals and groups. A Terraform input, packet,
environment value or uncommitted filesystem document can never enroll a root.

The evidence collector must use its enrolled identity to preserve raw request
and response bytes, transport endpoint/CA/TLS identities, API audit/request
IDs and observation times. The signed bundle must include authenticated raw
Kubernetes lists, the collector's own SelfSubjectReview and
SelfSubjectRulesReview results, per-principal identity/rules/access reviews,
and the authoritative provider group-membership response. The verifier, not
the collector's summary, reconstructs every list receipt and permission or
impersonation digest from those bytes.

No enrollment or evidence collection was performed while authoring this
source-only successor.

## v5 external enrollment and bootstrap prerequisites

Independent review rejected `d5c19b3a8b3345acbec7b16bd5a2c00a455874d8`
because the provenance document could assert its own acceptance. The v5 gate
therefore does not treat that document, a reviewer string, a Terraform value
or the v5 packet as an enrollment authority. Every evidence root additionally
needs a detached Ed25519 receipt in `root-enrollment-receipts-v1.json`. The
receipt statement binds the root key, role, principal, group and immutable Git
provenance, and its signer must be a retained
`platform-security-enrollment-authority` from an `ACTIVE`
`enrollment-authorities-v1.json` snapshot in a strict-ancestor commit. The
verifier resolves that historical commit/tree/path/blob and validates the
signature. The committed authority and receipt registries are empty and
`ENROLLMENT_REQUIRED`, so this source cannot activate or self-bootstrap.

The successor also requires a source-exact, already active bootstrap
ValidatingAdmissionPolicy and `Deny` binding. Their complete specs are defined
in `contracts/sai20-bootstrap-guard-v5.json`; raw API lists are signed and the
identity-stage apply read re-observes them before any v4/v5 policy is created.
The guard protects its own names and every successor policy/binding name. This
repository intentionally does not create that prerequisite: an integration
owner must establish and independently accept it under an earlier trusted
platform admission boundary, then supply its immutable UID/resourceVersion
receipt. If no such prior boundary exists, activation stays blocked.

Provider group membership is not a plan-only assertion. The v5 bundle signs
the exact observer executable digest and credential-subject digest. The final
unknown-nonce apply gate executes that observer, validates a fresh raw
provider transcript against the pinned endpoint and CA, and requires its empty
membership response to equal the signed response before the database policy
can become authoritative. No provider executable, credential or response is
committed here.

## Post-`efb29e68` authority closure

Exact `efb29e684e0c91b06553d76b43c487a8531016f2` remains rejected. Its
external enrollment and pre-existing bootstrap concepts are retained, but a
replacement packet must additionally bind authenticated principal UIDs,
`cnpg-system` Roles/RoleBindings, exact admitted ClusterRoleBinding subjects,
service-account token and CSR/signing escalation reviews, and source-derived
CNPG Deployment rollout lineages. Apply re-observation distinguishes the
singleton CNPG Cluster from list endpoints, and the provider observer is
executed from the same no-follow file descriptor whose bytes were hashed.

The peer admission contract evaluates both old and new template labels on
UPDATE. It accepts future CNPG ReplicaSet UIDs only through an exact signed
Deployment root, inherited rollout lineage and exact authenticated controller
identity. These additions do not enroll a root or authorize activation; the
source-owned registries remain empty and fail closed pending independent
external enrollment and review.

## Post-`a51b1d80` exact actor and authority closure

Exact `a51b1d80738a66774eaef945c6870ba79549a816` remains rejected. A replacement
packet must derive its namespaces from the complete signed Kubernetes
Namespace response, list Roles, RoleBindings and ServiceAccounts in every
namespace, and bind that complete inventory into all plan/identity/apply
phases. TokenRequest reviews identify the `token` subresource and exact
namespace/ServiceAccount name. Exact custodian user, UID, groups, extra keys
and ServiceAccount identity are explicit impersonation targets. Every
non-custodian and the evidence collector must be denied the complete dangerous
review set; final apply uses fresh SubjectAccessReviews for every exact
admitted subject, rather than treating an admitted RoleBinding subject or the
executor's own review as proof about another principal. Dangerous namespaced
and cluster bindings may resolve only to exact custodian subjects.

Rollout lineage is not Pod authority. Only the root's exact signed controller
actor may create a zero-replica ReplicaSet beneath the exact signed Deployment
name and UID. A later packet must sign the live ReplicaSet name, UID and owner
chain before it can be updated or own a Pod. Name-prefix and lineage-only Pod
branches are forbidden.

The apply-time provider observer must be root-owned and not group/world
writable. The verifier copies its authenticated bytes to an anonymous memory
file, applies write/grow/shrink/seal seals, and executes only that immutable
descriptor. The original open descriptor is retained solely for a secondary
metadata-stability check. No observer, credential, packet or root is included
here; the empty registries continue to fail closed.

## Post-`948e1836` credential and renewal closure

Exact `948e1836b4058779aff2c0c91c62aa898968da5d` remains rejected. A
replacement packet must inventory Secret identities in every namespace using
only Kubernetes `PartialObjectMetadataList` responses; Secret values are
forbidden from the evidence packet. It must derive generic and exact
resource-name Secret-read and base-ServiceAccount-mutation SARs from the full
Role/ClusterRole closure and refresh those decisions for every exact principal
at final apply. Secret readers and ServiceAccount mutators are dangerous
authority and may resolve only to exact custodian subjects.

The signed collector `kubectl` is no longer reopened by pathname. It must be a
root-owned, non-group/world-writable static ELF whose exact signed bytes are
copied to and executed from a sealed memory descriptor for all apply-time
identity, inventory and authorization reads. Script or dynamic-interpreter
execution fails closed.

The kubeconfig is separately copied into a sealed descriptor and every
`kubectl` process receives that descriptor rather than the caller pathname.
Only a direct HTTPS server, inline CA, and either an inline token or inline
client certificate/key are accepted for the selected context; exec,
auth-provider, token-file, external credential/CA files, proxy and extension
configuration fail exact-key validation. Both `kubectl` and the provider
observer receive a minimal explicit environment, and the observer must also
be a root-owned sealed static ELF. Identity and final apply must report the
same sealed kubeconfig digest.

Successor admission activation is a signed state transition. `INITIAL` binds
an empty predecessor set; `RENEWAL` binds the exact prior protected object
set. Both bind the digest of the eight source-rendered successor
policy/binding manifests. The final unknown-nonce reread compares their exact
specs and source metadata—not merely their names—and Terraform requires the
live digest to equal the signed planned digest. This supports a reviewed
second rollout pass and ordinary reapply while retaining exact old/new
custody. It does not enroll a root or authorize activation; the empty
registries remain the integration gate.

## Post-`17469ed79` connect-subresource closure

Exact `17469ed79eb56ae63327f0ddecb81d21b2170722` remains preliminary SOURCE
NO-GO evidence. Stored-credential closure now includes the Kubernetes
connect/subresource pivots `pods/exec`, `pods/attach`, `pods/portforward`,
`pods/proxy`, `pods/ephemeralcontainers` and `nodes/proxy` with their
applicable connect or mutation verbs.

The verifier emits namespace-generic reviews plus exact `resourceNames`
reviews derived from every raw Role and ClusterRole. The cluster-scoped node
proxy path has no fabricated namespace. Both namespaced and cluster binding
derivation classify the grants as dangerous and sensitive, dangerous subjects
must be exact custodians, and final apply refreshes the entire expanded review
set for every authenticated principal. This changes no workload or debugging
feature and does not read Pod, node or credential contents.

## Post-`6e1bf0f00` provider and credential-target closure

Exact `6e1bf0f00d85a80d228a7cea803511391076fb5a` is rejected and must not be
used as activation authority. The actual Kubernetes and Helm providers now
consume one launcher-retained, mode-0400, fully sealed memfd kubeconfig for the
whole workloads plan/apply pair. Direct Terraform invocation without that
descriptor fails closed; verifier and Terraform digests must match it.

The Pod-connect closure is target-aware. All platform-namespace
ServiceAccounts and metadata-only Secret names, the exact credential-bearing
Pod targets, and every active debug lease are content-derived from signed raw
inventories. Generic access to a protected namespace is custodian-only;
credential-free tenant targets remain available. Protected access requires an
exact-name, exact-Pod-UID, audited and tenant-scoped lease of at most 900
seconds from the exact broker. Admission separately prevents new unlabeled
workloads from selecting protected ServiceAccounts or Secrets and prevents
future broad RBAC from bypassing the signed closure. `pods/proxy` covers all of
`create`, `delete`, `get`, `patch` and `update`.

## Superseding dirty-WIP correction

The prior twelve-object draft is retained as negative evidence. The successor
activation set contains fifteen exact objects: a per-request, fail-closed debug
authorizer webhook and cluster-scoped RBAC custody policy/binding were added.
Protected Pod connect requests must match a currently active, exact-principal,
exact-operation, exact-tenant and exact-Pod-UID lease. The signed authorizer
contract denies stale, replayed, mismatched and unavailable decisions, so RBAC
objects that outlive their annotations do not extend authorization.

Credential protection is now derived from admitted principals plus the exact
dangerous/sensitive RBAC closure instead of treating every ServiceAccount as
privileged. Ordinary credential-free creates remain available; privileged
creates require a signed full Pod-spec contract, and controller children remain
bound to exact live owner UID and unchanged credential surface. ClusterRole and
ClusterRoleBinding mutation is custodian-only and is covered by the pre-existing
bootstrap guard during activation.

The same sealed descriptor-backed kubeconfig is retained across foundation and
workloads provider/helper paths. Empty external enrollment and debug-authorizer
roots remain fail-closed integration gates. Tests were authored but not run;
there was no Terraform, Helm, provider, live, credential, cleanup or deletion
action. This is a static candidate, not a source/integration/live GO claim.

## Post-`e8ac34b7` dynamic credential closure

Exact `e8ac34b7b9dd670015655d43cb24d14907abf8f1` / tree
`fddc6702a78ad54d3f95d830b880e8e2524edfda` remains rejected. The successor
uses one source-owned field contract for both static and admission-time Pod
Secret-reference classification, including CSI, FlexVolume and every listed
legacy volume Secret field. Any non-empty Secret reference is privileged even
when its name was created after the signed inventory; an exact CREATE contract
may consume it only after a fresh metadata-only inventory has observed it.

Debug authorization binds an exact signed inventory of all current Pod UIDs
and classifications. Unknown and controller-replacement UIDs fail closed until
the next signed generation. Ephemeral-container UPDATE crosses both the exact
lease webhook and credential custody; the latter compares the complete old/new
Pod specs and permits no ServiceAccount, token-automount or non-empty Secret
reference change. Privileged controller CREATE contracts bind the full object
spec and require a non-running state. Their exact name and UID must then enter
the signed workload inventory before a release UPDATE can activate them or an
exact native controller can create owner-bound children.

The added regressions are source-only and were not executed. No test, parser,
formatter, Terraform, Helm, provider, live, credential, cleanup or deletion
action occurred. Empty external enrollment and debug-authorizer roots remain
fail-closed integration gates; this is not a source, integration or live GO
claim.

## Post-`1d00f138` debugger, projected-token and rollout closure

Exact `1d00f13842ea0287b1aefa628bc0f224c461c65c` / tree
`4f3190b728569477f3b610f8dfa5eab949f312bb` remains rejected. A leased
ephemeral debugger is now an exact dual-signed full container spec with a
digest-pinned image, explicit command and least-privilege security context.
It may carry neither environment sources nor `volumeMounts`/`volumeDevices`,
and admission permits only the exact append of that spec to the old ephemeral
container list. Existing Secret, projected-token, CSI, PVC, hostPath and
runtime volumes therefore cannot be attached by the debugger. Target-container
process sharing is forbidden, and Pods using host PID/IPC/network or a shared
process namespace fail closed for this debugger path.

The single source-owned credential-field contract additionally classifies
every explicit projected ServiceAccount token by audience, expiration and
path. That surface is credential-bearing and is compared unchanged on signed
release, controller and debug paths even when implicit automount is disabled.

The authorization packet now declares every native workload-controller edge,
and each edge resolves to the exact live SelfSubjectReview identity of an
admitted controller. No controller username or group is hard-coded. Signed
Deployment and CronJob root UID/credential digests produce a propagated
lineage. Exact-owner first hops and authenticated lineage-bound
ReplicaSet-to-Pod and Job-to-Pod second hops preserve atomic rollout without
trusting a name prefix or requiring every newly generated intermediate UID to
exist in the prior inventory. A new privileged root still starts inert and
requires fresh signed UID/lineage attestation before activation.

Tests were authored but not executed. No parser, formatter, Terraform, Helm,
provider, cluster, credential, deployment, cleanup or deletion action ran.
The empty enrollment registries and external debug authorizer remain
integration gates, and this source-only successor makes no GO claim.

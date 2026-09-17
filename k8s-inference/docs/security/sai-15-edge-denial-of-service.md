# SAI-15 edge denial-of-service remediation

Status: source candidate only. This document records the unexecuted static
implementation authored on 2026-09-17. It is not integration, deployment, live
verification, or security acceptance evidence.

## Source contract

The public HTTPS and HTTP listeners now share one Gateway-level
`BackendTrafficPolicy`. Attaching both listener sections, rather than
enumerating application routes, extends the baseline to the landing website,
API, admin console, Grafana, HTTPS routes added later, and the port-80 redirect
and ACME solver routes. Each observed IPv4 client receives an independent
global bucket (`Distinct` over `0.0.0.0/0`) at 200 requests/second per route.
Requests under `/admin` also enter a separate 30 requests/minute client bucket.
The ordinary ACME challenge volume remains below the HTTP listener's per-client
baseline and does not match the `/admin` rule.

Client identity is fail-closed at source. No root or workloads variable accepts
a verification verdict, trusted-hop count, provider digest, signer key, or
direct-access boolean. Public workloads planning instead reopens the fixed
mode-0600 `<run_root>/edge-client-identity-receipt.json`, recomputes its payload
SHA-256, and verifies its Ed25519 signature against the source-owned issuer
registry. The registry is intentionally empty in this source candidate, so no
public activation is possible until Platform Security onboards its public key
in a separately reviewed commit. An arbitrary caller key cannot be supplied by
tfvars or the external-provider query.

The signed payload must equal the exact Terraform project, cluster, allocation,
public IPv4, network, subnet, worker security group, ingress rule, source CIDRs,
destination ports, Gateway, and HTTP/HTTPS listener contract. It also names the
actual provider load balancer, both provider listeners, backend identity,
Kubernetes Service name/UID, route tables, and the same SG/rule. Nonzero raw
provider-export and probe digests, a maximum 24-hour validity window, canonical
JSON, a nonce, and the authenticated issuer are mandatory.

The verifier—not the receipt author—derives `numTrustedHops` from the ordered
provider proxy chain. It accepts only observed append/overwrite semantics that
append the downstream remote address and make an untrusted client-supplied XFF
prefix irrelevant. For this exact topology the sole hop must be the signed
provider LB. It derives direct-access exclusion only when signed SG/routing
facts show the LB as the sole public entrypoint, no worker public addresses,
and no public ClusterIP, NodePort, or target-port route. The HTTP and HTTPS
`ClientTrafficPolicy` objects consume only that verifier projection. Direct
Helm assertion is not a supported public-edge deployment path.

Global counters use Envoy Gateway's rate-limit service and a network-isolated,
three-member Redis replication group supervised by a three-Sentinel quorum.
RLS receives the documented Sentinel URL form (logical master name followed by
three stable Sentinel endpoints), so it discovers one writable authority and
never sends writes through a Service that balances independent Redis servers.
A restarting StatefulSet member asks Ready Sentinels for the current primary;
only a no-quorum bootstrap uses ordinal zero. A two-Pod PDB, failover quorum,
required hostname anti-affinity, three-domain `minDomains` spread, probes, and
bounded resources cover a member/update loss. Public mode is rejected at both
the root and infrastructure stage unless the fixed regular system pool has at
least three nodes. Infrastructure emits an exact availability receipt naming
that node group, count, five-label selector, topology key, and minimum domain
count, plus the node-group update strategy and minimum retained capacity. The
selector includes the provider-owned `nebius.com/node-group-id` and the
task-owned `lifecycle.fs2.nebius/run`, not only generic system-pool labels.
Public mode requires positive surge, at most one unavailable system node, and
at least two nodes retained during an update. Foundation reopens the fixed
run-owned infrastructure state directly, compares both the whole receipt and
its canonical SHA-256, and queries the selected Nodes before creating the
foundation contract. Every eligible Node must have the exact group/run labels,
a non-empty UID and Kubernetes `resourceVersion`, Ready status, schedulable
state, no `NoSchedule` or `NoExecute` taint, and one of at least three distinct
`kubernetes.io/hostname` values. The edge workloads carry no hard-taint
tolerations. Workloads independently reread those exact Nodes, recompute the
UID/resourceVersion-bound receipt, and require every foundation UID to remain
currently eligible; neither a wrapper copy nor a saved foundation snapshot is
sufficient. Those plan-time checks remain an early diagnostic, but they are not
deployment authority.

Public mode additionally has two creation-only apply gates: one ordered before
the Redis/Sentinel StatefulSet and one after all workloads prerequisites but
before the control-plane Helm release. Each new plan embeds `plantimestamp()`
and is refused when it is more than four hours old. That plan-identity window
accommodates the declared 10–30 minute prerequisite waits, including both
15-minute cache bootstrap Jobs; it is not reused as the current-state clock.
Each protected resource
also consumes a second, deferred `data.external` mutation fence from its own
lifecycle precondition. That fence cannot be removed from the resource graph
while leaving the public mutation enabled. It repeats the complete authority
join after the prerequisites and records a whole-second `observed_at` only after
its fresh reads. In foundation both reads are ordered after the bootstrap
ConfigMap, headless and Sentinel Services, PDB, NetworkPolicy, and continuous
Node-authority binding, so delaying the StatefulSet cannot move the final read
ahead of an unprotected prerequisite.
The StatefulSet or Helm release proceeds only when the result says `PASS` with
at least three provider members, eligible Nodes, and hostname domains. The
Kubernetes scheduler is the final live fence at every Pod bind and re-evaluates
the exact selector, Ready/cordon/taint state, and required anti-affinity rather
than trusting the receipt as a scheduling decision.

At apply time both fences use only a source-enrolled provider observer. The
signed observer authority binds the Nebius API endpoint, credential authority,
credential subject, audience, immutable configuration digest, adapter digest,
and executable digest. The observer gets the exact cluster, derives its exact
provider project, explicitly pages through every NodeGroup under the cluster
and every Compute instance under the project, and gets the exact cluster,
NodeGroup, and candidate member instances before and after the Kubernetes
observations. No CLI profile, caller `HOME`, or caller-supplied endpoint crosses
this boundary. Enumeration uses bounded `page_size=100`
requests, follows each unique continuation token, rejects repeated resources,
tokens and empty continuation pages, and accepts only an observed terminal
empty token. It never treats a one-page result or CLI `--all` convenience as
pagination proof. Provider resource versions and canonical cluster/NodeGroup
objects must remain unchanged across the sandwich. A stable epoch requires a
`RUNNING`, non-reconciling NodeGroup with zero outdated nodes. A signed
prepare/cutover epoch permits only the contracted positive surge while the
fixed desired count remains unchanged and all three signed serving Nodes stay
eligible. Public mode now requires `maxUnavailable=0`; rollout cannot consume a
serving domain while a joining Node waits for admission.

Mutable scheduler labels, Kubernetes annotations, instance names, and name
regexes confer no membership authority. Public planning first authenticates a
canonical Ed25519 receipt from the source-owned membership issuer registry. The
signed exact subject binds project, cluster, NodeGroup, run, expected count,
minimum domains, maximum surge, selector digest, and sealed kubeconfig digest.
Its provider relation binds the exact NodeGroup resource version, sorted
Compute instance IDs, signed membership epoch, and provider-observer authority
to reopened authoritative export bytes. No username, UID, group, authentication
extra, opaque impersonation claim, SAR result, or admission-request identity is
an authorization input.

An epoch contains a monotonic sequence, exact predecessor payload digest,
phase, and disjoint serving/joining/retiring sets whose sorted union equals both
the admitted set and current provider membership. Stable contains exactly the
three serving members. Prepare retains all three old serving members and admits
at most the infrastructure surge bound; if a replacement Node races ahead, its
CREATE is denied and kubelet registration retries while the old three remain
because `maxUnavailable=0`. Cutover is accepted only after the new three-member
serving set is provider-owned, Ready, schedulable, hard-taint-free, and spans
three hostnames; the old bounded retiring member remains admitted for
quiescence, deletion, or rollback. A rollback is another exact next epoch that
restores the old serving set while retaining the other member as retiring.
Genesis must be sequence one/stable/zero-predecessor. Every later policy update
must be the exact next sequence and name the payload digest currently installed
on the policy. A separate fail-closed admission policy and binding express the
update semantics but are not trusted to protect their own
admissionregistration objects. On every API-server UPDATE the semantic guard
compares the new sequence, predecessor payload, predecessor phase, and exact
predecessor serving/joining/retiring annotation strings with `oldObject`.
Consequently, if two saved plans both derive different N+1 successors from
epoch N, the first may commit and the second is denied against the now-current
N+1 object even though its plan-time precondition once passed. The guardian
also denies update/deletion of either binding and deletion of either policy as
defense in depth; Terraform `prevent_destroy` mirrors that check. The
preventive authority is the external provider-IAM plus API-server boundary
described below. The policy also records its
exact serving, joining, and retiring sets. Terraform admits only these
predecessor-relative transitions: stable may
retain its exact serving set or enter prepare without changing it; prepare may
roll back to that serving set or promote exactly its joining set while retaining
only displaced old servers; cutover may finalize without changing its serving
set or reverse within the same admitted union. Thus a fresh signed receipt
cannot use a valid predecessor digest to jump directly to an unrelated serving
set. Rollback never decrements a sequence: it is another signed exact-next
epoch whose predecessor fields compare with the current `oldObject`. The empty
issuer and adapter registries remain external enrollment gates.

The CAS objects cannot bootstrap or preserve their own names. Before Terraform
may create them, Platform Security must install an external preventive boundary
that combines provider IAM with API-server admission enforcement. Source
enrollment pins its provider policy ID, API-server enforcement ID, controller
username/UID/groups, image digest, configuration digest, exact source
repository/commit/tree, provenance-attestation digest, and an exhaustive sorted
identity-path set covering direct users, service-account tokens, client
certificates, OIDC, provider control-plane identities, and every Kubernetes
impersonation dimension (user, group, UID, and userextra). The signed approval
must repeat that exact boundary receipt. Opaque review hashes alone are not
authority, and the in-cluster VAP remains defense in depth. Source enrollment
also pins one creator's username, UID, complete group and `userInfo.extra`
sets, plus an independent impersonation-review digest and a nonzero independent
RBAC review. Its fail-closed parameter is a
security-owned signed epoch approval containing the exact dynamic Node-policy
spec, complete annotations, content digest, membership receipt, actor
username/UID/groups/extras, and both review digests. The persistent external
policy compares the submitted object and authenticated actor to those exact
values, permits only the exact Deny binding CREATE, and rejects update/deletion.
Terraform has read-only custody and cannot create or repair that approval. A
copied digest or plausible sequence cannot authorize an allow-all policy. The
production registry is empty here, so this candidate cannot self-enroll the
external boundary. Both mutation fences reread and hash the
external policy/binding together with both CAS objects, so drift fails closed.

The boundary signature does not authenticate a summary alone. The fixed run
root must also contain three private, stable regular files named
`public-edge-preventive-provider-iam-export.json`,
`public-edge-preventive-apiserver-enforcement-export.json`, and
`public-edge-preventive-identity-path-review.json`. The signed evidence hashes
those exact bytes. The apply-time verifier reopens and parses them, then
reconstructs the enforcement join: provider IAM must be default-deny with one
exact controller binding over all six protected policy/binding names and four
mutation actions; API-server enforcement must be fail-closed and match the
same names/actions/controller; RBAC must contain the one exact controller rule,
no impersonation grant, and an exhaustive denial result for every enrolled
identity path. It recomputes the RBAC and impersonation review digests from the
raw structures instead of accepting opaque digest-shaped assertions. Each
export also binds the authoritative endpoint, collector executable and
configuration, request IDs, response attestation, resource version, and
collection time. The external Ed25519 receipt binds all three raw-file digests,
the live approval projection, project/cluster, source/controller provenance,
and exact boundary identifiers. The empty source trust registry prevents an
ordinary run-root file from self-enrolling this authority.

Terraform never launches a verifier pathname. The fixed launcher accepts only
logical source/mode pairs and chooses the canonical root-owned manifest,
bootstrap, Python, source, tool, and provider bundle. Integration builds it as
a static PIE and installs it root-owned mode 2755 to the dedicated no-member
`fs2-public-edge-capsule` group. The manifest/bootstrap are unreadable to the
ordinary caller. The launcher and bootstrap both prove the real/effective GID
transition and absence of capsule-group membership; direct environment markers
cannot reproduce it. The signed root-owned v2 manifest binds exact accepted
commit/tree, installer/launcher/bootstrap digests, every regular release file, executable
digests, a no-network Terraform provider mirror, and sealed CLI configuration.
Every operator invocation re-enumerates and hashes that complete tree before
source execution.

Both launcher paths require one independently attested static-PIE frozen Python
runtime. It is `ET_DYN` for ASLR but has no interpreter or external dependency;
the only dynamic metadata is a bounded self-relocation closure. Runtime and
offline verifiers enforce the same contract: exact file-to-memory congruence
and pairwise disjointness for the canonical inventory, payload, actual CPython
`_frozen` table, and `PyImport_FrozenModules` pointer storage; only bounded
relative/IRELATIVE relocations; GNU RELRO for relocation-time writable pointer
data; non-ALLOC provenance/attestation sections; and an external Ed25519 review
over the normalized artifact and complete frozen-module/build closure. The
review key is neither embedded in the candidate runtime nor supplied by its
build caller.

`./inference-stack <command>` immediately re-execs the atomically selected
activation-unit launcher before
argument parsing, Terraform probing, or run-root creation. The accepted source
then replaces apply-time Terraform, kubectl, Nebius, and crane arguments with
manifest-pinned `/proc/self/fd` paths. All child calls preserve only the capsule
descriptors and a fixed environment. Terraform external programs and every
local-exec helper for edge evidence, JobSet, Kueue, and mutation fencing are
finite logical capsule entries too; none starts through a caller shell,
`/usr/bin/env`, or a caller path. The reviewed invocation shape is:

```text
/usr/local/libexec/fs2-public-edge-current/launcher inference-stack operator -- apply --var-file ... --run-root ... --nebius-profile ...
```

For cloud/provider commands the caller supplies only the non-secret profile
selector. A fixed root-owned broker returns one short-lived Nebius token as a
sealed descriptor and a signed v3 exact-subject envelope. It binds caller UID,
real/effective GID, peer-observed UID/effective GID, operator, exact
profile/project/tenant/service-account subject, broker executable/config
digests, peer mode, and external runtime review. Terraform providers read that descriptor and never
load an ambient profile or caller `HOME`. The production broker-authority
registry is empty in source. Grafana/NGC/NVCR values remain in the launcher's
separate parent and are released after accepted config parsing only for four
exact logical slots. Generic uppercase environment inheritance is forbidden,
including unrelated AWS, GitHub, OpenAI, loader, Python, Terraform, proxy, and
Nebius secrets.
Each actual cloud-operation boundary obtains a fresh lease with at least two
hours remaining. Mutating Terraform is capped at 90 minutes. Before Terraform
starts, a fixed root-owned service records a signed, hash-chained/WORM mutation
intent under the external settlement root and refuses a conflicting active
predecessor. Only a signed external terminal record closes it. Caller-owned
run-root JSON is a cache and cannot clear the fence, so SIGKILL, power loss, a
nonterminal result, or a failed apply blocks later mutation.
A root-owned signed settlement first proves stage-appropriate provider
operations terminal (including authenticated zero-operation proof when true).
Reconciliation applies refreshed state, requires a full exit-zero no-drift plan
with ephemeral accepted secret slots, and records state/output digests. Resume
requires a second external signature over that exact evidence.

`status`, `output`, `proxy`, `debug-proxy`, `debug-view`, `debug-export`,
`activate-debug`, and `disable-debug` do not request
cloud authentication. They use a separate authenticated `local-read-only`
capsule mode with the same accepted source/tool descriptors but explicitly no
Nebius token, auth envelope, refresh descriptor, Terraform init, plan, or state
rewrite. Status/output read only retained run state. Proxy commands send only
the retained cluster identity and contract digest to an independently enrolled
root proxy broker. The operator never receives a kubeconfig and never starts a
raw `kubectl port-forward` listener.

The local-read-only command is classified by the accepted bootstrap before any
cloud-auth request. Its launcher handoff synthesizes neither an inherited
`--nebius-profile` nor an implicit tfvars argument, so an empty cloud-auth issuer registry or
unavailable cloud token broker cannot disable retained `status`/`output` or the
separately owned internal proxy broker. An explicitly supplied `--var-file`
selects only the retained run-root name and is not opened in this lane. The
proxy broker remains an explicit local availability dependency; it is not
replaced with ambient cloud credentials.

Request-debug redaction uses a normalized exact-name taxonomy rather than a
blanket `*token` suffix, so scientific telemetry fields including
`max_tokens`, `input_tokens`, and `output_tokens` remain useful. Exact secret
keys include API/GitHub/personal-access tokens, private/deploy keys, AWS secret
access keys/session credentials, passwords, cookies, signatures, and existing
authorization headers. Recognizable GitHub token, contextual AWS secret/access
key, authorization-scheme, JWT, and PEM private-key formats are removed even
from malformed or partial bodies. The current policy is re-applied before every
store write, decrypted read, and API export; a retained pre-hardening row or a
custom store therefore cannot bypass a later taxonomy improvement. The
separate accepted SAI-02 integration continues to own the exact 90-day record
retention and purge.

For cloud-authorized commands, the brokered token descriptor is not part of
the global child FD set. Only a child whose environment carries the exact
current signed delegated-auth envelope receives that one descriptor; local
readers and signature/provenance helpers receive immutable capsule descriptors
only.

The retained `public-edge/v2` `kubectl-port-forward` value and all existing
fields remain unchanged for state compatibility. New plans add an optional
`internal-proxy-isolation/v1` sub-contract. Local source derives the same
strict defaults while reading an older retained v2 output, so adoption does
not require infrastructure replacement or reapply.

The broker owns the credential, creates a private network namespace, and starts
both raw Kubernetes transports on that namespace's loopback interface. The
ordinary host-network listener is the broker's policy-enforcing proxy on the
contracted operator port and retains application authentication. Debug has no
host TCP listener: the broker creates one caller-owned mode-0600 Unix socket at
an exact session path. The signed lease reports an empty
`raw_host_listeners` list, exact private-namespace service/port tuples, the
namespace inode, and root-only kubeconfig custody. Ordinary `proxy` retains the
authenticated inference/MCP/admin surface. The separate `debug-proxy` requires
one externally signed exact tenant, public-model, and App UUID activation,
allows only GET/HEAD request-debug routes, and expires in at most seven days.
The debug bearer is never returned to the caller: the root broker injects an
Ed25519 assertion inside its private namespace and the backend independently
binds it to the broker, cluster, session, activation, App UUID, model, tenant,
method, and expiry on every read. Missing or untrusted scope is denied.
Because browser JavaScript cannot safely consume a Unix-domain socket, the
accepted operator surface also provides `debug-view` and
`debug-export --debug-request-id <uuid>`. These commands request only the exact list or
detail path over the root-authenticated broker control channel. The broker
returns a bounded, signed JSON result bound to the session, nonce, path,
operation, expiry, body digest, and durable audit-event digest. The commands
never receive a kubeconfig, private key, backend bearer, raw transport, or
generic forwarding capability; the ordinary browser/admin lane remains
unchanged and cannot inject debug authorization.
Debug remains default-off; request-debug records retain the independently
accepted SAI-02 exact 90-day purge contract rather than inheriting the
activation lifetime. The production broker and activation issuer registries
are empty, so integration fails closed until the external owner enrolls exact
artifacts and proves the namespace/listener boundary. Enrollment pins separate
nonzero digests for the broker executable, configuration, reviewed runtime,
network-namespace policy, and scope-enforcement policy; the signed session
response repeats all five rather than treating the broker's claim as an
unversioned assertion.

Activation install and disable take an exclusive kernel lock on the retained
mode-0700 run-root directory. While that lock is held, the command reopens and
verifies the complete signed activation and tombstone history, rejects a
second current grant or a reused tombstoned grant, and only then performs the
O_EXCL append. The directory lock itself creates, replaces, or deletes no
evidence and covers the full check-and-append transaction, so concurrent
operators cannot commit two distinct current grants.
Local history treats a revocation as terminal only when it also reopens a
matching broker-signed receipt containing the durable backend event digest and
proof that listeners, connections, children, raw transports, and active
sessions are gone. Disable durably appends that receipt before the owner
revocation marker; a crash in between leaves the old activation selectable for
an idempotent retry instead of committing an unproven tombstone.

The additive request-debug state-machine migration is intentionally reserved
as `0034_request_debug_activations.sql`. It is not yet added to the canonical
migration release list: SAI-21 owns accepted migrations 0030 and 0031, and
SAI-19 owns 0032 and 0033. Integration must first merge those exact sibling
bytes, then reseal the ordered migration hashes, count, last-version contract,
chart values/schema, and packaging expectations through 0034. This branch
does not copy, invent, or supersede the sibling migrations, and the standalone
SAI-15 source must therefore remain integration-blocked until that reseal.

The signed expiry is converted once to a local monotonic deadline. Every
15-second heartbeat is independently signed and binds the session, sequence,
expiry, namespace inode, listener state, and bounded raw transports. Wall and
monotonic clocks may not diverge by more than five seconds. Expiry, operator
shutdown, or a clock anomaly triggers a close request on the authenticated
control socket and requires a signed terminal receipt proving listener and
connection closure, raw-transport closure, child reaping, and namespace
destruction. A missing or invalid heartbeat or terminal proof fails closed.

The membership receipt additionally binds the absolute path, resolved path,
and SHA-256 of the capsule Python interpreter, provider observer, and kubectl.
Each resolved binary and parent chain must be protected. The verifier stable-
reads and hashes each opened executable, retains the descriptor, and invokes
the observer and kubectl only through inherited `/proc/self/fd/<n>` names. It
refuses any signed identity mismatch and never closes then reopens a command by
its mutable pathname. Provider subprocesses receive only pinned descriptors
plus `HOME=/nonexistent`, the manifest-owned tool directory, `C.UTF-8`, and
`/` as the fixed working directory. Membership and client-identity signature
checks invoke manifest-pinned OpenSSL; signed kubectl must resolve to the same
inode as capsule kubectl. The exact run-owned mode-0600 kubeconfig is stable-read
once, its SHA-256 is bound into the signed Terraform subject, and its bytes are
copied into a read-only sealed memfd. Kubectl receives only that sealed
snapshot; later in-place writes or path replacement cannot change its content
or authentication configuration. The verifier then pages the complete project
Compute inventory only to prove every signed member remains present;
names are retained solely as change detectors. Both inventory observations and
every signed exact-ID get must agree, and each member must have the exact project
parent, stable positive resource version, and current `RUNNING`, non-reconciling,
non-stopped state. Only then is a Kubernetes Node admitted when its name is a
signed instance ID and `spec.providerID` is exactly
`nebius://<instance-id>`.

Cluster API cluster, MachineSet owner, owner-name and Machine annotations are
retained only as corroboration: they can reject a provider member but can never
add a Node to the provider set. The owner is either the exact group ID or that
ID plus the provider's single five-character rollout generation; the Machine
is exactly one bounded child generation below that owner. Arbitrary prefixes
and additional suffixes fail. The two Node snapshots must retain identical
UID/resourceVersion-bound eligibility projections. Their instance IDs must be
a subset of exact provider membership and include the complete signed serving
set; a prepare-phase joining instance may not have registered yet. The terminal NodeList revision,
provider revisions, explicit pagination summaries, and non-secret projection
digests are included in the mutation-fence receipt. A cordon, hard taint,
replacement, label/annotation spoof, provider member change, provider rollout,
group move, incomplete enumeration, or stale saved plan therefore fails the
protected mutation path; the scheduler then enforces the same placement facts
at admission to a node. The final provider list/exact gets follow the second
NodeList. After that read, a fail-closed `ValidatingAdmissionPolicy` and binding
continuously reserve the five selector labels for the signed epoch and require
exact `providerID`. Protected fields are immutable for every identity. Only a
signed joining member may monotonically initialize a previously absent field
to its exact value; it cannot change or remove an initialized field. Ordinary
status updates remain valid. The policy contains no `request.userInfo`
authorization branch, so impersonating any controller tuple cannot extend
membership.
Each mutation fence also reads the exact membership and CAS
ValidatingAdmissionPolicies and both bindings
before the provider/Node sandwich and again after the terminal provider reads.
It requires stable UID and resourceVersion plus an exact canonical hash match
with the Terraform manifest, including membership payload/receipt, epoch,
sequence, predecessor, and provider-adapter digest annotations. Removing,
weakening, replacing, or racing any admission object therefore fails the
StatefulSet or Helm lifecycle precondition rather than leaving an unprotected
TOCTOU interval.
Redis/Sentinel, Envoy Gateway, RLS, and the Envoy proxy also receive required
`metadata.name` node affinity over that exact signed instance-ID set, so the
scheduler consumes provider membership directly as well as the protected
selector.
If the complete HA authority or RLS is unavailable, Envoy is fail-closed rather
than silently removing the security control.

Redis stores no customer data, credentials, request bodies, or durable
accounting. Its image is digest-pinned in source, runs without a service-account
token or Linux capabilities, and has read-only root storage. The image still
requires the normal independent vulnerability/SBOM/promotion gate before any
deployment. The six always-present Terraform addresses (ConfigMap, StatefulSet,
headless Service, Sentinel discovery Service, PDB, and NetworkPolicy), plus the
public-only apply gate, Node-authority policy/binding, and immutable CAS
policy/binding addresses,
are included by exact name in
`managed_resource_count` and exposed as a closed evidence output.

The Envoy data plane has two replicas, rolling availability, CPU/memory
requests and limits, a one-Pod minimum PDB, required hostname anti-affinity,
and hostname topology spread with `minDomains: 3`. The Envoy Gateway controller
and RLS use the same public-only hard placement contract; internal-only mode
retains soft placement and does not claim node-loss HA. Its `ScheduleAnyway`
constraints omit `minDomains`, which Kubernetes permits only with
`DoNotSchedule`; public mode retains `minDomains: 3` and `DoNotSchedule`. Both
listeners cap concurrent connections, connection lifetime, requests per
connection, incomplete-body time, idle time, stream lifetime, and concurrent
HTTP/2 streams. The active-stream ceiling is exactly 7,500 seconds, preserving
the supported audio allowance; the 7,800-second connection lifetime gives that
stream five minutes of connection/setup headroom. The application HTTPRoute
places an exact `/v1/audio/stream` rule before the `/v1` prefix rule and gives
both request and backend request a 7,500-second timeout. Other API paths retain
their 40-second bound.

The first source candidate, `f60ba3f8bfe8818a343bb16c2eda9ab9bdff6289`
(tree `7b7903d928bf9d49ae12bf197c3ca1f0b5a6f25a`), is preserved as rejected
evidence. It omitted the Terraform count, used a fail-open standalone store,
shortened audio streams, omitted the HTTP request limit, and hard-coded an
unproven trusted hop. Its successor,
`678c3606d33c05388559063f51df1b3620933451` (tree
`f1b8fd9820409953c59156146281b09450075043`), corrected the store, count,
listener, and stream issues but still trusted caller-asserted XFF booleans,
hop count, and digest. The authenticated-receipt successor,
`243cf47a73776e1c0f38091b34a4503fd36206c2` (tree
`ff97ee4f2eb3705a4c5b8a8d0d1e6c63d291c147`), removed that assertion path but
left the RLS NetworkPolicy unable to reach its configured Sentinel discovery
port, retained a stale one-listener regression expectation, and left the
operator runbook describing fail-open/two-hour behavior. All three commits
remain rejected evidence; this document describes their direct additive
successor, which admits only ports 6379 and 26379 from RLS to the selected store
Pods and aligns the source contracts without enrolling a production issuer.
That successor, `644b365e74797937f5e0236e0bdfb1d18b9fdaed` (tree
`e253e2cd57bd6825cfeef2ed93364db422ff5551`), is also preserved as rejected
evidence: its general `/v1` route still imposed 40-second request/backend
timeouts on `/v1/audio/stream`, and its spread preferences did not guarantee
three eligible nodes or prevent co-location. This document describes the
direct additive successor that closes those two source defects. Intermediate
commit `f35b2a18f8390119b98fda29caa25a676e25ad85` (tree
`104733c341e06e9331cd91da214c692e64b12ad3`) added the exact audio route and
three-domain placement, but foundation still trusted a wrapper-propagated
receipt and public callers could override the system-pool update strategy to
remove too much capacity. It is preserved as intermediate negative evidence.
The current additive successor binds foundation to infrastructure state and
its digest, records the Ready-node/hostname preflight, and closes the update
strategy override. Exact commit `2cb99698fd106be5225e722de14f318238d418cb`
(tree `fb2f388f3e00c0779ab43eb84864b512fc1deff9`) is preserved as rejected
evidence: workloads trusted only its saved foundation receipt, its selector did
not bind provider node-group/run ownership, and it counted hard-tainted Nodes
as eligible. Its internal-only soft-spread objects also paired `minDomains`
with `ScheduleAnyway`, which Kubernetes rejects. The current additive successor
adds an independent workloads-stage Node reread and exact UID/resourceVersion,
ownership, and taint eligibility while omitting `minDomains` only for the soft
internal mode. Exact commit `17407f590673a30be48bc739e09aab279c1f1b52`
(tree `2943858876b4bf9459c8f272c1e68cb1957acf1f`) is preserved as rejected
evidence: both Node reads still occurred only while saved plans were created,
and mutable labels were its only node-group ownership signal. Its interrupted
successor added an apply-time provider/Kubernetes revision sandwich but inferred
provider membership from a Compute instance name convention, executed
caller-`PATH` tools, left foundation reads separable from store prerequisites,
and spent a five-minute plan TTL on waits that can legitimately take much
longer. Exact commit `28247f8640ddd9ce41daf70dd3eb842346572679`
(tree `6fbab7694fbc03ce1367e6130a6fb4bb2803fc05`) is also preserved as rejected
evidence. It materially fixed authoritative NodeGroup membership, pagination,
the provider/Kubernetes sandwich, admission continuity, HA, and audio behavior,
but still started mutable verifier bytes before checking their planned digest,
reused a mutable kubeconfig file descriptor, inherited ambient startup state,
did not terminally hash the policy and binding, did not source-enroll the
provider adapter, and authorized the controller by username without complete
authentication/impersonation closure.

Exact `48a01d5f76532b230f4dde0a7d9fb3c4c118c8f7` (tree
`91fbf0d0575727b91b909614940ec8b42e0958f4`) is preserved as rejected evidence.
It still let a caller select matching source/digest pairs, ran caller-selected
Terraform and provider tooling, depended on an undocumented external launcher
installation for ordinary apply, asserted controller impersonation closure,
and lacked a nondisruptive exact-member replacement epoch.

Exact `6b406acc083e5a7a0ff7a88b8636089770044fed` (tree
`04d861eccdeef7febf5a88b104d74b1316f60455`) is also preserved as rejected
evidence. Its accepted capsule stripped the credential material required by the
Nebius CLI and Terraform provider while still importing unrelated ambient
uppercase secrets; its documented install preview closed verified inputs before
a separate, unimplemented copy; and its epoch transition remained only a
saved-plan check. Two different saved N-to-N+1 plans could therefore both pass
planning and the later apply could replace the first successor.

This direct additive successor replaces those assertions with the accepted
release capsule, documented build/install/re-exec contract, identity-free Node
admission, a descriptor-pinned privileged installer, brokered signed short-lived
authentication, and chained stable/prepare/cutover epochs with
`maxUnavailable=0`. The immutable guardian admission policy performs the epoch
compare-and-swap against the API server's current `oldObject`, so a stale second
successor is rejected at mutation time rather than trusted because its plan was
once current.
It retains sealed kubeconfig bytes, authoritative provider membership,
four-hour plan identity plus fresh fence observation, terminal exact admission
hashes, public three-domain HA, RLS fail-closed behavior, and exact audio
timeouts. Capsule, provider-adapter, membership-issuer, and client-identity
production registries remain intentionally empty. This is a SOURCE candidate
only until independent exact review; it makes no integration or live claim.

The provider authority adapter is based on the current primary contracts:

- Nebius documents retrieving the exact NodeGroup ID and the provider-created
  Kubernetes Node/Compute instance identity:
  <https://docs.nebius.com/kubernetes/node-groups/moving-workload>.
- The Nebius CLI requires an exact parent cluster for NodeGroup enumeration and
  exposes an exact-ID get with resource-version support:
  <https://docs.nebius.com/cli/reference/mk8s/node-group/list> and
  <https://docs.nebius.com/cli/reference/mk8s/node-group/get>.
- The Nebius NodeGroup API defines fixed count, `RUNNING`, target/current/Ready
  counts, outdated count, reconciliation state, and explicitly defines each
  Node as a Nebius Compute instance:
  <https://github.com/nebius/api/blob/main/nebius/mk8s/v1/node_group.proto>.
- The Nebius Compute API defines exact project-scoped instance pagination and
  its terminal `next_page_token` contract:
  <https://github.com/nebius/api/blob/main/nebius/compute/v1/instance_service.proto>.

The adapter deliberately accepts no alternate ownership representation. If a
provider revision changes these documented facts, public apply fails until a
new exact adapter and provenance are independently reviewed.

### Receipt and issuer custody

The production trust registry is
`stages/workloads/contracts/trusted-edge-evidence-issuers.json`. Each future
entry must contain exactly an authority ID, the fixed
`platform-security-edge-evidence` role, a `sha256:<hex>` key ID derived from the
raw 32-byte Ed25519 public key, and that key in canonical unpadded base64url.
The adapter rejects duplicate authorities, key-ID/key mismatches, alternate
roles, caller-supplied registry paths, and all receipts while this registry is
empty. Public keys are non-secret, but onboarding one grants evidence-signing
authority and therefore requires its own Platform Security provenance and
source review.

NodeGroup membership has a separate least-authority registry at
`stages/foundation/trusted-public-edge-membership-issuers.json`, also empty in
this candidate. Its fixed role is
`platform-security-public-edge-membership`. A receipt must reopen the exact
mode-0600 `public-edge-provider-membership.json` bytes and bind the provider
relation API, exact project/cluster/NodeGroup revision, provider instance IDs,
chained membership epoch, source-enrolled provider observer, sealed kubeconfig
digest, and attested toolchain. The fixed
mode-0600 receipt name is
`public-edge-node-group-membership-receipt.json`. Neither path, trust key,
member list, epoch, executable digest, nor verification result is
caller-configurable.

Provider observation has a separate source registry at
`stages/foundation/trusted-public-edge-provider-adapters.json`, also empty in
this candidate. Every future entry must bind exactly one observer ID, Nebius
endpoint, credential authority and subject, audience, configuration digest,
adapter digest, and executable digest. The gate compares the planned hash of these exact
registry bytes before accepting a receipt. Populating it is an external
enrollment action requiring owner-approved provenance and independent review;
ordinary tfvars, environment, signed evidence, and saved plans cannot add an
entry.

Accepted execution packages have a separate source registry at
`stages/foundation/trusted-public-edge-capsule-issuers.json`, also empty. The
offline package verifier requires both a valid Ed25519 installation receipt and
fixed protected root-owned issuer and acceptance files under `/etc/fs2`. The
acceptance file binds the registry digest, accepted commit/tree, manifest
digest, installer/launcher/bootstrap digests, and fixed package-verification OpenSSL
digest; none is a verifier CLI input. The verifier checks the digest against
the same stable manifest bytes it parsed and executes the pinned OpenSSL file
descriptor. Runtime
apply inputs cannot select an authority. Build, packaging, privileged
installation, atomic activation, and rollback are specified in
`public-edge-execution-capsule.md`.

Short-lived cloud authentication has a separate source registry at
`stages/foundation/trusted-public-edge-auth-broker-authorities.json`, also
empty. A future entry binds one root-owned broker socket, exact
profile/project/tenant/subject/operator matrix, fixed executable/config
digests, peer UID/GID/mode and runtime-review digest, Nebius endpoint, audience,
authority/key, and token-signing role. The signed v3 response binds those facts
plus caller real/effective GID, the broker's peer-observed effective GID,
commit/tree/manifest, nonce, token digest and bounded timestamps. Neither an ambient profile name nor an
untrusted environment token can populate this registry or satisfy the
signature.

The receipt is canonical JSON followed by one newline and contains exactly the
receipt schema, `ed25519` algorithm, payload, recomputed payload SHA-256, and
signature. The signature covers the schema, algorithm, payload and digest. The
payload contains the issuer, nonce, whole-second UTC issue/expiry timestamps,
exact Terraform subject, provider topology, derived-fact inputs, and eight raw
evidence digests. The fixed mode-0700
`<run_root>/edge-client-identity-evidence/` directory must contain mode-0600
`provider-load-balancer.json`, `provider-listeners.json`,
`provider-backend.json`, `security-group.json`, `routing.json`,
`xff-probe.json`, `direct-access-probe.json`, and
`connection-isolation-probe.json`. The adapter opens those exact
names relative to a no-follow directory descriptor, reads each stable regular
inode once, and refuses any byte digest that differs from the signed receipt.
It parses the content-addressed native listener list, requires a terminal
complete provider page, derives one identical source-IP concurrent-connection
cap for the exact HTTP and HTTPS listeners, and reconciles that revision with
the bounded two-source HTTP/1, HTTP/2, and incomplete-handshake probe. The
signed normalized observation is accepted only when it equals those locally
derived native facts.

The receipt itself is also opened through `O_NOFOLLOW` and must be a stable
mode-0600 regular inode no larger than 128 KiB. Signature verification uses
root-owned OpenSSL with anonymous in-memory file descriptors and creates no
verification files. Only non-secret digests and resource identities are
returned to Terraform.

The Terraform output `public_edge_client_identity_evidence` records the
accepted receipt digest, payload digest, signer key ID, exact provider LB ID,
derived hop count, derived per-source concurrent-connection cap, and derived
direct-access verdict. It is null in
internal-only mode. Raw provider exports, probes, signatures, or credentials
must not be copied into Terraform state or Helm values.

## Required integration and live evidence

The source regression tests were authored but deliberately not executed under
the coordinator's static-only boundary. A later reviewed integration must:

1. Validate and render the chart and foundation configuration from the exact
   accepted successor commit, including CRD compatibility with Envoy Gateway
   v1.8.3, the exact 7,500-second audio rule, the exact three-domain placement
   receipt and digest, both apply-time provider/Kubernetes rereads, the
   prerequisite-compatible four-hour plan bound and fence-time timestamp, both
   deferred mutation fences, complete explicit provider pagination, signed
   provider membership export, the signed accepted-release manifest, complete
   root-owned release inventory, setgid/no-member child proof, static launcher
   and independently attested static-PIE frozen runtime, exact actual CPython
   table/inventory/payload linkage, congruent read-only/RELRO mappings, bounded
   self-relocation closure, protected `inference-stack apply`
   re-exec before parsing/Terraform, manifest-pinned executables/providers
   executed through retained descriptors, a digest-bound sealed-memfd kubeconfig, a nonexistent-HOME
   allowlisted provider-command environment, stable
   cluster/NodeGroup/instance and NodeList revisions, `spec.providerID` equality
   with the exact signed Compute member set, terminal before/after exact hashes
   of both ValidatingAdmissionPolicies and both bindings, apply-time epoch CAS
   rejection of two concurrent saved N-to-N+1 plans, fail-closed Node admission, rejection of
   spoof-labeled/cordoned/hard-tainted Nodes, valid
   internal soft spread, the retained-capacity update strategy, and equality
   between the plan count and address allowlist.
2. Scan and promote every introduced image digest before creating resources.
3. Record the current shared-service release/image identity and integrate all
   deployed sibling remediations before rollout.
4. Stage the foundation store and prove one primary, two replicas, three
   agreeing Sentinels, quorum failover, and RLS recovery before enabling policy;
   retain the previous Helm revision and state-backed plan for rollback.
5. Onboard the exact Platform Security capsule, short-lived auth broker,
   client-identity, and
   provider-membership signing public keys plus the exact provider-observer
   entry by reviewed source commit. Bind the capsule package to the accepted
   commit/tree, exhaustive release inventory, installer/launcher/bootstrap/tool/provider
   digests, and bind the observer endpoint, credential
   authority/subject/audience, immutable configuration, adapter and executable
   digests. Prove the admission policies contain no identity-based bypass,
   reject a stale concurrent policy overwrite against API-server `oldObject`,
   and protect both bindings against update/deletion. Install and independently
   accept the external security-owned CAS bootstrap first; prove its exact
   creator UID/groups/extras and impersonation review. Reopen the exact signed
   provider-IAM, API-server enforcement, and RBAC/impersonation raw exports;
   prove their collector/API provenance and resource versions are current and
   that source recomputation denies every non-controller identity path. Then prove an ordinary
   VAP creator cannot preoccupy either CAS name. Prove fresh auth renewal, the
   90-minute mutation bound, and indeterminate reconciliation before retry.
   Verify both signature paths use static OpenSSL with no interpreter/dynamic
   runtime closure, and inject a failure at each installer phase to prove a new
   append-only attempt completes without removing the preserved partial one.
   Produce a fresh
   authoritative NodeGroup membership export and signed membership receipt,
   then produce a fresh signed client-identity receipt from independent provider/LB,
   listener, backend, SG, route-table, XFF-mutation, and direct-access captures;
   store it mode 0600 at the fixed run-root path, and store the seven reopened
   raw inputs under the fixed mode-0700 evidence directory. Prove the provider
   relation, executable identities, chained epoch/predecessor, and derived
   client address cannot be forged.
6. Saturate client A's general and admin buckets while client B continues to
   receive non-429 responses, then repeat against HTTP redirect/ACME, website,
   API, admin, and Grafana routes.
7. During one Redis/Sentinel member restart and one RLS rolling update, repeat
   the two-client isolation test and prove counters never split or fail open.
   During an isolated total-backend fault, prove bounded fail-closed responses.
8. Show at least two Ready Envoy proxy replicas on distinct nodes, an effective
   PDB, bounded resources, two Ready controller and rate-limit-service replicas
   on distinct nodes, three Ready store members on three distinct system nodes,
   the exact infrastructure node-group/count/selector/update receipt and digest,
   both creation gates and both mutation fences' provider/NodeList revisions
   and receipt digests, every
   foundation UID remaining in the current workloads-stage eligible UID set
   with a current resource version and matching Nebius provider identity, zero untolerated
   `NoSchedule`/`NoExecute` taints, `maxUnavailable = 0`, positive surge, all
   three serving system nodes retained, a prepare epoch with bounded joining
   member, cutover only after three-domain readiness, retiring-member
   quiescence, and a rollback epoch, plus accepted traffic policies.
9. Prove the existing Deployment/Service to StatefulSet/headless/Sentinel
   transition is a non-destructive staged migration: no old resource is deleted
   or replaced before the new single-primary/quorum contract is Ready, and no
   policy is enabled before authenticated identity evidence passes.
10. Exercise landing/catalog, PAT/model authorization, sync/stream inference,
   MCP, admin, operations/results/artifacts/uploads, storage, queue/model
   admission, observability, and rollback. Include an active audio stream and
   confirm idle, 7,500-second stream, and 7,800-second connection ceilings.
11. In internal-only mode, enumerate the host network namespace while both
    ordinary and debug sessions run. Prove the contracted control/admin ports
    are absent, exactly one broker-owned ordinary listener exists on the operator
    port, the interactive debug lane exists only as its mode-0600 caller-owned
    Unix socket, and one-shot view/export traffic remains on the authenticated
    broker control channel with no host listener,
    both raw transports exist only in the signed private namespace, and
    direct host connections to the raw ports fail. Prove ordinary authenticated
    inference/MCP/admin behavior is unchanged. Separately prove an active
    at-most-seven-day debug grant can read only its signed tenant/App/public
    model request rows, cannot use mutation methods or other routes, and cannot
    obtain or reuse the broker's Kubernetes credential.

Rollback is ordered and reversible: first restore the prior application Helm
revision so no active policy depends on the global rate-limit service; then
restore the prior foundation Helm revision and Terraform plan. Keep the HA
store until the policy and generated rate-limit service are confirmed absent.
A live operator must use the recorded prior revisions and state-backed plan,
not source assumptions, and must re-run the same customer and operator smokes
after rollback.

No live resource, credential, registry, provider, database, or customer payload
was inspected or changed while authoring this candidate.

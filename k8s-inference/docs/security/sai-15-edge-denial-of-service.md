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
objects must remain unchanged across the sandwich. The current NodeGroup must
be `RUNNING`, non-reconciling, fixed at the contracted count, have zero
outdated nodes, meet its target/current/Ready counts, and carry the exact run
scheduler labels in its Node template.

Mutable scheduler labels, Kubernetes annotations, instance names, and name
regexes confer no membership authority. Public planning first authenticates a
canonical Ed25519 receipt from the source-owned membership issuer registry. The
signed exact subject binds project, cluster, NodeGroup, run, expected count,
minimum domains, and selector digest. Its provider relation binds the exact
NodeGroup resource version, sorted Compute instance IDs, full managed Node
controller authentication tuple, and provider-observer authority to the
reopened bytes of an authoritative provider membership export. Controller
authorization includes exact username, UID, sorted groups, authentication
extras, authentication authority, a positive impersonation-prohibited fact,
and the digest of the independent impersonation review. The same exact
controller tuple must be enrolled beside the adapter in source; the membership
signer alone cannot introduce a new controller. Both the production adapter
registry and membership-issuer registry are intentionally empty until an
independently approved observer/controller contract and issuer are enrolled,
so caller inputs cannot manufacture this relation.

Terraform never launches a verifier source pathname directly. The only
supported apply entrypoint is the fixed
`/usr/local/libexec/fs2-public-edge-gate-launcher`, which integration must build
as a static PIE from the checked-in C source, independently attest, install
root-owned mode 0555, and place under a completely root-owned, non-writable
directory chain. The launcher checks its fixed `/proc/self/exe` identity,
rejects an ELF interpreter, clears the complete ambient environment before
Python starts, sets only fixed locale/PATH/nonexistent-HOME values, and starts
`/usr/bin/python3 -I -B`. It stable-reads the requested verifier, compares the
in-memory bytes with Terraform's planned `filesha256`, and compiles only that
verified snapshot. Consequently a source-path swap after hashing is not
executed. Public `inference-stack apply` must itself enter through the same
launcher in `--operator` mode before even the Terraform version probe; only the
deployment contract's explicitly named secret references may be preserved.
No ambient loader, Python, Terraform plugin/workspace, proxy, profile, or HOME
state is inherited by Terraform or its provider children.
The reviewed integration invocation has the following shape, where the source
digest and every preserved name come from the accepted commit and deployment
contract rather than ambient discovery:

```text
/usr/local/libexec/fs2-public-edge-gate-launcher /absolute/reviewed/inference-stack <sha256> --operator [--preserve-env=CONTRACT_SECRET_NAME ...] -- apply --var-file ... --run-root ... --nebius-profile ...
```

The membership receipt additionally binds the absolute path, resolved path,
and SHA-256 of the running Python interpreter, provider observer, and kubectl.
Each resolved binary and parent chain must be protected. The verifier stable-
reads and hashes each opened executable, retains the descriptor, and invokes
the observer and kubectl only through inherited `/proc/self/fd/<n>` names. It
refuses any signed identity mismatch and never closes then reopens a command by
its mutable pathname. Provider subprocesses receive only pinned descriptors
plus `HOME=/nonexistent`, `/usr/bin:/bin`, `C.UTF-8`, and `/` as the fixed
working directory. The exact run-owned mode-0600 kubeconfig is stable-read
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
UID/resourceVersion-bound eligibility projections and their instance-ID set
must exactly equal the provider membership set. The terminal NodeList revision,
provider revisions, explicit pagination summaries, and non-secret projection
digests are included in the mutation-fence receipt. A cordon, hard taint,
replacement, label/annotation spoof, provider member change, provider rollout,
group move, incomplete enumeration, or stale saved plan therefore fails the
protected mutation path; the scheduler then enforces the same placement facts
at admission to a node. The final provider list/exact gets follow the second
NodeList. After that read, a fail-closed `ValidatingAdmissionPolicy` and binding
continuously reserve the five selector labels for the signed member IDs, require
their exact `providerID`, and allow protected-field changes or new member Nodes
only from the signed managed-node controller. Unrelated kubelet/status updates
remain valid when protected values do not change. Membership transitions need a
new signed receipt and policy update before a provider rollout; a mutable label
cannot extend the accepted set during the read-to-mutation interval.
Each mutation fence also reads the exact ValidatingAdmissionPolicy and binding
before the provider/Node sandwich and again after the terminal provider reads.
It requires stable UID and resourceVersion plus an exact canonical hash match
with the Terraform manifest, including the membership, controller,
impersonation-review, and provider-adapter digest annotations. Removing,
weakening, replacing, or racing either admission object therefore fails the
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
public-only apply gate, Node-authority policy, and policy binding addresses,
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

This direct additive successor uses the protected static launcher, a sealed
kubeconfig content snapshot, an empty source-owned observer/controller
registry, a full controller tuple, and terminal exact admission-object hashes.
It retains the signed provider membership relation, four-hour plan-identity
bound plus fence-time observation, prerequisite ordering, fail-closed
admission, public three-domain HA, and exact audio timeouts. The production
provider-adapter, membership-issuer, and client-identity issuer registries all
remain intentionally empty. This is a SOURCE candidate only until independent
exact-commit review; it makes no integration or live claim.

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
relation API, exact project/cluster/NodeGroup revision, member instance IDs,
approved full managed-node controller identity, source-enrolled provider
observer, sealed kubeconfig digest, and attested toolchain. The fixed
mode-0600 receipt name is
`public-edge-node-group-membership-receipt.json`. Neither path, trust key,
member list, controller identity, executable digest, nor verification result is
caller-configurable.

Provider observation and controller identity have a separate source registry at
`stages/foundation/trusted-public-edge-provider-adapters.json`, also empty in
this candidate. Every future entry must bind exactly one observer ID, Nebius
endpoint, credential authority and subject, audience, configuration digest,
adapter digest, executable digest, and full Node-controller tuple including the
impersonation-review digest. The gate compares the planned hash of these exact
registry bytes before accepting a receipt. Populating it is an external
enrollment action requiring owner-approved provenance and independent review;
ordinary tfvars, environment, signed evidence, and saved plans cannot add an
entry.

The receipt is canonical JSON followed by one newline and contains exactly the
receipt schema, `ed25519` algorithm, payload, recomputed payload SHA-256, and
signature. The signature covers the schema, algorithm, payload and digest. The
payload contains the issuer, nonce, whole-second UTC issue/expiry timestamps,
exact Terraform subject, provider topology, derived-fact inputs, and seven raw
evidence digests. The fixed mode-0700
`<run_root>/edge-client-identity-evidence/` directory must contain mode-0600
`provider-load-balancer.json`, `provider-listeners.json`,
`provider-backend.json`, `security-group.json`, `routing.json`,
`xff-probe.json`, and `direct-access-probe.json`. The adapter opens those exact
names relative to a no-follow directory descriptor, reads each stable regular
inode once, and refuses any byte digest that differs from the signed receipt.

The receipt itself is also opened through `O_NOFOLLOW` and must be a stable
mode-0600 regular inode no larger than 128 KiB. Signature verification uses
root-owned OpenSSL with anonymous in-memory file descriptors and creates no
verification files. Only non-secret digests and resource identities are
returned to Terraform.

The Terraform output `public_edge_client_identity_evidence` records the
accepted receipt digest, payload digest, signer key ID, exact provider LB ID,
derived hop count, and derived direct-access verdict. It is null in
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
   provider membership export, the root-owned/static/fixed-path launcher and
   its independently attested binary digest, protected `inference-stack apply`
   startup before Terraform, attested executables executed through retained
   descriptors, a digest-bound sealed-memfd kubeconfig, a nonexistent-HOME
   allowlisted provider-command environment, stable
   cluster/NodeGroup/instance and NodeList revisions, `spec.providerID` equality
   with the exact signed Compute member set, terminal before/after exact hashes
   of the ValidatingAdmissionPolicy and binding, fail-closed Node admission, rejection of
   spoof-labeled/cordoned/hard-tainted Nodes, valid
   internal soft spread, the retained-capacity update strategy, and equality
   between the plan count and address allowlist.
2. Scan and promote every introduced image digest before creating resources.
3. Record the current shared-service release/image identity and integrate all
   deployed sibling remediations before rollout.
4. Stage the foundation store and prove one primary, two replicas, three
   agreeing Sentinels, quorum failover, and RLS recovery before enabling policy;
   retain the previous Helm revision and state-backed plan for rollback.
5. Onboard the exact Platform Security client-identity and provider-membership
   evidence-signing public keys and the exact provider-observer/controller
   registry entry by reviewed source commit. Bind the observer endpoint,
   credential authority/subject/audience, immutable configuration, adapter and
   executable digests, plus controller UID/groups/extras/authentication
   authority and independently reviewed absence of an impersonation path.
   Produce a fresh
   authoritative NodeGroup membership export and signed membership receipt,
   then produce a fresh signed client-identity receipt from independent provider/LB,
   listener, backend, SG, route-table, XFF-mutation, and direct-access captures;
   store it mode 0600 at the fixed run-root path, and store the seven reopened
   raw inputs under the fixed mode-0700 evidence directory. Prove the provider
   relation, executable identities, managed-node controller identity, and
   derived client address cannot be forged.
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
   `NoSchedule`/`NoExecute` taints, `maxUnavailable <= 1`, positive surge, at
   least two retained system nodes, and accepted traffic policies.
9. Prove the existing Deployment/Service to StatefulSet/headless/Sentinel
   transition is a non-destructive staged migration: no old resource is deleted
   or replaced before the new single-primary/quorum contract is Ready, and no
   policy is enabled before authenticated identity evidence passes.
10. Exercise landing/catalog, PAT/model authorization, sync/stream inference,
   MCP, admin, operations/results/artifacts/uploads, storage, queue/model
   admission, observability, and rollback. Include an active audio stream and
   confirm idle, 7,500-second stream, and 7,800-second connection ceilings.

Rollback is ordered and reversible: first restore the prior application Helm
revision so no active policy depends on the global rate-limit service; then
restore the prior foundation Helm revision and Terraform plan. Keep the HA
store until the policy and generated rate-limit service are confirmed absent.
A live operator must use the recorded prior revisions and state-backed plan,
not source assumptions, and must re-run the same customer and operator smokes
after rollback.

No live resource, credential, registry, provider, database, or customer payload
was inspected or changed while authoring this candidate.

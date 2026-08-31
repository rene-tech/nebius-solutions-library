# fs2-serve control-plane operations

## Authority and request path

The gateway has no model schema of its own. At startup it calls
`fs2_serve_catalog.consumer.load_gateway_catalog` against the canonical
`k8s-inference/catalog/runtime` base and its versioned serving-binding overlay.
The join fails closed on catalog/model digests, immutable model and runtime
identity, qualification, execution mode, protocol endpoints, operations,
license/entitlement policy, B300 identity, and MCP capability. An overlay that
names models but produces zero routes is rejected. The models lane owns the
base catalog and live overlay ConfigMaps plus the immutable qualification
receipt tree mounted read-only at `/etc/fs2-serve/evidence`. The chart expects
that tree through `catalog.evidencePersistentVolumeClaimName`; an enabled
binding cannot start without its digest-addressed evidence.

Enabled routes additionally require fresh signed evidence verified against the
file-mounted `FS2_ROUTE_ATTESTORS_FILE` trust root. The file is a bounded JSON
map from `sha256:<Ed25519-public-key-digest>` to a canonical raw base64url
public key. Keep it in the dedicated projected public-key Secret; never place
attestation private keys in the gateway Pod. The chart mounts the Secret as a
directory, not with `subPath`, so Kubernetes' atomic projected-volume update
is observable without a rollout. The parser accepts 1-32 keys and at most
64 KiB. Rotate by first projecting old and new keys, publishing a complete new
evidence session signed by the new key, waiting for every replica to report a
new validation generation, and only then removing the old key. Removing the
only signer before the new binding is valid withdraws every route.

The gateway reopens the canonical typed serving-binding loader, its immutable
catalog scale contracts, and signed activation lifecycle bindings at startup, every
`FS2_ROUTE_REVALIDATION_INTERVAL_SECONDS` (1-300 seconds), model listing, MCP
list/call, durable admission, controller claim/pre-mutation/post-mutation, and
immediately before inference dispatch. Route and scale evidence are validated
under one registry transaction. Expiry, key removal, an untrusted signer,
replayed subject/nonce, wrong evidence session, stale target identity, changed
catalog digest, or any other failure replaces the current projection with one
route-free snapshot. Listing, OpenAI/native/batch admission, and MCP filtering
therefore observe the same generation. A later complete valid set recovers
atomically. The gateway no longer accepts a separate activation-contract file;
the model lane's typed `GatewayModel.scale_contract` and signed
`GatewayModel.binding.activation` are the only mutation-authority inputs.

Envoy Gateway owns public TLS and coarse edge policy. This service strips all
caller-supplied `x-fs2-*` identity headers, authenticates its opaque PAT, checks
tenant/model/operation/use-policy scopes, and commits the operation before
activation. That commit is durable T0. Synchronous HTTP waiting is bounded;
the same durable row becomes a `202` operation whenever activation or inference
does not finish inside the wait. Kueue is not in this synchronous admission
path. Batch bindings may use it behind their runtime/activation adapter.
Wait reads use bounded exponential polling from
`FS2_WAIT_POLL_INITIAL_SECONDS` to `FS2_WAIT_POLL_MAX_SECONDS`, never a fixed
50 ms database loop. Each replica holds at most `FS2_MAX_SYNC_WAITERS`
connections, which must be at least its worker concurrency. Once those slots
are full, a newly committed request immediately returns its durable `202`
operation instead of failing or becoming unreachable. The edge adds a
configurable Envoy Gateway local rate limit (200 requests/second per Envoy by
default); PostgreSQL token budgets and operation concurrency remain the
authoritative cross-replica controls. The local edge limit is merged with any
Gateway-level platform policy rather than replacing it.
The chart's public `HTTPRoute` exposes only `/v1`, exact `/mcp`, and exact
`/.well-known/oauth-protected-resource` plus its resource-specific `/mcp`
variant; bootstrap admin, metrics, probes, and
internal ext-auth/OpenAPI paths remain cluster-internal. Enabling the route
without a parent reference fails rendering. DNS mode requires an explicit
hostname. IP mode is the source-chart default and forbids the `hostnames` value
entirely, so rendered `spec.hostnames` can be neither an IP literal nor an
empty list. It requires a private, project-checked static allocation input and
an exact IPv4 Certificate SAN.
The public model projection deliberately omits the cluster `service_origin`
and activation URL. Readiness fails closed if PostgreSQL is unavailable, route
revalidation is unhealthy, the canonical join has no routable model, a claim
loop is unhealthy, or the in-process maintenance loop has not completed a
successful pass.

Local activation never traverses HTTP. Admission commits a PostgreSQL intent
bound to its operation attempt and binding digest, then waits for a fenced
`ready` outcome. A distinct two-replica controller owns the only Kubernetes
identity. It uses one pre-created named Lease, PostgreSQL leases/fencing, and a
model advisory lock shared with admission. Its projected service-account token
has the Kubernetes API audience, expires after 600 seconds, and is reread on
every request. The controller rereads the exact KSA and Lease through the public
Kubernetes API and binds each database heartbeat to Pod namespace/name/UID, KSA
name/UID, Lease namespace/name/UID/resourceVersion, and the Pod-UID-derived
holder identity. Exact Roles allow only get/patch on configured resourceNames
for Deployments, NIMServices, or Jobs in `fs2-models`, get on the one controller
ServiceAccount, plus get/patch/update on the named Lease. The typed boundary and chart must agree on
`fs2-system/fs2-serve-control-plane-activation-leader` and
`fs2-models/fs2-serve-control-plane-activation-targets`; a mismatch withdraws
the route before a claim. No Service exposes the probe-only controller port; there is no
activation mutation endpoint, static activation bearer secret, arbitrary
create/delete, Secret, exec, Node, or cluster-wide permission.

Only enabled routes whose typed backend is `local-kubernetes`, whose signed
activation binding is enabled, and whose immutable scale contract validates as
`replica-scale` require the controller dependency. Conventional always-on local
and federated routes do not. After a complete route refresh and successful Lease
renewal, the leader publishes a short-lived PostgreSQL heartbeat bound to the
SHA-256 of a canonical, sorted projection of each required route's model ID,
model revision, binding digest, scale-contract digest, and PostgreSQL-intent
interface digest. The gateway recomputes the same projection for `/readyz` and
requires exact digest equality. Expiry, key rotation, model revision changes,
binding or scale-contract changes, and a stale controller generation therefore
return `503` until a leader on the new exact set refreshes the heartbeat. The
digest discloses none of those identities. Controller `/livez` remains
dependency-minimal: route, database, and leadership loss affect readiness,
while an absent or exited reconciliation supervisor fails liveness so the Pod
is replaced instead of remaining healthy but inert.

The catalog scale contract fixes the model/execution identity, exact target,
scale bounds, and optional scale-to-zero policy. The signed activation binding
adds the initial target UID, resourceVersion, observed generation, template
identity, and lifecycle receipts. Those signed values anchor an explicit
one-step transition chain; they are not an impossible prediction of the
resourceVersion Kubernetes will assign to a future PATCH. For each mutation,
the controller derives a canonical transition digest from the exact signed
contract/binding, model revision, durable intent ID and operation attempt,
prior UID/resourceVersion/generation/template and active state, and desired
active state. Its resourceVersion-preconditioned PATCH writes the digest in the
`fs2-serve.nebius.ai/activation-transition-sha256` annotation atomically with
the replica change. It accepts only the same UID/template, exactly one new
generation, the desired readiness state, and that exact annotation.

The controller obtains its bounded readiness duration from PostgreSQL
`clock_timestamp()`, the durable deadline, and the current claim lease before
entering the mutation guard. That guard holds one session advisory key for the
model but commits its leader/claim/demand validation transaction before any
Kubernetes work; it revalidates in a second short transaction afterward. It
never holds `fs2_activation_controller_status` across readiness, so Lease and
claim heartbeats continue on independent pool connections during minute-scale
activation. The readiness task is raced against leadership loss and cancelled
on loss. If a controller dies after PATCH but before completion, a new higher
model fence can recover only the exact operation-bound one-step transition from
the signed initial or last durable target; it adopts the annotation and waits
without a blind second PATCH. Completion reopens the
static signed authority and requires unchanged binding/scale digests, then
persists the result under both the monotonic leader-heartbeat epoch and a
separate PostgreSQL-issued per-model mutation fence. Completion performs a
model-lock/CAS against durable UID, template, resourceVersion, generation, and
active state. A distinct older intent becomes the idempotently replayable
`stale_model_fence` terminal result instead of regressing that state. Dispatch
reopens the signed anchor and validates the fenced durable chain.

Expired activation claims are recovered under the current leader and one
materialized PostgreSQL DB-clock boundary. Recovery requeues only while the
deadline remains open and `attempt < max_attempts`, clears the dead claim, bumps
the intent fence, and appends one `activation_intent_lease_requeued` event in
the same transaction. A deadline-expired or attempt-exhausted claim is instead
sealed as `expired` or `failed` with one immutable terminal event. The same
terminalization catches a queued row left by an interrupted legacy recovery.
Concurrent recovery is idempotent: only one transaction changes the row and
appends the event. The admission waiter treats either terminal intent as an
immediate activation failure, durably completes the operation, and replays that
same terminal operation for the original idempotency key. It does not depend on
the operation deadline being present.

Scale-down is admitted only after
the configured idle interval with zero queued/claimed/inflight work, and the
shared model lock prevents new admission from crossing the patch boundary.
Desired replicas equal to zero is not readiness: the public target observation
must also report current observed generation and zero total, ready, and
available target-owned replicas before zero GPU clients and cleanup completion
can be concluded.

Public routes are:

- `/v1/models`, OpenAI-compatible chat/completions/embeddings/images routes,
  and `/v1/models/{model_id}:invoke`;
- metadata-only operation status, cancel, and acknowledge routes;
- an explicit owner-scoped result route—cancel never grants result access;
- legacy bootstrap-admin PAT mint/list/revoke and payload-free audit routes
  under `/admin/v1/*` for CLI compatibility;
- session-authenticated, tenant-aware operator, API-key, audit, and reporting
  routes under `/admin/api/v1/*`;
- `/mcp`, current Streamable HTTP with protocol-specific model tools plus
  owner-scoped status, result, cancel, and acknowledge tools.

MCP calls use the same authorization and durable admission path and receive no
Kubernetes, GPU, database, artifact, or activation credential.

### Operator session and API-key workflow

The SPA owns `/admin/login`; the FastAPI control plane owns only the versioned
`/admin/api/v1/*` BFF boundary. Bootstrap without JavaScript by using a
mode-0600 cookie jar. This flow works directly against the chart-owned HTTPS
endpoint and does not assume an auth proxy:

```bash
set -eu
FS2_ADMIN_BASE=https://203.0.113.10
umask 077
FS2_ADMIN_COOKIE_JAR=$(mktemp)
read -r -s -p 'Bootstrap token: ' FS2_BOOTSTRAP_TOKEN
printf '\nheader = "Authorization: Bearer %s"\n' "$FS2_BOOTSTRAP_TOKEN" |
  curl --fail --silent --show-error --config - --request POST \
    --cookie-jar "$FS2_ADMIN_COOKIE_JAR" \
    "$FS2_ADMIN_BASE/admin/api/v1/session"
unset FS2_BOOTSTRAP_TOKEN
curl --fail --silent --show-error --cookie "$FS2_ADMIN_COOKIE_JAR" \
  "$FS2_ADMIN_BASE/admin/api/v1/keys?tenant_id=tenant-a"
```

The session exchange replaces any prior valid session with a random
`__Host-fs2_admin_session` cookie. The cookie is `Secure`, `HttpOnly`,
`SameSite=Strict`, has `Path=/`, and has no `Domain`. Neither the response body
nor any redirect contains the bootstrap credential or opaque session secret.

An administrator can hand off a scoped viewer/operator/admin identity by adding
the bounded JSON body `{"principal_id":"<operator-principal-uuid>"}` to the
session `POST`. The bootstrap credential authorizes that handoff; the selected
principal's role and tenant are enforced on every later request by the server.
Tenant-bound principals cannot enumerate another tenant or consume global
fleet aggregates. Every enabled viewer can read `/admin/api/v1/context`, so a
tenant session can initialize the SPA and select its deployment context. The
overview and model inventory remain global-only in this release: those
responses combine shared fleet runtime state and aggregate accounting rather
than a tenant-safe catalog projection, and return 403 to tenant identities.
Viewer can read scoped keys/audit/operations, operator can also issue,
atomically rotate, and revoke keys, and admin can manage operator principals
within the same tenant. Only a global admin can create or modify a global
principal. Delete `/admin/api/v1/session` and securely remove the cookie jar at
the end of an operator session.

API-key issue and rotation responses disclose the new opaque PAT exactly once
and carry `Cache-Control: no-store`. List and audit responses expose only
bounded metadata, fingerprint/prefix, and durable totals. `last_used_at` means
the last accepted, non-replay inference admission; bearer verification and
catalog-only calls do not add a PostgreSQL write to the authentication hot
path. Bounded integer `prompt_tokens`/`completion_tokens` (or the exact
`input_tokens`/`output_tokens` aliases) are retained from successful OpenAI
chat, completions, and embeddings responses. Malformed, ambiguous, missing, or
out-of-range usage stays unavailable without failing otherwise valid
inference. Native imaging/BioNeMo modality totals remain unavailable unless a
runtime adapter explicitly reports them. GPU-seconds are explicitly marked
`estimated` when derived from admission reservations. Legacy `/admin/v1/*` CLI
routes continue to accept the bootstrap bearer credential but are not browser
BFF routes.

Deferred after this MVP: external OIDC/SSO and independent per-user login,
CSRF nonces beyond exact Origin enforcement plus `SameSite=Strict`, automatic
expired-session row cleanup, and measured rather than reservation-estimated GPU
accounting. These do not change the current session, tenant, audit, or
one-time-disclosure contracts.

The canonical Streamable-HTTP URL is exactly `/mcp`, without a required
trailing slash. The SDK child owns that `/mcp` route and is deliberately mounted
at the parent root after the parent API routes, avoiding both `/mcp/mcp` and a
redirect-only `/mcp/` topology. `mount_mcp` explicitly enters the SDK child's
lifespan from the parent FastAPI lifespan because Starlette does not start a
mounted child's lifespan. Startup
therefore starts the admission workers and then the MCP manager; shutdown stops
the MCP manager before draining the admission workers. Streamable HTTP is
explicitly stateless: no process-local session ID is issued or required, so an
Envoy/Service may send initialize, tools/list, tools/call, status, result, and
ack requests to different replicas. Durable operation state remains in
PostgreSQL. This ownership is explicit because mounted Starlette child
lifespans are not a reliable process-lifecycle hook. Slash redirects are
disabled, including when an untrusted forwarded-proto header is present.

MCP DNS-rebinding protection remains enabled independently of the pod's
`0.0.0.0` Uvicorn bind address. Exact Host and Origin allowlists are derived
only from the validated `FS2_PUBLIC_BASE_URL` HTTPS origin (including its
explicit port, if any); credentials, wildcard/oversized hostnames, paths,
queries, and fragments fail startup. Keep that URL equal to the public Gateway
authority. In direct-IP mode it must be an exact IPv4 HTTPS origin on port 443;
both `IPv4` and `IPv4:443` are accepted and `X-Forwarded-Host` is ignored. The
same Host/Origin enforcement wraps every public `/v1`, `/mcp`, and
protected-resource metadata request. No separate environment allowlist can
broaden the trust boundary. OAuth metadata is served at both the pathless
compatibility location and canonical resource-specific
`/.well-known/oauth-protected-resource/mcp`; both advertise exact `/mcp`.

OpenAI-compatible routes use the request's selected, enabled canonical model
and route protocol to resolve the semantic policy operation. The base policy
and live binding must match exactly and contain one operation. Empty or
multi-operation policies are not positionally paired with protocol lists; the
route fails closed before durable admission. This permits `openai-chat` to map
to `chat` for Qwen/GLM and to `analyze-image` for CXR without route-owned
policy.

The Python wheel explicitly contains `fs2_serve`, all versioned SQL migrations,
and the canonical `fs2_serve_catalog` package sourced from
`k8s-inference/catalog/runtime`; the final container contains only the locked
installed virtual environment and imports in isolated mode. No runtime
`PYTHONPATH` is required. `serve`, `wait-schema`, and
`maintenance` never run schema DDL. Only the serialized, DDL-role migration
Job or an explicit operator-run `migrate` CLI does; that entry point requires
no payload, ledger, PAT, or route-attestor key.
`0005_terminal_accounting.sql` and `0006_activation_controller.sql` are
immutable applied versions; packaging tests guard their exact order and
SHA-256 values. The singleton generation heartbeat is created only by monotonic
`0007_activation_controller_health.sql`. Additive
`0008_activation_fencing_identity.sql` clears the ephemeral pre-upgrade
heartbeat, requires a complete observed Kubernetes identity on the replacement,
adds separate leadership and per-model mutation fences, and installs the exact
security-definer runtime enqueue function. A database ending at gateway
terminal accounting applies activation as `0006` next; an existing seven-
migration activation database applies only `0008`. Additive
`0009_maintenance_least_privilege.sql` leaves every applied `0001`-`0008` byte
unchanged, adds an explicit payload-erasure marker, and owner-controls the
terminal-accounting trigger. An existing eight-migration database applies only
`0009`. Additive `0010_admin_access_accounting.sql` leaves every applied
`0001`-`0009` byte unchanged and adds operator sessions, key lifecycle metadata,
and typed usage fields; an existing nine-migration database applies only
`0010`. No upgrade path changes an applied hash.

The migration command validates four distinct configurable group-role names
and creates missing reporting, runtime, maintenance, and activation roles as `NOLOGIN`; it
rejects a pre-existing group role with login capability.
ExternalSecret-backed database users must be separate `LOGIN` members of only
their corresponding group role. On every migration pass, the DDL owner revokes
the complete application table, sequence, view, and function surface from all
four limited roles before
reapplying the closed grants. Runtime receives intent and controller-health
`SELECT` plus `EXECUTE` on the exact operation-fenced enqueue and model-lock
functions. It has no direct intent mutation, activation-event write, activation
sequence, target-state, model-fence, or heartbeat mutation privilege.
Runtime can append ordinary audit events but has no usage-fact access and no
`UPDATE`/`DELETE` on audit or usage facts; it cannot delete operations or PATs.
The maintenance role can erase expired ciphertext and delete bounded aged
operations, PATs, audit rows, and usage rows through the maintenance process.
Its column-level reads exclude ciphertext, principals, token digests, results,
and audit detail, and it has no fact `INSERT`/`UPDATE` or schema authority.
The activation role receives intent, target-state, and controller-health
`SELECT`/`INSERT`/`UPDATE`, activation-event `INSERT` plus sequence `USAGE`, and
only operation columns `id`, `model_id`, `model_revision`, `status`, `attempt`,
`lease_expires_at`, and `deadline_at`. It cannot read tokens, audit,
event history, request/result ciphertext, or create schema objects. Only the
migration Job owns role creation and grant changes.

### PostgreSQL cross-lane release contract

`contracts/postgresql-release-contract.json` is the canonical value-suppressed
handoff to the PostgreSQL and supply-chain lanes. Generate the exact same bytes
from a source checkout or installed image with:

```bash
fs2-serve postgresql-release-contract
```

The emitter verifies that the migration directory contains exactly the ordered
`0001` through `0010` set, no missing/extra/renamed/symlinked file, and the
contracted SHA-256 for every file. The migrator and `wait-schema` use the same
validator. They also require the applied migration ledger to be an exact
ordered prefix while an upgrade is running and the exact full set before a
runtime becomes ready; extra or reordered database rows fail closed.

The required final release-receipt inputs are the ordered full-manifest
migration-set SHA-256
`33d3ec1ffaace5521312f5a5c17f2b82bf27800752302cbc06db72adc2ac843b`,
count `10`, first version `0001_initial.sql`, last version
`0010_admin_access_accounting.sql`,
and namespace/role ownership SHA-256
`47397ccc7c42612a11c568101f67ccd7a3446899b2ede5af3bf3bd926aa111ca`.
The whole logical contract payload is SHA-256
`2c0837d8168f8226e3b44597e8d669238dc0eaee179c53f45bc0f49af38f086e`.
The migration Job emits the payload, ordered-set digest, count, first/last
version, and namespace/role digest as annotations. A later additive migration
updates this one manifest contract; Helm and PostgreSQL code must not
special-case an individual migration number.

The namespace split is deliberate and must not be collapsed: CloudNativePG
Cluster `fs2-control-db`, its `fs2-control-db-rw` Service, database `fs2serve`,
and owner role `fs2serve` belong to `fs2-data`; consuming credential Secrets
belong to the workload namespace. Runtime Secret `fs2-system/fs2-serve-database`,
migration-owner Secret `fs2-system/fs2-serve-database-migrations`, maintenance
Secret `fs2-system/fs2-serve-database-maintenance`, and activation Secret
`fs2-system/fs2-serve-database-activation` all use key `url` and have distinct
principals and single named consumers. Reporting uses
`fs2-observability/fs2-serve-database-reporting`. The PostgreSQL platform release
owns Cluster/database-owner/Secret writes. Only
`fs2-system/fs2-serve-control-plane-migrate`, running as
`fs2-serve-control-plane-migration`, owns schema DDL and creation/grants for
NOLOGIN groups `fs2_serve_runtime`, `fs2_serve_maintenance`,
`fs2_serve_activation`, and `fs2_serve_reporting`. Application, maintenance,
controller, and Grafana workloads only consume their named Secret and group
membership.

The PAT and principal that admitted an operation have an implicit capability
to read its status/result, cancel it, and explicitly acknowledge its retained
payload. An HTTP `inference.invoke` token or MCP `mcp.invoke` token therefore
needs no surprise companion scope after receiving `202`. Exact token and
principal ownership are both required. A different token receives the same
not-found result as an unknown ID even if it carries an `operations.*` scope;
only explicit same-tenant `tenant.admin` overrides ownership. The
`operations.*` names remain accepted for compatibility but grant no delegated
cross-token access.

Input boundaries are fail closed before admission or reflection: model IDs are
1-128 characters, idempotency keys are 8-200 characters, synchronous waits are
finite and no greater than the configured maximum, and relative deadlines are
finite in `(0, 86400]` seconds. External absolute timestamps must include a
timezone. PostgreSQL repeats the model/idempotency length checks for stored
operations. Audit `jsonb` details are explicitly decoded and required to be
objects before typed API projection.

## Payload and ledger guarantees

Queued requests and retained results use AES-256-GCM with a fresh 12-byte
nonce and metadata-bound AAD. PostgreSQL stores only `key_id`, nonce,
ciphertext, a private keyed HMAC, content type, and bounded terminal metadata.
It never stores a plaintext request/response or a public raw SHA-256 digest.
The SQL constraints require a complete envelope, 12-byte nonce, and at least a
16-byte GCM tag. Request bytes are decrypted only under a current lease after
activation/readiness and immediately before invocation. Completion encrypts
the response once and does not decrypt it for metrics.

Results survive repeated GET/invoke replays until explicit acknowledgement or
`FS2_PAYLOAD_TTL_SECONDS`; first delivery does not purge them. The fixed-cadence
maintenance CronJob independently purges expired envelopes, reaps stale leases,
terminalizes queued work whose deadline elapsed, and deletes bounded batches.
Deadline finalization atomically releases the unclaimed GPU reservation and
the token concurrency slot while the encrypted request remains retained only
until its existing payload TTL. Terminal operation/idempotency rows are deleted
after `FS2_OPERATION_RETENTION_SECONDS`; revoked/expired PAT verifier rows are
deleted after `FS2_PAT_RETENTION_SECONDS` once no operation references them.
Audit rows have an independent `FS2_AUDIT_RETENTION_SECONDS` bound. A
payload-free `fs2_usage_facts` row is inserted exactly once by the same
database transaction that first makes any operation terminal, including
cancel, revocation, deadline/payload expiry, exhausted release, stale recovery,
preemption, and normal completion. Facts survive shorter operation retention
and expire only after `FS2_USAGE_RETENTION_SECONDS`.
Prometheus terminal request totals, cumulative terminal duration, and
conservative estimated allocation are restart-safe projections of those facts
on every scrape and maintenance pass. The worker completion callback observes
only process-local latency histograms and is deliberately not a terminal count
or accounting authority.
The optional `PrometheusRule` requires the matching `ServiceMonitor` and alerts
on missing route metadata/scrapes, unavailable Deployment replicas, sustained
queue age/depth, synchronous-wait saturation, and authentication failure
spikes. Its expressions use only bounded catalog/state/reason series; they do
not introduce principal, tenant, token, prompt, response, or bearer labels.
Status and cancel always return metadata, never a decrypted result; status
does not mutate or purge result availability. Every lifecycle route and MCP
tool resolves the authenticated principal/token owner before model policy, so
cross-owner identifiers return the same 404/not-found result as unknown IDs.

The PostgreSQL adapter deliberately keeps asyncpg's default JSON/JSONB codec:
audit and operation-event writes remain explicit `json.dumps` values. Because
default asyncpg reads JSONB as text, `list_audit` alone normalizes a bounded
JSON object immediately before `AuditEvent` validation. Malformed JSON,
non-object JSON, oversized text, and unexpected driver value types fail closed
with content-independent errors; stored detail is never reflected.

Error details are payload-independent. Access logs, metrics, and traces never
capture request/response bodies or authorization headers. Prometheus labels
use only bounded catalog/protocol/state values. Per-principal usage comes from
the payload-free PostgreSQL ledger rather than an unbounded Prometheus label.
The runtime HTTP adapter disables environment proxies and forwards only the
random `x-fs2-operation-id` correlation ID plus a syntactically valid, nonzero
W3C `traceparent` with a prompt. Tenant, principal, and token identifiers remain
mapped to that operation inside the control plane because model containers may
log all request headers. Runtime response headers are never trusted for Pod,
node, physical GPU, GPU count, or preemptible attribution. The injected
allocation metadata interface accepts only the opaque operation ID and catalog
model ID; its fail-closed default records no live runtime identity until a
trusted controller/proxy/Kubernetes metadata implementation is configured. Any
future need to send caller identity downstream requires a separately reviewed
mTLS trust boundary plus enforced header and log redaction—it is not a supported
default path. GPU accounting is always labeled a conservative estimated
allocation and has no per-principal metric label or downstream-asserted
preemptible label. Actual GPU utilization remains the separate DCGM panel and
is not billing authority until joined to trusted Kubernetes allocation
intervals.

The adapter bounds and validates required content-type/runtime-status headers,
streams activation/readiness without buffering, and normalizes transport,
protocol, decode, schema, and trusted-metadata failures to bounded codes without
exception chaining. Non-success upstream bodies are closed without buffering
and only status/failure metadata enters the ledger.

## Exact SM90 federation boundary

The canonical catalog and signed serving binding remain the sole promotion
authority. A separate control-plane-owned
`fs2-serve.nebius.ai/federation-routes/v1` document supplies only the private
transport needed to reach an already-qualified federated binding. It cannot
enable a disabled model. Its route set must exactly equal the enabled
non-local binding set, and every route repeats the exact model, runtime image,
endpoint identity, trust-bundle, backend-class, and credential-requirement
digests from that signed binding. Any changed digest is a different subject and
fails startup; newer images are never accepted as aliases.

The federation document, CA bundles, scoped bearer tokens, and optional mTLS
client identities live only in the `fs2-serve-federation` Secret mounted at
`/var/run/secrets/fs2-serve/federation`. The chart can reconcile that Secret
from one provider object through an `external-secrets.io/v1` `ExternalSecret`;
neither values nor rendered manifests contain upstream origins or credentials.
Each credential-requirement ID maps to fixed bounded filenames. The gateway
verifies the mounted CA bytes against the binding's exact trust-bundle digest,
requires TLS 1.2 or newer with hostname verification, and either reads a
bounded scoped bearer at request time or loads the binding-specific mTLS
certificate and key. Rotate provider material, wait for ExternalSecret sync,
then roll every gateway replica so TLS identities and signed route metadata
change together.

Outbound requests never use environment proxies or redirects. The private
route identifies one lowercase DNS Host/SNI value and at most eight exact
globally routable connect IPs; the client connects directly to those IPs while
validating the certificate for the pinned Host. Loopback, private, link-local,
metadata, multicast, unspecified, wildcard, plaintext HTTP, and URL/path
override destinations fail closed. The NetworkPolicy accepts only matching
operator-supplied `/32` or `/128` egress destinations and bounded ports; it
rejects broad Internet CIDRs. Standard Kubernetes NetworkPolicy cannot express
an FQDN policy, so the direct-IP transport, Host/SNI validation, signed endpoint
identity, and exact egress CIDRs form one required boundary.

Federated activation never calls the local GPU scaler. It first checks the
route-specific upstream health endpoint, then the canonical model readiness
and optional warmup probes. Invoke deadlines include all retries and backoff
and are capped at 30 seconds. At most three transport attempts use the same
opaque operation UUID as both correlation and idempotency key; client PAT,
tenant, principal, model credentials, and public trace identity remain inside
the control plane. Retry status codes are a fixed safe set, response bodies are
not read before a retry, and a per-replica circuit breaker bounds repeated
upstream failures. Upstream health/readiness and failure codes remain distinct
from local B300 activation and allocation evidence.

The exact-model inventory currently authorizes no federated route. MolMIM's
exact H200 KServe/NIM backend is the preferred candidate but remains gated on
fresh signed identity/readiness/two-semantic-response receipts and a scoped
credential. Evo2-40B's exact H200 Serverless candidate remains disabled until
credential rotation, immutable digest pinning, and the same qualification.
DiffDock, RFdiffusion, and ProteinMPNN remain historical-only with no
professional exact backend. Do not put any existing service origin, token, or
credential value in Git, a ConfigMap, logs, the public model list, or MCP
metadata. Origins belong only in the provider-backed federation Secret; token
and private-key values are separate files in that same mounted Secret.

## Leases, retries, revocation, and budgets

Claims carry an attempt and monotonically increasing fencing token. Heartbeats
cap the lease at the operation deadline. A heartbeat loss cancels the local
invoke; stale workers cannot complete, retry, or overwrite a newer fence.
Retryable activation, inference, and preemption failures retain the operation
ID and receive bounded attempts, deadline-capped deterministic jitter, and a
new fence. Shutdown stops claims, drains for `FS2_SHUTDOWN_GRACE_SECONDS`, then
cancels and awaits both runtime and heartbeat children before a fenced release;
only then is the local claim cleared. It safely requeues only if token, payload,
deadline, and attempt gates remain valid. Both claim selection and its fenced
update require `attempt < max_attempts`, while PostgreSQL enforces
`attempt <= max_attempts`. Releasing the final admitted attempt terminalizes
the operation as `attempts_exhausted`; it cannot create a free N+1 attempt.
Claim and janitor loops use capped
retry backoff and publish their state through readiness so a silent task crash
cannot leave a Ready replica. A failed fenced shutdown/error release receives
six bounded retries while that worker claims nothing else. If neither release
nor a stale/conflict fence can be established, liveness fails so Kubernetes
restarts the stuck replica instead of leaving capacity permanently lost.

Every claim consumes one conservative full-attempt GPU-seconds charge,
including attempts later lost to cancellation, activation failure, expiry,
stale reaping, or preemption. Only the unclaimed remainder is released. The
metric and dashboard say **estimate**, not measured utilization; DCGM panels
are the distinct source for measured GPU utilization. A future downward
reconciliation requires trusted allocation/DCGM evidence.

PAT revocation takes the token-scoped transaction lock, marks the token
inactive, cancels all queued/active rows, increments their fences, clears
leases, and releases only unclaimed reservations. Claim also rejects an
inactive/expired token. Thus revocation stops future execution even if an auth
request raced the revocation read.

Natural PAT expiry cannot become a queue head. Each claim first terminalizes a
bounded batch of inactive-token queued rows and atomically releases their
unclaimed reservations. The subsequent candidate query joins the token table
and admits only currently active PATs, so inactive rows beyond the cleanup
batch do not hide valid later work. Encrypted request payloads remain subject
to their independent retention TTL; a multi-day payload TTL therefore never
becomes a multi-day admission delay.

Queued deadline expiry is independent of claiming and payload purge. The
fixed-cadence janitor takes the token lock before the operation lock,
terminalizes a bounded batch as `deadline_exceeded`, releases the full
unclaimed reservation in the same transaction, and records a
`deadline_expired` ledger event. A request that is never claimed therefore
stops consuming concurrency and budget reservation without waiting for its
payload TTL.

All mutations that touch both accounting and an operation lock in
token-then-operation order. The token-scoped advisory key permits independent
tokens to proceed in parallel. Heartbeats do not take that lock. Janitors use
bounded `LIMIT`/`SKIP LOCKED` batches, and deadlock/serialization aborts receive
three bounded whole-transaction retries. Only schema migration uses one global
advisory lock, to serialize version/checksum application and `CREATE TYPE`.

## Key separation and staged rotation

Three independent mounted key rings are mandatory:

1. PAT peppers: keyed prehash before Argon2id; never an AEAD/HMAC key.
2. Payload AEAD keys: AES-256-GCM only.
3. Ledger HMAC keys: idempotency/private payload digests only.

Use this staged rotation procedure; do not change a key in one pod at a time:

1. Add old and new key IDs/material to the relevant mounted ring and roll all
   replicas. Verify every pod has the dual-key file without printing material.
2. Switch `active_key_id` to the new key and roll all replicas. New writes use
   the new ID; old rows remain readable/verifiable.
3. For a PAT pepper, successful authentication rehashes that token to the
   active pepper. Keep the old pepper through the maximum token deletion
   horizon for inactive tokens that cannot rehash.
4. Keep an old payload key for at least the payload TTL plus rollout/clock
   margin. Keep an old ledger HMAC key for at least the operation/idempotency
   retention horizon plus rollout/clock margin. Removing an HMAC key early
   makes old idempotency replays fail closed with conflict; it never silently
   rebinds the key.
5. Confirm no retained row references the old ID, then remove it from every
   pod in one rollout. The ledger HMAC and payload AEAD keys must never share
   material or a Secret key.

## Helm deployment and handoff

The chart expects pre-created Secret references, the models lane's read-only
canonical catalog filesystem PVC, serving-binding/scale-contract ConfigMap, and
separate read-only qualification-evidence PVC. It
creates no plaintext Secret; when enabled, its `ExternalSecret` contains only a
provider reference and reconciles the private federation Secret. It runs as UID
65532 with a read-only root filesystem, dropped capabilities, no service-account
token, PSS-compatible seccomp settings, resource bounds, HA rolling strategy,
PDB/HPA, default-deny ingress/egress, probes, a serialized migration Job, and a
fixed-cadence maintenance CronJob. Set `catalog.rolloutDigest` to the exact
models-lane publication digest so catalog changes cause a rollout; empty,
placeholder, and zero digests are rejected. The nested canonical catalog tree
cannot be represented truthfully by one flat ConfigMap volume.

Gateway, maintenance, and migration use three distinct service accounts and
component selectors. The Service and PDB select only `component=gateway`;
maintenance and migration Pods can never become serving endpoints. On install,
the migration Job is an ordinary release resource so Helm can create its
ServiceAccount and referenced Secret provider alongside the Job and Deployment;
the Deployment's DML-only `wait-schema` init process is released when that Job
finishes. On upgrade, the same Job is a serialized `pre-upgrade` hook using the
already-present identity and Secret, so it completes before Helm rolls out an
image whose init process waits for the new schema. Both modes avoid a
migration/Deployment wait cycle. The Job receives only the DDL-capable
`secrets.migrationsDatabase` reference.
Maintenance receives only `secrets.maintenanceDatabase` plus bounded retention
durations. The explicit erasure marker lets it purge ciphertext without payload
or ledger keys. It receives no runtime database, catalog, payload/ledger
keyring, PAT-pepper, route-attestor, admin, activation, federation, or DDL
credential.
Payload AEAD, ledger HMAC, PAT pepper, and public route-attestor material are
separate Secret objects and projected only into consumers that need them.
`secrets.migrationsDatabase`, `secrets.database`, and
`secrets.maintenanceDatabase` must remain distinct; Helm rejects any Secret/key
reuse. The gateway chart does not accept an activation Secret, service account,
controller Deployment, RBAC, NetworkPolicy, projected Kubernetes identity, or
controller settings. The PostgreSQL release contract still provisions the
distinct activation group role and names the separately reviewed activation
child as its only consumer. That child must mount its credential and Kubernetes
authority in its own chart and integration review. There is no activation HTTP
Service, shared token, runtime-to-controller egress path, or Kubernetes writer
in this gateway publication.
The activation role first loses every table privilege, then receives only
column-level operation reads limited to `id`, `model_id`, `model_revision`,
`status`, `attempt`, `lease_expires_at`, and `deadline_at` for claim state;
read/write access to its intent,
target, and heartbeat tables, and INSERT plus sequence use for activation
events. It cannot read PAT, principal, tenant, idempotency, request, response,
or result material, and it cannot select or update activation events.
Migration creates separate NOLOGIN runtime and reporting group roles, revokes
schema creation from `PUBLIC` as well as both group roles (closing inherited
privileges on databases created with older PostgreSQL defaults), grants runtime
only the required DML tables/sequences, and
grants reporting only three aggregate views. Grafana never selects raw
`fs2_operations`, `fs2_tokens`, or ciphertext. A separately provisioned
Grafana login may be made a member of the reporting role without mounting that
credential into any gateway workload.
The dashboard ConfigMap is rendered directly into `fs2-observability`, the
exact namespace watched by the pinned foundation Grafana sidecar; it carries
only `grafana_dashboard=1` and contains no datasource credential.
Its PostgreSQL panels bind fixed UID `fs2-serve-reporting`. The PostgreSQL lane
owns the separately value-suppressed datasource Secret contract and reporting
login; the gateway renders only dependency names and the two aggregate-view
allowlist, never a datasource value or raw-table privilege.

The chart owns the complete concrete direct-IP edge: one cluster-scoped
`GatewayClass`, its namespaced `EnvoyProxy`, one namespaced `Gateway`, the
application `HTTPRoute`, the short-lived IP `Certificate`, and optionally a
namespaced ACME `Issuer`. The cluster lane owns only the pinned Gateway API,
Envoy Gateway, and cert-manager CRDs/controllers. The `GatewayClass` binds the
`EnvoyProxy` through `parametersRef`; that proxy's data-plane Service carries
only the private deploy-time
`nebius.com/load-balancer-allocation-id` annotation. Per the
[Nebius load-balancer contract](https://docs.nebius.com/kubernetes/clusters/load-balancer),
the public Service deliberately omits `nebius.com/load-balancer-type: internal`.
Helm refuses the edge without the binding, a project attestation that
differs from `publicLoadBalancer.targetProjectId`, or a non-public-IPv4
allocation type. It also rejects empty and conventional placeholder project or
allocation identities; the exact private provider IDs must enter through
release values.

The `EnvoyProxy` fixes `externalTrafficPolicy: Cluster`. Nebius CCM rejects
`Local` for this managed LoadBalancer path, so changing that field is not a
source-IP preservation option. Its StrategicMerge Service patch also pins the
reviewed production mappings: HTTP `80 -> 10080` on NodePort `31425` and HTTPS
`443 -> 10443` on NodePort `32633`. NodePorts are values because a collision
may require a reviewed replacement, but the schema restricts them to
`30000..32767`, requires distinct values, and fixes the listener/target pairs
to the Envoy Gateway v1.8.3 contract. Change a NodePort only in the chart and
Terraform `public_edge_service_ports` together, then review a fresh Terraform
plan before reconciling the Service.

Nebius worker security groups must admit all three layers for both protocols:
public listeners `80/443`, shifted Envoy targets `10080/10443`, and pinned
NodePorts `31425/32633`. Allowing only the public listeners can produce
intermittent reachability because managed-LB paths may be filtered before or
after translation. The Terraform-owned stateful TCP rule is the source of
truth. A Kubernetes `EnsuredLoadBalancer` event is not external-reachability
proof; after an edge change, probe repeated trusted TLS connections and verify
the rendered Service mapping before declaring the endpoint healthy.

The `Gateway` deliberately has no `addresses` or listener `hostname`. Its
HTTP:80 listener admits only same-namespace `HTTPRoute`s. A catch-all
`PathPrefix /` route redirects with status 308 to HTTPS; cert-manager's Exact
temporary HTTP-01 challenge route has Gateway API precedence over that
redirect. The static application route attaches by `sectionName` only to HTTPS:443, terminates the referenced TLS
Secret, and exposes only `/v1`, exact `/mcp`, and the two exact protected-resource
metadata paths. It never exposes admin, probe, metrics, schema, or activation
paths on plaintext HTTP.
An Envoy `ClientTrafficPolicy` attaches only to the HTTPS listener and fixes
the accepted protocol range to TLS 1.2 through TLS 1.3.

The optional namespaced issuer selects only Let's Encrypt's exact staging or
production directory, the `shortlived` ACME profile, a generated account-key
Secret reference, and the HTTP listener parent. The `Certificate` contains only
the exact IPv4 SAN, requests the 160-hour lifetime, renews with 40 percent of
the issued lifetime remaining, and rotates the private key on every issuance.
Production rendering is refused until release values attest that the same IP
received a Ready staging certificate, the temporary HTTP-01 solver was
externally reachable through Envoy, the staging HTTPS endpoint succeeded, and
bind a non-zero SHA-256 of the
sanitized staging receipt. This is a promotion interlock, not evidence by
itself: the operator must retain the Ready/renewal and public HTTP-01 receipt.
No Secret value, ACME account key, allocation ID, contact address, or staging
receipt is committed. Before rendering private release values, read back the
exact Terraform-retained allocation and verify its live provider parent/type;
before rollout, verify the accepted `GatewayClass`, programmed HTTP listener,
Ready staging certificate, solver reachability, programmed HTTPS listener, and
EnvoyProxy/TLS references. Schema checks are not provider ownership evidence.

NetworkPolicies bind both namespace and Pod labels. Foundation default-denies
`fs2-system` and `envoy-gateway-system`; this release supplies the complementary
flows without guessing provider health-check CIDRs. Public IPv4 ingress selects
only the Envoy Gateway v1.8.3 managed proxy and its reconciled listener target
ports 10080/10443. The worker VPC rule separately admits the full listener,
target, and NodePort tuple documented above. That proxy may reach only gateway port 8080, cert-manager's
stable HTTP-01 solver selector on 8089, and the exact Envoy Gateway controller
selector on xDS port 18000. Matching gateway, solver, and controller ingress
policies are included. The pinned artifact observation is
`contracts/public-edge-artifact-observations.json`; it is optional hardening,
not a bespoke render-time release gate. The networked provenance check
`scripts/verify-public-edge-artifacts.sh` fetches the exact Envoy Gateway 1.8.3
and cert-manager 1.21.1 OCI artifacts and Git tags, verifies their immutable
digests, and checks the managed-proxy selectors, 10000 listener-port shift,
HTTP-01 solver label/8089 port, exact challenge match, and Gateway parent
behavior. Prometheus scrape and
OTLP export use separate selectors in `fs2-observability`; PostgreSQL traffic
goes only to `cnpg.io/cluster=fs2-control-db` in `fs2-data`; runtime traffic
goes only to the canonical models-lane `part-of=fs2-serve` / `managed-by` Pod
label contract in `fs2-models`. An activation controller and Kubernetes
mutation authority are not deployed by this chart; the distinct activation
lane owns that implementation. Maintenance and migration deny all
ingress and may reach only selected cluster DNS and the database on their exact
ports. Change a selector only after inspecting the live retained component
labels; never broaden it to an entire namespace.

The chart intentionally has invalid empty defaults for the immutable image and
public/authorization URLs. Rendering requires exact non-placeholder values.

Build only an exact committed source through the repository-root wrapper. It
uses `git archive`, then asks BuildKit's `context-audit` target to materialize
the Dockerfile-specific filtered context and requires byte-path equality with
the committed allowlist. A generic adjacent `.dockerignore`, working-tree
state, untracked files, tests, evidence, and secret-named material are never
inputs. Both Python stages and the Dockerfile frontend are digest-bound;
runtime and PEP 517 dependencies are hash-constrained from `uv.lock`; only an
application wheel is installed without dependency resolution. The build emits
max-mode BuildKit provenance/SBOM requests and verifies commit/tree/lock/build
policy OCI labels before writing its local non-secret provenance record:

```bash
python3 k8s-inference/components/control-plane/scripts/build_image.py verify-context --ref EXACT_COMMIT
python3 k8s-inference/components/control-plane/scripts/build_image.py build \
  --ref EXACT_COMMIT \
  --builder ATTESTATION_CAPABLE_BUILDX_BUILDER \
  --image fs2-serve-control-plane:EXACT_COMMIT \
  --provenance-file /tmp/fs2-serve-control-plane.provenance.json \
  --oci-file /tmp/fs2-serve-control-plane.oci.tar
```

Publishing, signing, and attaching registry-native attestations are later
reviewed actions. Do not treat the local tag or provenance JSON as a registry
digest, signature, or deployed release.

Render and validate before deployment:

```bash
helm lint deploy/helm/fs2-serve-control-plane \
  --namespace fs2-system \
  --set image.repository=REGISTRY/fs2-serve-control-plane \
  --set image.digest=sha256:EXACT_DIGEST \
  --set catalog.rolloutDigest=sha256:EXACT_MODELS_PUBLICATION_DIGEST \
  --set config.publicBaseUrl=https://PUBLIC_IPV4 \
  --set config.publicAuthorityMode=ip \
  --set config.authorizationServerUrl=https://PUBLIC_IDENTITY_HOST \
  --set publicGateway.enabled=true \
  --set publicLoadBalancer.enabled=true \
  --set publicLoadBalancer.targetProjectId=TARGET_PROJECT_ID \
  --set publicLoadBalancer.allocationProjectId=VERIFIED_ALLOCATION_PROJECT_ID \
  --set-string publicLoadBalancer.allocationId=VERIFIED_PRIVATE_ALLOCATION_ID \
  --set publicTls.enabled=true \
  --set publicTls.ipAddress=PUBLIC_IPV4 \
  --set publicTls.issuerRef.name=fs2-serve-ip-acme \
  --set publicTls.acmeIssuer.enabled=true \
  --set httpRoute.enabled=false
helm template fs2-serve deploy/helm/fs2-serve-control-plane \
  --namespace fs2-system \
  --set image.repository=REGISTRY/fs2-serve-control-plane \
  --set image.digest=sha256:EXACT_DIGEST \
  --set catalog.rolloutDigest=sha256:EXACT_MODELS_PUBLICATION_DIGEST \
  --set config.publicBaseUrl=https://PUBLIC_IPV4 \
  --set config.publicAuthorityMode=ip \
  --set config.authorizationServerUrl=https://PUBLIC_IDENTITY_HOST \
  --set publicGateway.enabled=true \
  --set publicLoadBalancer.enabled=true \
  --set publicLoadBalancer.targetProjectId=TARGET_PROJECT_ID \
  --set publicLoadBalancer.allocationProjectId=VERIFIED_ALLOCATION_PROJECT_ID \
  --set-string publicLoadBalancer.allocationId=VERIFIED_PRIVATE_ALLOCATION_ID \
  --set publicTls.enabled=true \
  --set publicTls.ipAddress=PUBLIC_IPV4 \
  --set publicTls.issuerRef.name=fs2-serve-ip-acme \
  --set publicTls.acmeIssuer.enabled=true \
  --set httpRoute.enabled=false \
  > /tmp/fs2-serve.yaml
```

Keep `publicTls.acmeIssuer.environment=staging` and the application route
disabled until the exact staging certificate and renewal/HTTP-01 receipt pass.
The production values then additionally set `environment=production`,
`productionPromotion.approved=true`,
`productionPromotion.stagingCertificateReady=true`,
`productionPromotion.stagingSolverReachabilityReady=true`,
`productionPromotion.stagingExternalHttpsReady=true`, the exact
`productionPromotion.stagingIpAddress`, and its sanitized
`productionPromotion.stagingReceiptSha256`. Enable `httpRoute.enabled=true`
only after the production certificate and HTTPS listener are Ready.

The deferred source-only Helm 4 gate deploys the actual chart and dirty candidate image
only into a uniquely named ephemeral Kind cluster. It uses `--wait=watcher
--wait-for-jobs`, proves a clean zero-route install, a same-manifest idempotent
upgrade, and automatic rollback after a failing pre-upgrade migration, then
deletes its cluster and registry:

```bash
bash scripts/test-helm4-lifecycle.sh
```

This gate is not a publication blocker for the lean source handoff while the
shared host is at its inotify-instance ceiling. The exact phase-attributed
attempts and the bounded retry condition are recorded in
`docs/fs2-serve/DEFERRED-HARDENING.md`; do not rerun it while the foundation or
B300 task-owned Kind clusters are active.

### Lean-live model image promotion

Advance a retained model's manifest and `all-models-live-services.json` digest
only after the exact candidate Pod is bound to a B300/CC10.3 runtime receipt,
two concurrent semantic validators pass, health and readiness remain within the
declared latency gate throughout that load, and the complete public HTTP/MCP
suite passes. Commit only a secret-free hash index; keep raw Pod, log, metric,
and acceptance receipts outside Git.

The 2026-08-27 DiffDock threaded-server candidate passed that boundary with
four serialized semantic requests, 82/82 successful concurrent probes (maximum
0.958066 seconds), and 15/15 HTTP plus 15/15 MCP acceptance. Its manifest may
therefore use HTTP `/readyz` and `/healthz` probes backed by the reserved probe
handler. The sibling ProteinMPNN candidate reached 1.084021 seconds on
`/readyz`, exceeding the one-second gate, and was rolled back; its manifest and
inventory must retain the prior digest. The decision and external evidence
hashes are recorded in
`models/structure/evidence/threaded-health-qualification.json`. This is a lean
live exception, not a signed formal variant promotion: static variant route
authority remains false.

Run the source acceptance harness directly with the control-plane virtual
environment; the script prepends its adjacent control-plane and catalog source
packages so an older installed wheel cannot supply stale release contracts:

```bash
k8s-inference/components/control-plane/.venv/bin/python \
  k8s-inference/components/control-plane/scripts/accept_all_models_live.py \
  --endpoint https://PUBLIC_ENDPOINT \
  --token-file /OWNER_ONLY/PAT_FILE \
  --output /OWNER_ONLY/acceptance.json \
  --timeout-seconds 7200 \
  --concurrency 4
```

The token file remains owner-owned mode `0600`; never print it or persist its
value in the evidence receipt.

The default `--tls-mode verified` uses normal certificate verification for
both the HTTP and MCP transports. Only the Terraform disposable staging gate,
whose Let's Encrypt staging chain is intentionally not publicly trusted, may
add `--tls-mode disposable-staging-insecure`. That explicit mode refuses DNS,
private, loopback, reserved, and IPv6 authorities; it accepts only a globally
routable IPv4 HTTPS origin and records the bypass in the evidence receipt. It
does not make a production endpoint eligible to skip verification.

The retained 2026-08-27 reconciliation is deployed as Helm revision 8. The
control plane runs the immutable `sha256:b307083e...` index built from
`ed42bff1`, and mounts release `e6643ca0b1d5` (catalog `7d678cdb...`, inventory
`435f5475...`). The post-rollout public harness passed 15/15 HTTP and 15/15 MCP
with terminal operation identity/accounting checks. During that run the HPA
scaled from its two-replica floor to three and returned to 2/2 Ready with zero
restarts after its 300-second stabilization window. The public Gateway
intentionally does not route the internal `/healthz` endpoint, so its public
404 is expected; `/v1/models` and `/mcp` remain 401 without authorization and
both OAuth protected-resource metadata paths return 200. The secret-free
release and rollback tuple is recorded in
`models/structure/evidence/threaded-health-deployed-provenance.json`; raw
operational receipts remain outside Git.

Before a shared rollout, record the current Deployment/ReplicaSet image digest
and source commit, integrate it into the deployment branch, and verify existing
catalog/MCP routes after rollout. The chart retains the platform by design.

Only after the live endpoint passes unauthorized, wrong-scope, revoke,
catalog, inference, MCP, ledger, metrics, logs, and traces smokes should the
manager mint Rene's token. Write it once to
`<private-state-dir>/github/rene.pat` with mode `0600`, transmit it outside
Git/logs/task-card text, and do not print it. The token file does not exist
before live verification.

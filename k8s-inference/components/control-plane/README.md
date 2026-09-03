# fs2-serve control plane

`fs2-serve-control-plane` is the authenticated, durable admission gateway for
the fs2-serve platform. It consumes the canonical model catalog through
`fs2_serve_catalog.consumer.load_gateway_catalog`; it does not define a second
model authority.

The service provides revocable scoped PATs, OpenAI-compatible and native
admission routes, encrypted PostgreSQL operations, Streamable-HTTP MCP tools,
payload-free Prometheus/OpenTelemetry instrumentation, and an exact-binding
federation transport for qualified SM90 upstreams. The private transport
overlay cannot promote catalog models and never appears in public model or MCP
metadata. See
[`docs/operations.md`](docs/operations.md) for deployment, retention, and key
rotation procedures.

The optional scientific-batch consumer adds durable staged Operations,
canonical scientific request/result validation, fenced Kueue Job/JobSet
reconciliation, and shared artifact-result projection. It is disabled by
default and fails closed unless qualified catalog profiles plus immutable
scheduling and execution maps are mounted. See
[`docs/scientific-batch-controller.md`](docs/scientific-batch-controller.md).

Chart `0.3.0` owns the concrete direct-IP edge as one bound
`EnvoyProxy`/`GatewayClass`/`Gateway`, an HTTPS-only application `HTTPRoute`,
an exact-IP 160-hour `Certificate`, and an optional namespaced Let's Encrypt
`shortlived` ACME `Issuer`. HTTP:80 has a redirect-only catch-all;
cert-manager's more-specific exact HTTP-01 solver route takes precedence.
The Gateway-scoped `ClientTrafficPolicy` accepts only TLS 1.2 and 1.3.
Production issuance is gated on a Ready staging receipt.
The chart creates no Secret and accepts the retained allocation only as a
private, project-verified release input.
Its Envoy Service contract explicitly uses `externalTrafficPolicy: Cluster`
because Nebius CCM rejects `Local`, and pins HTTP
`80 -> 10080 / NodePort 31425` plus HTTPS
`443 -> 10443 / NodePort 32633`. A matching Terraform worker-security-group
rule admits each listener, shifted target, and NodePort; all six ports are
required for reliable managed-LB traversal.

The same chart can optionally deploy the separately built FS2 admin console as
a digest-only, provenance-annotated static workload. The UI has no service
account token, Secret, database connection, Kubernetes client, or observability
credential. A dedicated HTTPS `HTTPRoute` sends the more-specific
`/admin/api` prefix to the typed control-plane BFF and `/admin` to the static
service; caller-supplied FS2 identity headers are removed on both rules. The
workload runs as UID/GID 101 with a read-only root filesystem and a bounded
memory-backed `/tmp`. It is disabled by default and cannot expose a route until
the existing HTTPS Gateway, reserved LoadBalancer, TLS, and application route
are all enabled. See `../admin-console/docs/HELM-RELEASE.md` for provenance,
rollout, acceptance, and rollback.

The edge NetworkPolicies are the release-owned complement to the foundation's
default-deny boundary. They bind the Envoy Gateway v1.8.3 reconciled proxy
selector and target ports 10080/10443, gateway port 8080, cert-manager solver
port 8089, and Envoy xDS port 18000. Solver ingress combines the exact
`envoy-gateway-system` namespace selector with the exact proxy pod selector,
so the cross-namespace challenge hop remains both reachable and bounded.
Exact controller artifact digests and the
reconciled artifact observations are in
`contracts/public-edge-artifact-observations.json`. Those observations are
explicitly not release authority and do not add a bespoke render-time receipt
to the lean integration values.
`scripts/verify-public-edge-artifacts.sh` reopens the pinned OCI charts and
source tags, verifies archive/manifest/commit/tree/file digests, and checks the
selectors, listener-port shift, configurable Service patch support, solver
label/port, and HTTPRoute behavior used by the release-owned policies.
The optional combined-foundation attestation and the resource-blocked Kind
lifecycle replay are tracked in `docs/fs2-serve/DEFERRED-HARDENING.md`.

When monitoring is enabled, the chart also supplies bounded Prometheus alerts
for missing catalog metadata/scrapes, unavailable replicas, excessive queue age
or depth, synchronous-wait saturation, and authentication failure spikes.
The public edge adds certificate Ready, overdue-renewal, and 48-hour expiry
alerts appropriate to the mandatory 160-hour IP certificate.
They use only bounded model/state/reason series and never principal, tenant,
token, prompt, response, or bearer labels.

Route promotion is live, not startup-only. One registry transaction reloads a
bounded projected Ed25519 public-key set and the canonical typed catalog,
immutable scale contracts, signed bindings, and lifecycle receipts periodically
and before dispatch. The gateway owns no second activation-contract file or
schema. Failure atomically removes the route from HTTP and MCP. API replicas have no Kubernetes identity:
durable PostgreSQL activation intents are owned by a separate HA controller
with leader election, fenced claims, projected KSA identity, and exact-name
get/patch RBAC. There is no activation HTTP endpoint or `fs2-control` Service.
The controller observes its exact Pod UID, signed KSA name/UID, and signed Lease
name/UID/holder/resourceVersion through the public Kubernetes API; PostgreSQL
accepts heartbeats, claims, mutation guards, retries, and completion only for
that current identity and its monotonic leadership fence. After refreshing the
signed route set and acquiring the named Lease, the leader
publishes a short-lived, value-suppressed digest of the exact sorted local
activation set. Gateway readiness requires that digest to match its own current
projection; an old controller generation cannot keep newly changed routes ready.
Every claimed intent also receives a distinct, monotonically increasing
per-model mutation fence. A late intent is terminalized and replayed as
`stale_model_fence`; it cannot regress durable UID, resourceVersion, generation,
template, or active state written by a newer intent.

Zero advertised/promoted routes is a valid fail-closed bootstrap: readiness is
green when PostgreSQL, schema, configuration, and supervised loops are healthy,
while model discovery is empty and every model invocation remains unavailable.
Activation and federation dependencies gate readiness only when an enabled
route actually needs them.

The immutable admin configuration ConfigMap is a Terraform baseline, not a
one-time bootstrap receipt. At startup the gateway validates it against the
catalog and appends it to PostgreSQL revision history whenever its canonical
ETag differs from the current revision, using actor `terraform-baseline`.
No external receipt is required for a normal tfvars-driven change, and an
unchanged restart is idempotent. A receipt mounted with the ConfigMap opts into
the existing reviewed plan/reconcile correlation and status-closing path;
configuration history and rollback planning are shared by both paths. A stale
or invalid optional receipt does not hold readiness down: the mounted Terraform
baseline is still adopted, while the unrelated reconciliation stays unfinished.

Source/runtime variants remain candidate-only in the canonical static catalog.
The gateway separately mounts `model-variant-promotions.json` beside the normal
serving binding and consumes it only through
`fs2_serve_catalog.variant_promotions.load_variant_gateway_catalog`. That loader
reopens the signed supply, runtime tuple, cold/warm semantic cohorts,
qualification, and independent review before intersecting the exact canonical
model, disabled normal binding, and immutable scale contract. An empty overlay
adds no route; ambiguity, drift, expiry, or a static-only candidate withdraws
the route. Variant MCP exposure remains disabled until its own reviewed
capability contract exists.

The controller holds only a session-scoped per-model advisory lock across a
Kubernetes readiness wait. Leader, claim, deadline, and demand checks use short
pre/post PostgreSQL transactions, so the controller-health row never blocks its
own heartbeat during a long cold load. Readiness is raced against Lease loss
and cancelled immediately on loss. If Kubernetes already accepted the exact
intent-bound PATCH, the next higher model fence adopts its transition annotation
and waits for readiness without issuing a second PATCH.

The `fs2-serve-control-plane` wheel contains the service's `fs2_serve`
package, its versioned SQL migrations, and the canonical models-lane
`fs2_serve_catalog` consumer package.
The latter is included directly from `../../catalog/runtime/fs2_serve_catalog` at build
time; the service never depends on a source-tree `PYTHONPATH` fallback.
The clean-wheel gate inspects both package entries and the `fs2-serve` console
entry point, installs the wheel without repository paths, and runs both the CLI
and isolated imports. The immutable image copies only the locked virtual
environment into its final stage before repeating those checks.

The migration default resolves inside the installed `fs2_serve` package (and
to the same source migration tree during development). The immutable-image
gate proves every declared migration is available from the
installed wheel. Gateway terminal accounting remains the immutable migration
`0005_terminal_accounting.sql` (SHA-256
`fedb6789a4839d42645c5ffb6905ce46525c213d81f15d9d987eacc109614197`).
Activation is the next unique migration, `0006_activation_controller.sql`
(SHA-256
`ac15d435e5fefb03da2780011e059736da803e5ded482414a5d1012ee265b022`),
and is likewise immutable; controller-health state is introduced only by
additive migration `0007_activation_controller_health.sql`. Additive
`0008_activation_fencing_identity.sql` introduces exact Kubernetes leader
identity, separate leadership/model fences, per-model fence state, and the
security-definer runtime enqueue function; it does not change any applied
`0005`-`0007` bytes. Additive `0009_maintenance_least_privilege.sql` preserves
the immutable `0001`-`0008` set, records payload-erasure state without exposing
ciphertext, and makes exactly-once terminal accounting owner-controlled.
Additive `0010_admin_access_accounting.sql` preserves `0001`-`0009`, adds
tenant-scoped operator principals and opaque sessions, key lifecycle metadata,
and bounded runtime-reported token/modality accounting.
Only
`fs2-serve migrate` executes
DDL; it connects with the dedicated migration role without loading any runtime
cryptographic Secret. The Helm chart runs it in a distinct migration Job and
service account. That Job alone creates the four validated, distinct
`NOLOGIN` group roles and resets their exact grants; runtime can enqueue/read
activation intents only through the bounded enqueue function and can read
controller health, while the activation credential
can fence/complete intents and publish health without token, audit, raw-payload,
result, or DDL access. The separate maintenance credential owns bounded
payload/fact retention but cannot read raw payloads or audit detail, mutate
usage/audit facts, insert audit facts, or perform DDL. Runtime can append audit
facts and invoke owner-controlled terminal accounting but cannot update/delete
audit facts, directly read/write usage facts, or delete durable ledger rows.
Serving Pods use a bounded DML-only `wait-schema` init check, and the serving
and maintenance processes retain no DDL credential. This package preserves the
versioned activation tables, typed store boundary, and fail-closed controller
heartbeat check, but the gateway image and chart intentionally ship no
activation controller, Kubernetes writer, projected service-account token, or
activation credential. Those executable surfaces belong to the separately
reviewed activation child and are merged only by the later integration lane.

`fs2-serve postgresql-release-contract` emits the non-secret cross-lane receipt
inputs. The committed contract under `contracts/` hash-binds the exact ordered
set through additive `0020_scientific_atomic_admission.sql` (including
the immutable activation lineage, additive maintenance boundary, admin
configuration, ModelDeployment ledger, scientific artifact/controller state,
append-only GPU lifecycle accounting, deployment-bound scientific access, and
the recoverable scientific-admission outbox), the `fs2-data`
database-resource versus `fs2-system` credential-Secret namespace split, and
the sole migration/group-role owner. Migration and schema-wait paths reject
missing, extra, reordered, or changed source and applied-ledger entries.
Migrations `0014` through `0020` are the integrated additive scientific
artifact, controller, state-compatibility, lifecycle, and deployment-access
lineage; their recorded bytes must be preserved by later rollouts.
The lifecycle schema, exact correlation model, reconciliation tolerance, safe
application spans, and operator projections are documented in
[`docs/workload-lifecycle-telemetry.md`](docs/workload-lifecycle-telemetry.md).

The image uses
digest-pinned Python and `uv` stages. Runtime dependencies come only from
`uv sync --frozen`; the application is installed as a wheel without dependency
resolution, and PEP 517 dependencies are constrained with hashes exported from
the same `uv.lock`. No distribution-wide package upgrade runs.

Repository-root builds use `Dockerfile.dockerignore`, not the adjacent generic
ignore convention. The policy starts closed and admits only the control-plane
package, migrations, contracts, and canonical consumer package. The build
wrapper archives an exact Git commit, materializes BuildKit's filtered context,
and proves it equals the committed allowlist before building. Untracked files,
tests, evidence, credentials, and Secret material therefore cannot enter the
context.

Use the exact-source wrapper from a committed tree:

```bash
python3 k8s-inference/components/control-plane/scripts/build_image.py verify-context --ref HEAD
python3 k8s-inference/components/control-plane/scripts/build_image.py build \
  --ref HEAD \
  --builder ATTESTATION_CAPABLE_BUILDX_BUILDER \
  --image fs2-serve-control-plane:exact-source \
  --provenance-file /tmp/fs2-serve-control-plane.provenance.json \
  --oci-file /tmp/fs2-serve-control-plane.oci.tar
```

The build requires an attestation-capable Buildx builder, requests max-mode
provenance, and generates an SPDX SBOM with a digest-pinned scanner. It verifies
that both in-toto attestations target the exact image manifest, imports only the
linux/amd64 runtime manifest into the local daemon, then verifies OCI labels for
the commit, tree, `uv.lock`, Dockerfile, and context-policy hashes.
The local provenance file contains no credentials and must be retained beside
the eventual immutable registry digest; registry publication remains a
separate reviewed action.

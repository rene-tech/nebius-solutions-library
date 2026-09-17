# SAI-15 edge denial-of-service remediation

Status: source candidate only. This document records the unexecuted static
implementation authored on 2026-09-17. It is not integration, deployment, live
verification, or security acceptance evidence.

## Source contract

The public HTTPS listener now has one Gateway-level `BackendTrafficPolicy`.
Attaching at the listener, rather than enumerating application routes, extends
the baseline to every same-listener route, including the landing website and
Grafana. Each observed IPv4 client address receives an independent global
bucket (`Distinct` over `0.0.0.0/0`) at 200 requests/second per route. Requests
under `/admin` also enter a separate 30 requests/minute client bucket. A client
that exhausts either of its buckets cannot consume another client's bucket.

The address contract trusts exactly one rightmost `X-Forwarded-For` hop. This
must not be promoted until integration proves that the retained external load
balancer is that hop, appends the real client address, and prevents direct
access that can forge the trusted position. If that proof fails, promotion is
blocked; do not increase the trusted-hop count or accept an untrusted header.

Global counters use Envoy Gateway's rate-limit service with an ephemeral,
network-isolated Redis store. The service has two replicas, bounded resources,
and node spread. Redis stores no customer data, credentials, request bodies, or
durable accounting. Its image is digest-pinned in source, runs without a
service-account token or Linux capabilities, and has read-only root storage.
The image still requires the normal independent vulnerability/SBOM/promotion
gate before deployment. Rate-limit backend failure is fail-open to preserve
customer availability; proxy connection caps and application token budgets
remain active.

The Envoy data plane has two replicas, rolling availability, CPU/memory
requests and limits, a one-Pod minimum PDB, and hostname topology spread. Both
listeners cap concurrent connections, connection lifetime, requests per
connection, incomplete-body time, idle time, stream lifetime, and concurrent
HTTP/2 streams. The two-hour active-stream ceiling is below the former 7,500 s
audio allowance while preserving long-running speech inference.

## Required integration and live evidence

The source regression tests were authored but deliberately not executed under
the coordinator's static-only boundary. A later reviewed integration must:

1. Validate and render the chart and foundation configuration from the exact
   accepted commit, including CRD compatibility with Envoy Gateway v1.8.3.
2. Scan and promote every introduced image digest before creating resources.
3. Record the current shared-service release/image identity and integrate all
   deployed sibling remediations before rollout.
4. Stage the foundation store and rate-limit service, then the application
   policy, retaining the previous Helm revision and manifests for rollback.
5. Prove the trusted-hop assumption with an unforgeable client-address test.
6. Saturate client A's general and admin buckets while client B continues to
   receive non-429 responses, then repeat against website, API, admin, and
   Grafana routes.
7. Show at least two Ready Envoy proxy replicas on distinct nodes, an effective
   PDB, bounded resources, two Ready rate-limit-service replicas, and accepted
   traffic policies.
8. Exercise landing/catalog, PAT/model authorization, sync/stream inference,
   MCP, admin, operations/results/artifacts/uploads, storage, queue/model
   admission, observability, and rollback. Include an active audio stream and
   confirm idle and two-hour ceilings behave as documented.

Rollback is ordered and reversible: first restore the prior application Helm
revision so no active policy depends on the global rate-limit service; then
restore the prior foundation Helm revision and Terraform plan. Keep the Redis
Deployment until the policy and generated rate-limit service are confirmed
absent. A live operator must use the recorded prior revisions and state-backed
plan, not source assumptions, and must re-run the same customer and operator
smokes after rollback.

No live resource, credential, registry, provider, database, or customer payload
was inspected or changed while authoring this candidate.

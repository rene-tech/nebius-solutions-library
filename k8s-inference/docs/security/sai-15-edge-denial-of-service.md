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

Client identity is fail-closed at source. The chart defaults to unverified,
zero trusted hops, an empty evidence digest, and no direct-access attestation.
Root Terraform, the workloads stage, and the chart each refuse a public edge
until a release owner supplies a nonzero SHA-256 of an authoritative provider
or load-balancer contract, the proven integral hop count, and proof that direct
access cannot forge the trusted XFF position. The HTTP and HTTPS
`ClientTrafficPolicy` objects then consume that single reviewed value. Source
does not assume that the current topology has one trusted hop.

Global counters use Envoy Gateway's rate-limit service and a network-isolated,
three-member Redis replication group supervised by a three-Sentinel quorum.
RLS receives the documented Sentinel URL form (logical master name followed by
three stable Sentinel endpoints), so it discovers one writable authority and
never sends writes through a Service that balances independent Redis servers.
A restarting StatefulSet member asks Ready Sentinels for the current primary;
only a no-quorum bootstrap uses ordinal zero. A two-Pod PDB, failover quorum,
strict node spread, probes, and bounded resources cover a member/update loss.
If the complete HA authority or RLS is unavailable, Envoy is fail-closed rather
than silently removing the security control.

Redis stores no customer data, credentials, request bodies, or durable
accounting. Its image is digest-pinned in source, runs without a service-account
token or Linux capabilities, and has read-only root storage. The image still
requires the normal independent vulnerability/SBOM/promotion gate before any
deployment. The six Terraform addresses (ConfigMap, StatefulSet, headless
Service, Sentinel discovery Service, PDB, and NetworkPolicy) are included by
exact name in `managed_resource_count` and exposed as a closed evidence output.

The Envoy data plane has two replicas, rolling availability, CPU/memory
requests and limits, a one-Pod minimum PDB, and hostname topology spread. Both
listeners cap concurrent connections, connection lifetime, requests per
connection, incomplete-body time, idle time, stream lifetime, and concurrent
HTTP/2 streams. The active-stream ceiling is exactly 7,500 seconds, preserving
the supported audio allowance; the 7,800-second connection lifetime gives that
stream five minutes of connection/setup headroom.

The first source candidate, `f60ba3f8bfe8818a343bb16c2eda9ab9bdff6289`
(tree `7b7903d928bf9d49ae12bf197c3ca1f0b5a6f25a`), is preserved as rejected
evidence. It omitted the Terraform count, used a fail-open standalone store,
shortened audio streams, omitted the HTTP request limit, and hard-coded an
unproven trusted hop. This document describes its additive successor only.

## Required integration and live evidence

The source regression tests were authored but deliberately not executed under
the coordinator's static-only boundary. A later reviewed integration must:

1. Validate and render the chart and foundation configuration from the exact
   accepted successor commit, including CRD compatibility with Envoy Gateway
   v1.8.3 and exact equality between the plan count and address allowlist.
2. Scan and promote every introduced image digest before creating resources.
3. Record the current shared-service release/image identity and integrate all
   deployed sibling remediations before rollout.
4. Stage the foundation store and prove one primary, two replicas, three
   agreeing Sentinels, quorum failover, and RLS recovery before enabling policy;
   retain the previous Helm revision and state-backed plan for rollback.
5. Supply and verify the authoritative LB/XFF contract digest, exact hop count,
   and direct-access exclusion; prove them with an unforgeable address test.
6. Saturate client A's general and admin buckets while client B continues to
   receive non-429 responses, then repeat against HTTP redirect/ACME, website,
   API, admin, and Grafana routes.
7. During one Redis/Sentinel member restart and one RLS rolling update, repeat
   the two-client isolation test and prove counters never split or fail open.
   During an isolated total-backend fault, prove bounded fail-closed responses.
8. Show at least two Ready Envoy proxy replicas on distinct nodes, an effective
   PDB, bounded resources, two Ready rate-limit-service replicas, three Ready
   store members on spread nodes, and accepted traffic policies.
9. Exercise landing/catalog, PAT/model authorization, sync/stream inference,
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

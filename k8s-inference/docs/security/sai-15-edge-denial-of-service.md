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
   v1.8.3 and exact equality between the plan count and address allowlist.
2. Scan and promote every introduced image digest before creating resources.
3. Record the current shared-service release/image identity and integrate all
   deployed sibling remediations before rollout.
4. Stage the foundation store and prove one primary, two replicas, three
   agreeing Sentinels, quorum failover, and RLS recovery before enabling policy;
   retain the previous Helm revision and state-backed plan for rollback.
5. Onboard the exact Platform Security evidence-signing public key by reviewed
   source commit. Produce a fresh signed receipt from independent provider/LB,
   listener, backend, SG, route-table, XFF-mutation, and direct-access captures;
   store it mode 0600 at the fixed run-root path, and store the seven reopened
   raw inputs under the fixed mode-0700 evidence directory. Prove the derived
   address cannot be forged.
6. Saturate client A's general and admin buckets while client B continues to
   receive non-429 responses, then repeat against HTTP redirect/ACME, website,
   API, admin, and Grafana routes.
7. During one Redis/Sentinel member restart and one RLS rolling update, repeat
   the two-client isolation test and prove counters never split or fail open.
   During an isolated total-backend fault, prove bounded fail-closed responses.
8. Show at least two Ready Envoy proxy replicas on distinct nodes, an effective
   PDB, bounded resources, two Ready rate-limit-service replicas, three Ready
   store members on spread nodes, and accepted traffic policies.
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

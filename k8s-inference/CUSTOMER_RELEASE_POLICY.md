# Customer-shaped release acceptance

This policy is mandatory for every customer, event, proof of concept, and
hackathon deployment of `k8s-inference`.

## Governing rule

**A capability may be described as customer-ready only after it succeeds
end-to-end through the exact path that the customer will use.**

Component tests, schema checks, direct-runtime probes, operator-key tests,
single-model canaries, and historical qualifications are useful evidence, but
they never establish customer readiness by themselves. Their result must be
reported only at their measured scope.

If any customer-visible identity or behavior changes after acceptance, the
affected customer-shaped acceptance must be rerun. This includes changes to:

- application or runtime image digests;
- Terraform, Helm, Kubernetes, controller, model, cache, or scaling settings;
- model or snapshot revisions;
- tenant, principal, key grants, quotas, concurrency, or budgets;
- MCP tool names, schemas, envelopes, protocol adapters, skills, or client
  configuration;
- public endpoint, TLS, authentication, request routing, queues, or storage;
- hot/cold state, replica count, node pool, accelerator, or workload shape.

An untested difference is not a caveat on a release. It blocks the broader
customer-ready claim until it is tested or the unsupported behavior is removed
from the customer contract.

## Required release gate

The release owner must preserve a machine-readable receipt proving all of the
following against the exact candidate release:

1. **Customer identity:** use a disposable canary principal in the real customer
   tenant with the same grants, scopes, concurrency and budget policy. An
   internal operator or administrator key is not equivalent.
2. **Customer client:** exercise every supported customer client and integration
   that is part of the handoff, including the actual MCP client, installed
   skills, configuration and tool refresh behavior. A raw MCP client alone does
   not qualify LibreChat or an agent integration.
3. **Customer invocation:** call the actual public endpoint and actual tool or
   API route the client selects. Test named tools and every advertised generic
   or compatibility fallback. Gateway controls must never reach a model payload.
4. **Durable outcome:** follow asynchronous work to a terminal state and validate
   the semantic result or output artifact. HTTP or MCP acceptance alone is not a
   successful inference.
5. **Workload shape:** reproduce the promised hot/cold state, concurrency,
   queueing, batch size, autoscaling and retry behavior. Sequential tests do not
   qualify concurrent service; a prepared node does not qualify a new-node path.
6. **Failure behavior:** prove idempotent replay, bounded retries, actionable
   errors, and continued availability while representative failures occur.
7. **Observability:** correlate the customer request, operation/run, attempts,
   model, tenant, principal, Pod, node, accelerator and terminal outcome in the
   operator surfaces used for support.
8. **Usage truth:** reconcile customer-visible usage with the lifecycle ledger
   for the acceptance cohort. Reservations, worst-case budgets and retries must
   not be presented as measured billable consumption.
9. **Clean cohort:** complete at least two consecutive unchanged-release cohorts
   with no unexpected terminal failures, unexplained warnings, leaked resources,
   or manual recovery. A known unexplained runtime warning fails the gate.
10. **Final identity:** record source revision, image digests, configuration
    digests, tenant policy, client build, operation IDs and timestamps. Evidence
    from a runtime-equivalent or earlier image cannot qualify the final digest.

Secrets and customer payloads remain outside Git. Receipts must contain stable
identities, hashes, status and timings without credentials or private inputs.

## Claim discipline

- `unit-tested`, `schema-validated`, `runtime-probed`, `model-qualified`, and
  `customer-ready` are distinct states.
- A release report must name failed, skipped and untested paths. Skipped work
  cannot be counted as passing.
- Customer-ready status is false if the customer-shaped gate is absent, stale,
  incomplete, or run against a different release identity.
- Readiness and availability are based on terminal customer operations, not only
  HTTP status, Kubernetes `Ready`, or a transient health response.
- The release owner—not an individual component task—owns the combined verdict.

## Incident-derived requirement

This rule was formalized after the September 2026 Stockholm deployment. Narrow
typed-tool and sequential snapshot checks passed, but the event exercised a
different generic MCP envelope and concurrent runtime behavior. Those component
passes did not justify the customer-ready claim. Stockholm remediation is tracked
in the Agent Task Deck under `fs2-stockholm-customer-readiness-remediation-r20260915`.

Cosmos snapshot remediation is intentionally outside that Stockholm work item and
must be handled independently.

# Customer-shaped release acceptance

This policy is mandatory for every customer, event, proof of concept, and
hackathon deployment of `k8s-inference`.

## Durable access and availability

Customer API keys are non-expiring by default. Do not add automatic expiry,
rotation deadlines, or short-lived route-registration windows without an
explicit operator requirement. Explicit revocation and model grants remain
effective. A signing-evidence timestamp is not the service lifetime of an
operator-configured deployment: native Serverless registrations remain active
until removed or revoked, with their signatures and exact identities verified
on every reload. Controller leadership leases and signed download URLs are
separate operational mechanisms, not customer API-key lifetime policies.

Optional App registration failures must not make unrelated Apps or the public
API unready. Test both process startup and periodic reload with expired signing
evidence, a missing registration file, a malformed entry, and revoked trust.
Include a clock-advance regression and real public-API execution; a successful
catalog listing alone does not establish availability.

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

1. **Customer-shaped identity:** internal qualification uses the existing
   `system/qa` or `system/development` inference identity, with an explicitly
   recorded grants, scopes, concurrency and budget profile. Never use an actual
   customer's key, change their limits, or create a canary in their tenant just
   to run an internal test. Testing a customer's exact identity/storage binding
   needs specific owner approval; without it, report that binding as untested.
   An internal operator or administrator key is not an inference test identity.
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

## Shared-model concurrency, capacity and scaling

Every production App is a shared, multi-customer service by default. This applies
to every model family, not only speech. A single-tenant example is not a production
serving architecture. Reuse platform tenant grants, durable operation state,
idempotency, artifact authorization and usage attribution; do not introduce a
shared demo bearer or process-global customer result namespace as substitutes.

The existing release gate must include the following evidence:

- **Isolation and scheduling:** overlap distinct customer identities with
  distinguishable inputs. Prove transcript/output, decoder/cache state, artifact,
  cancellation and idempotency isolation. A lock held for an entire live session
  is not an acceptable default wrapper. Use bounded admission and a runtime-safe,
  fair scheduler or supported batching. Removing a lock without isolating state
  is unsafe. A genuinely single-execution runtime must declare that measured
  constraint and prove shared service through adequate replicas; it must not be
  advertised as concurrent sessions per GPU.
- **Measured capacity envelope:** pin model and runtime revisions, image,
  GPU class/count (and partition), precision, settings, input/output shape,
  session duration, streaming chunk/context profile and customer/workload mix.
  Define the throughput/latency/error/quality objectives before the run. Measure
  sustained load, bursts, mixed interactive/batch traffic and overload/recovery
  through customer routes. For live audio, pace input in real time; file decode
  speed alone is not live-session capacity. Report latency distributions,
  queue growth, completion/error counts, quality, real-time factor and resource
  use with sample counts, duration and repeatability. State the highest qualified
  concurrency and the first failing or untested bound, not an unqualified exact
  universal maximum. GPU-group results must not be divided into a per-GPU promise
  without validating that topology. Other models use their native work units
  (requests, tokens, frames or jobs) and representative input sizes.
- **Saturation and headroom:** expose active work, bounded pending work, oldest
  wait, admission/rejection, cancellation, completion and runtime pressure in the
  existing operator telemetry. Streaming services additionally expose buffered
  audio/chunks and processing/transcript lag. Derive headroom from the measured
  envelope, not GPU utilization alone or a configured request limit. Missing,
  stale or unsupported telemetry is unknown, never zero capacity or zero load.
  Keep metric labels bounded; correlate opaque customer/request identities in
  authorized logs/traces without logging audio, transcripts or credentials.
- **Scaling and failure:** bind admission limits and scale-out thresholds to the
  measured envelope, observed warm-up time and chosen reserve. Record hysteresis,
  replica ceilings and capacity-unavailable behavior. Prove scale-out under load,
  fair batch/live service, cancellation/disconnect cleanup, graceful session drain
  on scale-in and explicit worker-loss errors. Verify the new-node path separately
  when advertised; replica creation does not prove GPU node provisioning. Retest
  the actual deployment substrate: Kubernetes evidence does not qualify a
  Serverless Endpoint's autoscaling. Keep FS2/KEDA/Kueue and infrastructure
  ownership intact, rather than adding a competing scaling writer.

Use `docs/QUEUE_AND_GPU_TELEMETRY.md` and
`components/admin-console/docs/CAPACITY-OBSERVABILITY-CONTRACT.md` as the existing
telemetry contracts. Store the capacity profile and thresholds with the exact
qualified release, expose their freshness and limitations to operators, and
rerun affected measurements after model, adapter, GPU, precision or workload
changes. Throughput-priority service may accept more latency, but must still
declare bounded buffering/timeouts and show sustainable queues and recovery.

No customer-ready production claim passes without this evidence. Existing Apps
with missing measurements remain explicitly unqualified for their untested
capacity/scaling claims; this policy does not retroactively qualify them.

## Claim discipline

- `unit-tested`, `schema-validated`, `runtime-probed`, `model-qualified`, and
  `customer-ready` are distinct states.
- A bounded smoke fixture qualifies only the exact operation, input shape,
  output shape, client path, and workload state that it exercised. It must
  never be summarized as the App, model, integration, or platform "working."
- Before saying `working`, `ready`, or `done`, the release owner must enumerate
  every customer-requested and customer-advertised workflow and attach current
  end-to-end evidence for each one. Any missing workflow makes the combined
  verdict `not ready`.
- If the intended customer workflow or acceptable reduced scope is ambiguous,
  the release owner must ask the user before making a readiness claim. A narrow
  implementation must be named as narrow and cannot silently redefine the
  requested outcome.
- Upstream model capability and platform-exposed capability are separate. A
  platform App cannot inherit a capability claim from a model card unless the
  deployed API/MCP contract, artifact transport, runtime, terminal result, and
  customer client have all passed together.
- A release report must name failed, skipped and untested paths. Skipped work
  cannot be counted as passing.
- Customer-ready status is false if the customer-shaped gate is absent, stale,
  incomplete, or run against a different release identity.
- Readiness and availability are based on terminal customer operations, not only
  HTTP status, Kubernetes `Ready`, or a transient health response.
- The release owner—not an individual component task—owns the combined verdict.

## Incident-derived requirement

Event-specific preparation, archive and identity-retirement steps are in
[the event closeout runbook](docs/event-closeout.md). Export outcomes before
retention expires; do not confuse transport GETs with model requests, loaded
alert rules with delivered notifications, or event closure with full qualification.

This rule was formalized after the September 2026 Stockholm deployment. Narrow
typed-tool and sequential snapshot checks passed, but the event exercised a
different generic MCP envelope and concurrent runtime behavior. Those component
passes did not justify the customer-ready claim. Stockholm remediation is tracked
in the Agent Task Deck under `fs2-stockholm-customer-readiness-remediation-r20260915`.

Cosmos snapshot remediation is intentionally outside that Stockholm work item and
must be handled independently.

The rule was reinforced after the September 2026 Cosmos3-Nano robotics handoff.
The exact pinned model/runtime supported image-to-video, video-to-video,
transfer, and robotics action modes, but the deployed public adapter exposed
only bounded text-to-image and text-to-video acceptance paths. A successful
text-to-video fixture was therefore not evidence that the Cosmos App satisfied
the advertised robotics workflows. Remediation is tracked under
`fs2-cosmos3-customer-workflows-remediation-r20260915`.

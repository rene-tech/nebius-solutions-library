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

## Release source and image provenance

Acceptance evidence is only meaningful for a release whose source and images
are reproducible. Every mutating rollout — a full `inference-stack apply` and
equally a Helm-only application digest bump — must additionally satisfy the
provenance gate (see `security/image-provenance/README.md`):

1. **Anchored source:** every receipted release carries a `release/*` or
   `deploy/*` tag backed by a hash-recorded, restore-tested `git bundle` in
   the private run root (`inference-stack anchor-release`) — mandatory even
   when the commit is also pushed to `origin/main`/`origin/release/*`. The
   deploy-source gate additionally requires a clean checkout (tracked and
   untracked drift both fail) reachable from an anchored or remote release
   ref; the wrapper enforces it on `apply` and offers a standalone
   `release-gate` command for Helm-only upgrades. Overrides require
   `--allow-unreleased-source` with a sanitized reason bound to a non-secret
   tracking identifier plus an approver from the reviewed, scoped, expiring
   `security/image-provenance/release-approvers.json` allow-list (empty by
   default: exceptions are impossible until the owner designates approvers).
   All gate evaluations and exceptions are recorded in a hash-chained,
   checkpointed, tamper-evident history (`release-source-history.jsonl`);
   anchors are annotated-tag-only, content-addressed, and published without
   replacement; receipts are write-once, published through no-replace
   primitives (never a directory rename, which could replace an injected
   empty target), and fully revalidated on every load, with bundle git
   operations bound to private snapshots of the hash-verified bytes.
2. **Bound release receipt:** before signing, every digest gets a cosign-signed
   release receipt binding it to its source commit/tree, the durable anchor
   bundle, and validated SBOM evidence
   (`security/image-provenance/provenance.py receipt`). Signing and
   allow-listing refuse digests without one.
3. **Signed digests:** every published platform image digest is cosign-signed
   with the operator release key before it is deployed; a signature without a
   bound receipt is artifact presence, not provenance.
4. **Admission allow-list:** the allow-list renders only from a signed, fresh,
   complete release inventory enumerating live workloads, the Helm rollback
   window, and frozen scientific-stage bindings with their observation
   snapshots, bound to the committed owner-approved release scope
   (`security/image-provenance/release-scope.json` — ships EMPTY, so
   rendering fails closed until the owner ratifies the exact cluster,
   namespaces, prefixes, principals, and key identity; the signed inventory
   and every CLI argument must equal it exactly). Scope and exception-approver
   authority is OWNER-SIGNED (detached cosign signatures verified against the
   release key over the exact bytes parsed, with the key fingerprint pinned
   in reviewed source; no Git ref is consulted, and branch anchors in the
   release gate only count when `git ls-remote` confirms their exact tip
   against the real remote) — dirty, locally-committed, or substituted
   authority files and keys fail closed. Deploy principals are automation
   ServiceAccounts only (owner decision: short-lived automation-only release
   identity), disjoint from the security principals that operate the
   security-owned `fs2-provenance-guard` protecting the parameter ConfigMaps
   (admission-configuration objects are architecturally exempt from
   in-cluster admission, so their non-removability is the external
   provider/IAM owner control; the renderer detects and refuses drift or
   deletion of every policy object at render), with a reversible,
   owner-signed break-glass (never deletion). MindEval passes these identical gates with no
   exception. The inventory carries a strictly monotonic generation checked
   against a SIGNED, hash-chained, no-replace acceptance chain (no replay of
   older signed inventories), and rendering authoritatively re-observes the
   cluster through the authenticated API — Pods AND workload controllers,
   the Helm rollback window per revision, frozen bindings re-fetched from
   their exact signed resources, cluster identity, and resource identities
   all compared — so an active image can never be omitted, drained, or
   claimed from forged resources, and the LIVE admission policy objects must
   equal the committed, scope-pinned definitions. An active-live image can
   never be drained out — it must be receipted and signed, or the admission
   policy's match scope must be changed by owner decision; drains apply only
   to audited non-live entries. Extras, missing entries, stale or
   future-dated inventories (finite bounded freshness only), and unreceipted
   digests abort rendering; every receipt is re-proven against the registry
   and its retained content-addressed SBOM evidence at render time. The
   `fs2-image-provenance` ValidatingAdmissionPolicy then refuses unpinned,
   foreign-registry, and non-allow-listed platform images in the platform
   namespaces — including ephemeral containers injected via `kubectl debug`
   (pods/ephemeralcontainers is matched; debugging stays available with
   digest-pinned images from allow-listed registries).

A release deployed from an unanchored or unsigned identity is not
customer-ready regardless of its test results.

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

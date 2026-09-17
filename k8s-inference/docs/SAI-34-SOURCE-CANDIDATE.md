# SAI-34 static source candidate

This commit is an additive source candidate for SAI-34. It is not an
integration, deployment, live-verification, or final-acceptance claim. The
coordinator boundary in force during authoring prohibited test execution,
builds, rendering, formatters, package managers, scanners, cloud access, and
live probes.

Source parent: `83bcb2d6c7f4dc112e414e00596e0d6b03e22712` (tree
`84f89363a90062432aaa04e23aaf66f69986acfb`). The candidate deliberately does
not merge sibling remediation branches; an integration owner must review and
combine them in dependency order.

## Source changes

- A Gateway-scoped Envoy Gateway `SecurityPolicy` denies `TRACE` before any
  chart-owned or separately attached public route can dispatch it. FastAPI also
  rejects `TRACE` with an empty, non-cacheable 405 response as defense in depth.
- Newly issued, rotated, and newly provisioned PATs use a domain-separated
  HMAC-SHA-256 operator fingerprint under the existing rotatable PAT pepper.
  Existing unsalted fingerprints remain accepted for non-disruptive bootstrap
  reconciliation and are atomically re-keyed with the digest when the PAT
  pepper rotates; this source-only change performs no bulk rewrite.
- The existing application authentication on legacy `/admin/v1/*` and
  `/internal/ext-authz` is preserved. The public-chart regression contract
  continues to forbid routing either path.
- The reviewed `user_storage_cloud_tenant_id` setting is absent from this
  branch. A source regression prevents the unused setting from being
  reintroduced when customer-storage work is integrated.

## Authored verification (not executed)

- `test_sai34_security.py` checks domain-separated keyed fingerprints,
  rotation, compatibility with an existing legacy bootstrap fingerprint,
  application-layer TRACE rejection without header reflection, and absence of
  the unused storage setting.
- `test_helm_chart.py` checks that the rendered policy targets the complete
  public Gateway, denies only `TRACE`, defaults all other methods to allow, and
  keeps the two legacy internal paths out of every public `HTTPRoute` match.

No test or renderer was executed while preparing this commit. These checks are
requirements for an authorized integration worker, not evidence that the
candidate passes.

## Findings that remain integration or operational gates

- DCGM exporter `CrashLoopBackOff` is runtime evidence, not safely diagnosable
  from source alone. Integration must inspect the then-current pod events/logs,
  preserve provider ownership, and require one Ready exporter per admitted GPU
  node plus non-empty attributed `DCGM_FI_*` series before closing the gap.
- The Kueue maintenance-Job admission race remains coupled to SAI-02. This
  candidate does not duplicate or bypass that remediation.
- Scientific-fleet execution-identity drift and the CPU/local-queue contract
  failures require independent reconciliation against the accepted runtime;
  receipt identities must not be edited merely to make tests green.
- The public-export test already rejects historical developer paths, private
  repository layout names, and provider resource identifiers in committed
  evidence. Those records were not deleted, suppressed, or rewritten under the
  no-delete boundary. Publication remains blocked until an owner-approved
  provenance-preserving export or redaction process passes that existing test.

## Integration, rollout, and rollback boundary

An authorized integration worker must merge current accepted remediation work,
run the focused control-plane and chart tests plus the repository export gate,
and independently validate the exact Envoy Gateway CRD contract. Before a
shared rollout, record the active Helm revision and image digests and prove the
candidate contains the deployed source. Live negative evidence must show
`TRACE` denied without reflection on the landing, API/MCP, and admin paths;
positive smoke tests must preserve landing/catalog, lead intake, PAT/model
authorization, sync and stream inference, MCP, admin sessions, operations,
artifacts/uploads, storage, queue admission, request debugging, and
observability.

Rollback is a normal revert of the candidate on the reconciled integration
branch followed by restoration of the recorded prior Helm revision and image
digests. Do not delete shared resources or historical evidence as rollback.

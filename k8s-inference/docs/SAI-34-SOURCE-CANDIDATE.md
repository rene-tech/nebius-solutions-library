# SAI-34 static source candidate

This commit is an additive source candidate for SAI-34. It is not an
integration, deployment, live-verification, or final-acceptance claim. The
coordinator boundary in force during authoring prohibited test execution,
builds, rendering, formatters, package managers, scanners, cloud access, and
live probes.

The first candidate `0cb3afabfceda0a445efb0f02d80bd880347315c`, merged with
local `origin/main` at `a747048cdc5e4d93a28deec237e5431688aa6764` / tree
`c9f7ea0ec2b8c6a71404f5f6c8f22eb97a96d71e`, was independently rejected. Its
five findings are preserved in the Task Deck review record. This successor is
a direct additive correction of that rejected tip; it does not claim to
integrate sibling remediation branches.

## Source changes

- A Gateway-scoped Envoy Gateway `SecurityPolicy` denies `TRACE` before any
  chart-owned or separately attached public route can dispatch it. FastAPI also
  rejects `TRACE` with an empty, non-cacheable 405 response as defense in depth.
- Newly issued, rotated, and provisioned PATs use the stable, versioned,
  truncated `fp:v2:sha256-128:<32-hex>` operator identifier. It is independent
  of the PAT pepper. Expand-only migration `0030` moves persisted legacy full
  hashes into a separate versioned column and nulls the old field, allowing old
  pods to continue during a mixed-version rollout. A database trigger converts
  legacy-column writes from old pods before persistence or `RETURNING`. The
  serializer never returns a legacy full hash, and any successful PAT proof also
  migrates a legacy/null row without waiting for pepper rotation.
- Pepper rotation and fingerprint migration use exactly one atomic store
  primitive. The redundant second rehash transaction and immediately
  overwritten raw-SHA assignments from the rejected candidate are absent.
- The existing application authentication on legacy `/admin/v1/*` and
  `/internal/ext-authz` is preserved. The public-chart contract now evaluates
  Gateway path-match semantics, the actual control-plane backend, and rule or
  backend `URLRewrite` filters instead of comparing literal match strings.
- The Gateway TRACE denial documents the Envoy inheritance contract. Every
  HTTPRoute-targeted `SecurityPolicy` must use `mergeType: StrategicMerge`; a
  repository-wide Terraform regression rejects a more-specific child policy
  that would replace the parent. The SAI-26 candidate must satisfy this test
  before integration.
- `public_export.py` creates a non-overwriting, commit-bound redacted projection
  and records source/export hashes plus rule counts for every file. Historical
  evidence remains byte-identical in the canonical tree.
- The reviewed `user_storage_cloud_tenant_id` setting is absent from this
  branch. A source regression prevents the unused setting from being
  reintroduced when customer-storage work is integrated.

## Authored verification (not executed)

- `test_sai34_security.py` checks the versioned/truncated format, stable
  correlation across pepper rotation, pre-proof response masking, same-pepper
  lazy migration, one atomic verifier update, TRACE rejection without header
  reflection, and absence of the unused storage setting.
- `test_helm_chart.py` checks the parent TRACE policy, child merge contract, and
  structural internal-path non-exposure including rewrites and backend target.
- `test_security_policy_composition.py` scans every checked-in Terraform and
  YAML child `SecurityPolicy` and includes rejecting/accepting adversarial
  fixtures for the merge contract.
- `test_public_export.py` validates the projected tree and proves a known
  historical evidence file is redacted while its source bytes and both hashes
  remain available as provenance.

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
- Public publication remains an integration gate until an independent worker
  executes the projection test and validates an exact generated manifest.
  Canonical evidence was not deleted, suppressed, or rewritten.

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

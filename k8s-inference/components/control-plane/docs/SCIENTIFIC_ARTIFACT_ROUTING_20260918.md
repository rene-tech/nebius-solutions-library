# Scientific artifact continuity during gateway rollout

## Observed failure

Qualification operation `35f22396-c56f-46be-ab9b-17b4d7c990dc`
(Proteina-Complexa) completed generation, filtering and evaluation, but its
analysis-stage input download exhausted connection retries at approximately
19:02 UTC on 18 September 2026. This overlapped Helm revision153's gateway
rollout. The stage failed before analysis began; it was not a model-quality or
GPU-capacity failure. The original failed operation is retained.

The internal artifact Service publishes not-ready addresses intentionally:
already-admitted jobs must still reach the HTTP artifact API when worker health
temporarily makes every gateway unready. Using that Service as the primary
route also exposes normal artifact requests to starting/terminating gateways.

## Repair

- New stages use the normal readiness-gated gateway Service first.
- Artifact companions can use the existing artifact Service as a fallback on
  alternate attempts within the existing bounded transient-error retry budget.
- Both routes reach the same authenticated artifact API. Authorization failures
  do not trigger a retry. No credentials, permissions or admission limits change.
- Signed object-storage URLs are never rewritten; their independent transport
  and byte/hash checks are preserved.
- The fallback remains optional for older manifests and standalone clients.
  Cosmos LeRobot's application-specific HTTP client receives the new primary
  route but does not acquire companion fallback behavior through this change.
- No retry budget, timeout, GPU count, model policy or customer quota increases.

## Verification before deployment

Focused settings, CLI, manifest renderer and routing tests:52passed.
Companion/staged-workspace/chart suite:173passed, one pre-existing failure in
`test_committed_scientific_profile_binds_exact_helm_execution_map_bytes`.
The same assertion was reproduced using the untouched committed chart and
unchanged catalog files. Do not rewrite qualification hashes to conceal it.
The current execution-map catalog has explicit qualification baselines, while
that older assertion compares every profile directly to the complete map hash.

Regression tests cover GET/POST connection errors and503responses, readiness
fallback propagation into materializers/collectors, unchanged signed-object
URLs and immediate authorization-error propagation. Ruff passes.

Deployment and live re-execution receipts belong to the active qualification
campaign. Unit tests alone do not establish uninterrupted customer delivery.

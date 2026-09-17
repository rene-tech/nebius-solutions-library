# SAI-23 Cosmos media reference remediation — additive r03 candidate

Status: **SOURCE CANDIDATE ONLY — INTEGRATION/LIVE NO-GO**

This additive successor starts from independently rejected commit
`918b09e0897ad26f8127398ec324d31168277dbe` (tree
`c54427f5473f27b8dd0930f4299ca26515470d46`). The rejected commit and its
review findings remain preserved in history. No test, build, formatter, linter,
package manager, scanner, CI, container, Terraform, Helm, cloud, cluster,
database, registry, credential, deployment, cleanup, or live command was run
while authoring this correction.

## Corrections in this successor

- The embedded text-to-image DTO now accepts the contract's explicit
  `output_delivery: inline-base64` selection while continuing to reject every
  other delivery value. Its existing strict `extra=forbid` behavior remains.
- Reference-less edge and blur controls now emit an upstream configuration
  object whenever a threshold/strength preset is supplied, including at the
  default control weight of `1.0`. The preset is no longer lost as a bare
  boolean control value.
- The artifact materializer translates tenant-scoped not-found, policy,
  verification, conflict, stale-attempt and content-size resolution failures
  into the existing non-retryable `ArtifactInputError`. The operation worker
  therefore records a terminal `artifact_input_invalid` outcome on the first
  attempt and retains the durable row. Storage-unavailable failures remain
  outside this permanent-error set so existing transient retry behavior is not
  collapsed.
- The dispatch gate validates canonical base64 and computes decoded length as
  `(encoded_length / 4 * 3) - padding`. Exact-limit values ending in `=` or
  `==` are no longer over-counted; malformed/interior padding and decoded
  values above the limit remain rejected before runtime invocation.

## Preserved boundaries

The correction does not broaden the accepted media transport. Admission and
dispatch still reject raw URL, filesystem path and caller-provided data URL
values. Only tenant-owned platform artifact references may be admitted, and
only control-plane-materialized data URLs may cross the runtime boundary. The
32 MiB aggregate pre-stream budget, mode-specific MIME constraints, runtime
DTO/content validation, signed output identity checks, and adapter `finally`
cleanup remain unchanged.

Landing, catalog, PAT/model authorization, sync/stream inference, MCP, admin,
operations/results/artifacts/uploads, storage, queue/model admission,
observability, caches and customer request-debugging source were not changed.

## Authored regression coverage (not executed)

- Explicit text-to-image `inline-base64` delivery parses in both the published
  JSON Schema and embedded runtime DTO; unsupported artifact delivery fails.
- Reference-less edge and blur presets with default weight survive into the
  upstream `extra_params` object.
- A tenant-scoped missing artifact ends as `artifact_input_invalid` on attempt
  one, invokes no runtime, and leaves the operation row present.
- A padded base64 control whose decoded bytes exactly equal the 4 MiB limit is
  accepted by the final runtime budget gate.

These tests are source only and have not been executed under the coordinator's
static-only boundary.

## Remaining program gates

This source candidate is not independently releasable. Integration still
depends on SAI-03 model-pod egress containment, SAI-18 principal-level artifact
isolation and SAI-19 independent digest verification. A later authorized
integration lane must also prove mixed-version rollout and queue drain,
four-worker control-plane peak memory, all qualified media modes and content
paths, bounded temporary-file behavior, and rollback. No GO, integration,
deployment or live-verification claim is made here.

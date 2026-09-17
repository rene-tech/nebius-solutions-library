# SAI-27 model-existence oracle remediation

Status: source-only candidate for independent review. This document is not an
integration, deployment, or live-acceptance claim.

## Source contract

The candidate starts from commit
`83bcb2d6c7f4dc112e414e00596e0d6b03e22712` (tree
`84f89363a90062432aaa04e23aaf66f69986acfb`). Public OpenAI-compatible and
native HTTP inference routes now perform model-grant and dynamic-publication
authorization against an enabled-or-disabled registry entry before exposing
route readiness. A model-policy denial is converted to the same `KeyError`
boundary already used for unknown models.

The resulting policy-denied response is exactly:

```json
{"error":{"type":"not_found","message":"model or operation was not found"}}
```

with HTTP 404 and `Cache-Control: no-store`. An authorized caller continues to
receive the existing readiness response, including HTTP 503 for an unavailable
route. Authentication dependencies still run before the route handler. PAT
scope enforcement, admin/operator errors, MCP tool errors, model admission,
and runtime dispatch remain unchanged.

## Regression contract

`test_public_http_model_policy_denials_are_identical_to_unknown_models` covers
chat completions, completions, embeddings, image generation, and native model
invocation. For every route, it compares an existing private App that is in the
PAT's model grant but outside the principal allowlist with an unknown private
App name and requires identical status, bytes, JSON, and cache policy. The
existing customer-ownership test now also requires HTTP 404 for a model-grant
denial while retaining its authorized dispatch, missing-scope,
disabled-principal, result-isolation, and usage checks.

The tests were authored but not executed because the parent coordinator limited
this ticket to static additive source work. No formatter, linter, test, build,
package manager, scanner, Terraform, Helm, container, browser, cloud, cluster,
database, registry, credential, or live command was run.

## Deployment and rollback

No resource was inspected, created, changed, or removed, and no deployment was
attempted. Integration must first reconcile this source candidate with the
current shared-service source and independently run the focused and control-plane
suites. If later rollout verification fails, restore the prior control-plane
image digest or Helm revision recorded by that release and revert this isolated
source commit through the normal reviewed integration workflow.

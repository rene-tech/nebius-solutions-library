# SAI-27 model-existence oracle remediation

Status: source-only candidate for independent review. This document is not an
integration, deployment, or live-acceptance claim.

## Preserved rejection and successor correction

Independent review rejected exact commit
`b596a3502bd32bd2e6d2c344389877d4469d2157` / tree
`7935cec8167793ed3ebf76f3b781fd27f2b4d735` as SOURCE, INTEGRATION, and LIVE
NO-GO. That candidate checked model policy before the required public inference
scope, so a catalog-only key could still distinguish an existing granted model's
403 from an unknown model's 404. Its check was also preliminary: a route refresh
could tighten policy before authoritative admission and expose the final
`PermissionError` as 403.

The successor preserves the ordinary 403 missing-scope behavior by requiring
`inference.invoke` before any registry lookup on public HTTP inference routes.
It also converts only the final post-refresh principal/model authorization
denial to a dedicated `KeyError` subclass. Model-required-scope, operation,
protocol, MCP, admin/operator, and authorized readiness errors retain their
existing behavior. Final admission resolves enabled-or-disabled state, applies
principal/model authorization, and only then exposes stale or unavailable route
status, preserving 503 for authorized callers without reopening the oracle. The
branch merges current `origin/main` at
`0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697`; rejected history is preserved
without rebase or amendment.

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

`test_missing_inference_scope_does_not_disclose_model_existence` requires the
same 403 body for existing and unknown names before registry access.
`test_post_refresh_policy_tightening_is_identical_to_an_unknown_model` changes
the private allowlist inside `route_refresh` and requires the authoritative
admission denial to remain byte-identical to unknown-model 404 behavior. Both
regressions cover OpenAI chat and native HTTP invocation.

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

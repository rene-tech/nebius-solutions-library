# SAI-28 request hardening source handoff

This is a static SOURCE candidate for the 2026-09-16 SAI-28 finding. It does
not constitute integration, deployment, live, or final-acceptance evidence.
The corrected implementation commit is
`4d9ca55e21b3665f49bc70b680bf7e9322544e18` (tree
`c6a8485ef513578aaaeec7579dc126ac49ba2e21`).

## Corrected source behavior

- Bearer verification completes before Scientific Apps refresh. Successful
  refreshes are cached for a bounded five seconds per process, concurrent cache
  misses are singleflight, and refresh failures invalidate freshness and fail
  callers closed with a generic availability error. A one-second retry window
  prevents a failed repository from being hammered. The last complete snapshot
  is retained only for operator diagnosis; a failed `refresh()` never returns
  it as current policy.
- App create/update and scientific workload rendering use forced refreshes.
  This bounds cross-replica policy staleness on ordinary request paths without
  allowing a stale app-to-runtime mapping at mutation or privileged execution
  boundaries. Refresh failures are logged internally with a fixed event and
  exception class only, while public responses remain generic.
- The existing pure-ASGI request-size boundary now scans JSON bytes
  incrementally across chunks and rejects more than 64 nested containers before
  FastAPI/Pydantic or manual route parsing. Strings and cross-chunk escapes are
  tracked so structural characters in string values do not affect depth. The
  boundary applies to `application/json` and structured `+json` media types,
  including native `NativeInvocation` request bodies.
- Only `AdmissionInputError`, raised at the unsupported-protocol and canonical
  OpenAI JSON branches, maps to HTTP 400. Route refresh, registry/configuration,
  store, cryptographic, and invariant `ValueError`/`RecursionError` failures are
  no longer caught by `invoke()` as client mistakes.
- A streamed request that crosses the configured byte ceiling is converted by
  the ASGI boundary into the same structured 413 response as a declared
  `Content-Length` overflow.

The preserved candidate `8b53c35188fa169f218105950df272fa0ab0fc55`
(tree `301f9addd61bea3146bf77d67fbf0ca5d932d61d`) was independently rejected:
it refreshed twice per valid request, blanket-mapped admission exceptions to
400, and covered only manually decoded JSON. It remains negative evidence and
must not be promoted.

## Authored regression coverage

`tests/test_sai28_request_hardening.py` now contains source coverage for:

- authentication-before-refresh ordering;
- five-second cache reuse, forced refresh, concurrent singleflight, atomic
  snapshot retention, retry suppression, and fail-closed refresh errors;
- typed protocol/canonical-JSON client errors and propagation of route-refresh
  and untyped admission server faults;
- excessive depth through both an OpenAI route and an actual FastAPI native
  Pydantic route; and
- streamed overflow both at the pure-ASGI boundary and through the complete
  FastAPI middleware stack.

These tests were authored but not executed. When the coordinator lifts the
static-only boundary, an independent verifier should run the focused module,
the control-plane unit/integration suite, and the repository's established
lint/security checks. A live rollout still requires separate deployed-image
provenance, sibling-feature reconciliation, rollback identity, and positive and
negative customer-flow smoke evidence.

## Main and sibling-candidate reconciliation

The task branch merged current `origin/main`
`0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697` with normal merge commit
`24cd2d25efa1d4a57615d70a42bc43b85e381623`; current main is therefore an
ancestor of the corrected source. The merge was clean and did not remove a
file.

SAI-01 and SAI-27 remain separate, unintegrated source candidates and were not
silently imported into this finding:

- SAI-01 `d4e2d79fa3f36deb64e099e8033b24778eb66127` moves request-debug model
  attribution until after authorization and adds the bounded off-path capture
  queue. Integration must retain that ordering while placing the SAI-28 typed
  `AdmissionInputError` catch around admission; it must not restore
  caller-supplied pre-authorization attribution.
- SAI-27 `ca13cc2c24c90b12e6db0b16a1ad70069f1f815c` authorizes public model
  visibility before route readiness. Integration must retain its
  `public_model_or_not_found()` calls after SAI-28 bounded parsing and before
  admission. The native depth guard remains outside framework parsing and does
  not disclose model existence.

Those exact siblings require their own independent acceptance before an
integration branch combines them. This task makes no acceptance claim for
either candidate.

## Static-only boundary and rollback

No tests, builds, linters, formatters, package managers, scanners, CI jobs,
Terraform, Helm, containers, browsers, downloads, live probes, cloud/provider
APIs, databases, registries, credentials, clusters, GPUs, or shared services
were accessed or executed for this correction. No runtime resource or state was
created, mutated, or cleaned up. Source rollback is a normal non-destructive
revert of the SAI-28 commits after preserving the rejected-candidate and review
records.

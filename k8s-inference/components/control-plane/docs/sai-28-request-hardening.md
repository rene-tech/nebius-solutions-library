# SAI-28 request hardening source handoff

This source candidate addresses the 2026-09-16 SAI-28 finding without changing
authentication policy, customer request-debugging, model grants, admission
semantics, or operator surfaces.

## Source changes

- Bearer verification now completes before the Scientific Apps inventory may
  refresh, so an unauthenticated request cannot trigger its repository reads.
- Manually decoded JSON is limited to 64 nested containers using an iterative
  walk. Invalid, excessively nested, or recursion-triggering JSON returns 400.
- Admission `ValueError` and `RecursionError` failures caused by request input
  return a generic 400 without reflecting payloads or internal error text.
- A streamed request that crosses the configured byte limit is intercepted by
  the pure ASGI size guard and receives the same structured 413 response as a
  request rejected from `Content-Length`.

Regression coverage is authored in
`tests/test_sai28_request_hardening.py` for authentication ordering, JSON depth,
admission error classification, and chunked-body overflow.

## Integration notes

The source base is commit `83bcb2d6c7f4dc112e414e00596e0d6b03e22712`
(tree `84f89363a90062432aaa04e23aaf66f69986acfb`), which exactly matched local
`main` when work started. No SAI remediation was integrated into that `main`.
The unintegrated SAI-01 candidate also changes the `invoke`, OpenAI route, and
scientific-submit neighborhoods for request-debug attribution; integration must
preserve both candidates deliberately instead of resolving either side away.

## Verification boundary

Per the parent coordinator's static/additive-source boundary, this candidate
has not been executed, built, linted, scanned, deployed, or tested against a
live service. It is a SOURCE candidate only and is not integration, deployment,
or live GO evidence. A reviewer may run the focused test module and the broader
control-plane checks only after that boundary is explicitly lifted.

No cluster, cloud, provider, database, registry, container, GPU, or shared
service was accessed, and no credential value was read or modified. No runtime
resources were created or changed, so there is no live rollback operation.
Source rollback is a normal revert of the candidate commit after preserving
review evidence.

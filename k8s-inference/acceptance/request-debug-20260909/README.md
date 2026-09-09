# Request-debug live acceptance

Live API capture verification passed on 2026-09-09 after the explicitly preserved
OpenFold2 harness correction below. Future runs still require the release owner
to verify the exact rollout and enable request-debug capture. The helper never
enables it or changes any key, user, model setting or capacity policy.

From `k8s-inference/components/control-plane`, inspect the offline plan with:

```sh
.venv/bin/python ../../acceptance/request-debug-20260909/verify.py
.venv/bin/pytest -q ../../acceptance/request-debug-20260909/test_verify.py
```

Only after explicit live authorization, substitute the retained **ordinary
internal-test** key file, existing access bundle, fresh private output directory
outside the repository, and the full verified deployed commit:

```sh
.venv/bin/python ../../acceptance/request-debug-20260909/verify.py \
  --execute \
  --key-file /private/internal-test-key.json \
  --access-bundle /private/final-stack-output.json \
  --output /private/request-debug-r01 \
  --release FULL_40_CHARACTER_DEPLOYED_COMMIT
```

The key file must be the existing `fs2-customer-key/v1` document with mode 0600;
the helper does not create or rotate it. The supplied release is the operator's
rollout attestation, not independent image inspection by this client.

The six cases are one unchanged synthetic PhenoAge prediction, two empty native
Boltz2/OpenFold2 validation failures, malformed authenticated HTTP JSON, MCP
discovery, and malformed MCP invocation arguments. Exactly three logical native
operations are intended, sequentially, with no replay. The two validation
operations must fail with retained upstream 400/422 bodies; their public result
endpoint must remain 409 (there is no successful result). Malformed requests must
have no operation ID. HTTP 200 alone does not make an MCP tool call successful.
The native operation comes from current public discovery, not archival catalog
metadata. `--cases openfold2 malformed-http mcp` selects only those remaining
cases; it does not replay or resubmit previously admitted PhenoAge/Boltz2 work.

The helper polls operation status for at most 300 seconds and capture persistence
for at most 60 seconds by default. It compares actual public request/response
bytes, content types, server request IDs, operation/attempt IDs and verified
owner/token metadata. Upstream request bytes must match the dispatched synthetic
payload; full upstream responses, including validation errors, are retained.
Metadata lists must exclude bodies, headers and query strings; expanded App and
global details must agree. Authentication headers must be redacted.

`summary.json` and `http-events.json` contain credential-free identities,
statuses, timestamps, byte counts and hashes, not payloads. Matched raw debug
documents stay under private `raw/` with mode 0600. A mismatching document is
preserved unless it contains authentication material, in which case it is not
written. No raw body or credential is printed. Exit zero means every assertion
passed; a failure is retained and never silently retried or reclassified.

On timeout or failure, accepted operation IDs remain in private receipts. The
helper does not cancel work or attempt recovery: the release owner decides any
further action. An admin session opened by this run is closed in `finally`.
Historical missing payloads cannot be reconstructed by these checks.

## Live evidence — 2026-09-09

[Credential-free summary and capture manifest](live-summary.json) bind the exact
release `88520758f90a7e171abd86a4a94787a6739d6ba7` and control-plane image
`sha256:aabc80f6ae713fa70a4850742b4fdd12ba55cd6cb3c2c50ab96d210bf2036bdb`.
Raw documents remain in the private release receipts, not this repository.

- `request-debug-r01`, 08:44:48–08:45:09 UTC: PhenoAge succeeded with exact public
  and upstream 200 captures; invalid Boltz2 failed with exact upstream 422.
  OpenFold2 then returned operationless 403 because the helper used archival
  `predict` instead of the selected runtime's advertised `predict-structure`.
  This original failure remains recorded; no authorization or product fix was made.
- `request-debug-r02`, 08:48:18–08:48:30 UTC: only the previously unexecuted cases
  ran. OpenFold2's corrected operation failed as intended with upstream 400;
  malformed HTTP returned operationless 422; MCP discovery succeeded and malformed
  invocation returned a tool error inside HTTP 200 with no operation. All exact
  public byte/content-type, owner/token, correlation and authentication-redaction
  assertions passed. Full upstream requests and error responses were retained.

Exactly three native logical operations were admitted across both runs, all on
attempt one. No inference replay, key/configuration/capacity mutation or automatic
recovery occurred. Both task-owned admin sessions closed; both verifier processes
exited. This is not an error-free first-run claim, an inference-quality benchmark
for invalid model inputs, or a qualification of unrelated typed-MCP changes.

The helper's initial/final SHA-256 and each private summary/capture hash are in the
manifest. Offline coverage: 24 verifier tests pass, including the selected-runtime
operation correction and bounded case selection; existing helper tests add 25.
Actual Chrome UI inspection also passed on the deployed admin console: the
PhenoAge public exchange displayed both complete bodies, redacted authentication
headers, tenant/principal/key and operation IDs; its JSON download matched the
selected exchange and collapsed details were removed. The Boltz2 upstream 422
displayed the complete `polymers` missing-field error with copy/download actions
and no UI alert. Only these synthetic exchanges were expanded.
The private PhenoAge JSON download SHA-256 is
`9f7587bb295c3bdc294ed07fb9ed124cfc0c3c3fa6d356a22b01bced7fcf07bd`.
The first page load hit a transient browser `ERR_NETWORK_CHANGED` while fetching
its JavaScript asset; a reload loaded the deployed UI successfully. No code fix
or change to customer/model settings was needed.

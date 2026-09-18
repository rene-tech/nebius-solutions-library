# Campaign public API failures: bounded diagnosis and repair

Status: source repair tested; exact-image deployment and subsequent public
verification belong to the release owner. This is not proof that all historical
500/503 responses share one cause or that the service is interruption-free.

## Findings kept separate

| Retained event (UTC, 18 September) | Evidence and conclusion |
| --- | --- |
| 20:40:48–49, scientist08 MolMIM MCP | Client retained `HTTPStatusError` before durable admission. The encrypted debug exchange independently shows inner HTTP200/SSE, zero response bytes, `response_body.complete=false`, semantic outcome unknown, no operation. The complete outer client reply was not saved, so its exact status/body and failure boundary cannot be reconstructed. Same-key reconciliation later admitted one operation; its explicit `generation_exhausted` is a distinct model outcome. |
| 21:09:39, gateway access logger | Retained stack ends in Starlette `BaseHTTPMiddleware.call_next` raising `RuntimeError: No response returned.` at the access logger. A deterministic test reproduces this when a mounted ASGI app returns on observed client disconnect before response headers. |
| 21:11:42 onward, scientist10 MCP | Original caller reported HTTP500 without an operation ID. Owner lookup found no matching admission among all 94 operations. No matching debug row was found in the bounded 21:11:42–48 query. Original response headers/body were not retained; the earlier access-log stack is not exact correlation. Same-key post-160 retry admitted successfully at 21:14:40; its later Cosmos GPU decode failure is separate. |
| 21:12:12, background scientific worker | SQL `TimeoutError` occurred while releasing a reconciliation lease. This is not a public request trace; no proof links it to the preceding 500. No pool, timeout, retry or lease change is made here. |
| About 21:13, scientist07/09 Runs UI | Workbench capture raised on HTTP503 before saving response headers/body; login/messages worked. The Runs bridge may propagate an upstream503 or map fetch interruption/45-second timeout to503. The retained evidence cannot distinguish those causes. The capture owner has separately repaired evidence preservation. |
| Latest scientist08 harness stop | Local evaluator import failed because NumPy was absent. This is not an API failure. Release owner resumed using the established evaluator environment and added dependency preflight; the failed receipt is retained. |

## Narrow source change

Commit `20c167d6b` replaces only the outer function-based access logger with
`AccessLogMiddleware`, a pure-ASGI observer. It removes the response-repackaging
task that manufactures `No response returned` on disconnect. It neither catches
and converts genuine application errors nor fabricates a response when none was
sent. Request bodies are not drained. Response statuses, duplicate headers,
stream chunks, cancellation and transport failures pass through unchanged.
Existing `nosniff`/`no-store` defaults remain. Existing duration-to-headers
semantics remain; missing status, incomplete response and disconnect are explicit
in the existing access log, joined to the existing request ID when available.
No credentials, payloads or exception details are added to logs.

The unchanged encrypted debug and semantic telemetry middleware still records
actual outcomes. No MCP handler, artifact bridge, admission budget, timeout,
pool, quota, runtime model or deployment setting was changed.

Verification: 87 tests passed across `test_access_logging.py`,
`test_request_debug.py`, `test_request_telemetry.py` and `test_api_mcp.py`.
Includes the old failure reproduction, actual mounted stateless MCP over real
Uvicorn, first-chunk-before-completion, partial/disconnected requests, error and
cancellation propagation, headers and unchanged debug/semantic evidence. Ruff,
`git diff --check`, and mypy of the new module passed. This is not a full CP
typecheck or a production soak.

## Read-only current verification and retained evidence

At 21:19:35–37, six ordinary owner-authenticated `GET /v1/operations?limit=1`
requests across scientist07/09/10 returned HTTP200, 372–638ms. No inference,
new keys, model changes or customer-record mutations were used. Readback found
three Ready/Available/Updated gateways on Helm160 image
`sha256:6146b058c61fca92c6b3450a1284f5397c81f3ab956c2ed6fc09c1c756f1cd22`.
These reads establish current history availability only, not the cause of old
failures or validation of the not-yet-deployed logger change.

Protected evidence root:
`/home/tux/secure-handoff/scientific-qualification-20260918/public-api-failures/`.

| Receipt | SHA-256 |
| --- | --- |
| `read-only-history-probes.json` | `407ddef9bfe3aac335fdf7e587ceba9f858069ca4b0420cbc667aec292292d8a` |
| `molmim-debug-list.json` | `ccaca6e7e4396594c6733d2b186169405f1680f8773e16b819d880dfb6c365d4` |
| `molmim-debug-detail.json` | `ec7d7307448a238a7ed5a7b466504a0f31d582581ce0bfe4fed7fd9d382c832f` |
| `scientist10-debug-list.json` | `db8cfe4d1f0671b262df83aa8bf0a53c264886eef73578058253e51ee57bbd0e` |

Original sibling `input-contract-release/gateway-rollout.log` SHA-256:
`4b2002f89dc617274d894fe1298257de0d43c46305487efc480d9b961716ca87`.
Raw debug/customer bodies remain private. Absence from a best-effort debug
capture is not proof that the edge received no request. Any uncertain admission
must still reconcile by the original idempotency key, never duplicate GPU work.

Remaining gate: parent exact-image rollout and renewed bounded public HTTP/MCP
history/disconnect checks, with complete status/request-ID/body capture if any
failure recurs. Preserve readiness503 when dependencies are genuinely unavailable;
do not mask it or inflate timeouts to make the gate appear healthy.

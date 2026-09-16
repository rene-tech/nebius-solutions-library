# Request debug logging

This is an opt-in operator debugging facility, separate from ordinary logs, usage
counters and logical run history. It is **off by default** and, when enabled, is
governed and fail closed: the request body (the debugging target) is stored redacted
within the cap, while the response body is **never stored** — it is withheld entirely,
because any part of an untrusted response (a string, a numeric value, an object key, or
a streaming/binary payload) can carry an opaque secret. A request body that exceeds the
cap is withheld rather than stored as a prefix; captures are deleted by the platform's
central retention purge; and reading a captured exchange requires an ADMIN operator and
is audited. Enable it deliberately for a bounded window rather than leaving it on as a
standing state.

## Enable capture

There is **no global capture switch**. Enabling `request_debug_enabled` without a
tenant/model scope and a bounded future expiry is rejected at startup, so capture
is always scoped and self-closing. A minimal activation names a scope and an
expiry within the maximum window:

```hcl
deployment = {
  # Preserve the rest of the existing deployment configuration.
  observability = {
    # Preserve the other existing observability settings.
    request_debug_enabled    = true
    request_debug_tenants    = "tenant-a"        # REQUIRED: a tenant scope is mandatory
    request_debug_models     = "boltz2"          # optional: narrows within the tenant scope
    request_debug_expires_at = "2026-09-20T00:00:00Z"
  }
}
```

The default is `false`. Enable it through the normal reviewed Terraform/release
workflow for a bounded window; it is not a browser setting, and it should be
disabled again once the investigation is complete. Reading a captured exchange
requires an **ADMIN** operator (tenant scoping still applies); customer API keys
do not gain access to the admin debug API.

**Enabling capture alone records nothing** — you must name what to capture and
when it stops:

- `request_debug_tenants` — comma-separated tenant IDs to capture. **A non-empty tenant
  scope is MANDATORY** so capture can never span tenants; only the named tenants are
  recorded, and unauthenticated/rejected requests (no tenant) are not. There is no
  model-only ("all tenants on a shared App") mode.
- `request_debug_models` — comma-separated model (App) IDs. This is an **optional
  narrowing WITHIN the tenant scope**: when set, a captured exchange must match both a
  named tenant AND a named model (including pre-admission rejections for that App under a
  named tenant). It cannot be used alone.
- `request_debug_expires_at` — a required RFC3339 instant after which capture stops
  even while enabled. To actually capture, set it in the future and within the capture
  ACTIVATION window `request_debug_max_window_seconds` (default and ceiling **7 days** =
  604,800s — how long capture may stay enabled). There is no unbounded or "capture
  everything" mode. This activation window is DISTINCT from the 90-day record-retention TTL
  (how long captured rows live before deletion — see "Completeness, storage and limits").

The control plane validates this at startup: an enabled policy without a tenant scope, with
no expiry at all, or with an expiry beyond the maximum window fails fast rather than
capturing broadly. A **past** expiry is deliberately allowed and is service-safe: it
is treated as capture-off (the runtime gate fails closed), so a stale expiry never
crash-loops the control plane and never widens capture. Because capture is
time-bounded, an operator normally disables it before the expiry passes; if the
expiry lapses first, capture simply stops and a later restart still succeeds with
capture off.

One more chart value bounds each retained record and is safe to leave at default:

- `config.requestDebugMaxBodyBytes` (default `65536`, max `262144`) caps the stored
  size of each captured body and the bytes the sanitizer ever processes for one body.
  A body over the cap is withheld entirely (never a boundary-cut prefix). The lower
  max also bounds sanitizer CPU/memory, which runs off the event loop.

Retention: captured exchanges have a **90-day TTL** (7,776,000 seconds). A record older
than 90 days is eligible for deletion by the platform's central maintenance purge (its own
DELETE grant and schedule); a record **within 90 days is always preserved**. This capture
facility never deletes rows. Two DISTINCT bounds apply and must not be conflated: the capture
ACTIVATION window (`request_debug_max_window_seconds`) is capped at **7 days** (capture may be
enabled for at most 7 days going forward and then stops adding rows — it deletes nothing),
while the separate 90-day RECORD-retention TTL governs how long stored rows live before the
central purge removes those older than 90 days. Before any purge runs, a **payload-free
retention preflight** (`retention_preflight`:
oldest `started_at`, total count, and the count over the 90-day cutoff — aggregates over the
clear timestamp column only, no payload read) proves how many rows are eligible; if any row
within 90 days would be affected it must not run. The purge does not execute in any live
environment without explicit rollout authorization.

Capture covers observed public `/v1/` HTTP exchanges and `/mcp` traffic that the
policy admits, including validation failures and requests rejected before an
operation exists. Token management endpoints (and the storage-credentials
endpoint) are excluded. Admin/debug endpoints are not themselves
captured. Actual dispatched model HTTP exchanges are recorded separately as
`upstream`, including individual federation HTTP attempts. This is not a recording
of every internal Python call, GPU kernel, or scientific stage's internal network
traffic.

## Inspect a request

1. Open **Apps → an App → Runs → Request log**. The log includes known-model
   rejected requests even when the logical-run table is empty. Run-state/user
   filters for the table do not filter this separate request log.
2. For a particular logical run, open its detail and find **Run request / response
   debug**. This list is filtered by the original operation ID and selected time
   window; public polls/replays and actual upstream attempts remain separate rows.
3. For requests with no App/model attribution, open **Apps → Show all request
   logs**. This collapsed global view is not filtered by App search.
4. Select **Inspect** to fetch one full retained exchange. Lists contain metadata
   only; they do not preload customer bodies. Close the row to remove its body
   query from the UI cache.

Expanded details show source, request/operation IDs, operation and upstream attempt
numbers, precise timestamps, tenant/principal/key ID, model/tool, method/endpoint,
actual HTTP status, disconnect/error details, query string, headers and bodies.
Duplicate header entries are preserved. An API key **ID** is attribution, not its
secret value. Operation attempts and upstream HTTP retries are different counters.

Bodies and error details render as literal text, never HTML. Non-UTF-8 data is
represented as base64. **Copy** copies the displayed retained representation;
**Download exchange JSON** exports that representation with its encoding and
capture flags, not an executable HTML document. Handle these exports as customer
data: keep them in approved private storage and never paste them into stdout,
Loki, Git, tickets, chat or other unapproved destinations.

An HTTP 200 on MCP does not establish tool success: the response body is not stored, so
read the retained **`mcp_is_error`** boolean and the fixed **`mcp_failure_category`** enum
(with a bucketed **`mcp_error_code`**) — all set server-authoritatively from the dispatch
result, never from the withheld body — to distinguish the failure kind. The category is one
of a fixed set: `invalid_request` (invalid request/arguments), `route_unavailable` (route or
method unavailable), `tool_execution_failure` (semantic/tool execution failure),
`output_contract_failure` (output-contract/wrong-output failure), `internal_failure`
(internal/server failure), or `unknown` (fixed catch-all). `mcp_error_code` is a fixed COARSE
bucket — `jsonrpc_client`, `jsonrpc_server`, `tool`, or `unknown` — never a raw or verbatim
code (even reserved JSON-RPC codes are bucketed, not stored numerically), so no raw exception,
message, tool argument or body is ever stored. Likewise, a public accepted response, an
upstream 422, and a logical run's eventual failure are different observations, not
conflicting rows.

## Read-only admin API

Paths below are relative to the same authenticated admin origin and require an
ADMIN operator session. Responses use the existing `AdminEnvelope` with `data` and
contextual `meta`. Each `Inspect` of a full exchange emits a `request.debug.read`
audit event so every disclosure of a captured payload is recorded.

| GET endpoint | Result |
| --- | --- |
| `/admin/api/v1/apps/{app_id}/requests` | This App's exchange summaries, including known-model pre-operation failures |
| `/admin/api/v1/apps/{app_id}/requests/{exchange_id}` | One authorized full exchange bound to that App |
| `/admin/api/v1/requests` | Authorized global summaries, including requests with no App attribution |
| `/admin/api/v1/requests/retention` | Payload-free retention preflight (oldest `started_at`, total, and count over the fixed 90-day cutoff) — proves the eligible set before any purge; reveals no payload and deletes nothing |
| `/admin/api/v1/requests/{exchange_id}` | One authorized full exchange |

List queries accept `from`/`to` ISO timestamps, `limit` (1–200; UI uses 50), opaque
`cursor`, and optional `operation_id`. Time filtering uses **request start**, not
logical operation acceptance or completion. Pass `next_cursor` back unchanged to
continue within the same window; it is pagination, not a request count. The UI
resets cursor paging when its selected request window changes.

List `data` is `{items: DebugExchangeSummary[], next_cursor: string | null}`.
Summaries include identities, endpoint/method/status/error type, source, attempt
numbers, observed byte counts and completeness/redaction flags. They intentionally
omit bodies, headers, query strings, error-detail text and the MCP failure signal
(`mcp_is_error` / `mcp_failure_category` / `mcp_error_code` are detail-only fields — open
the exchange to see them).

Detail `data` is one `DebugExchange`, adding `query_string`, header-pair lists,
`error_detail`, `mcp_is_error` / `mcp_failure_category` / `mcp_error_code` (the fixed MCP
failure signal), `request_body`
and `response_body`. Each body contains:

```text
encoding: utf-8 | base64
data: retained text in that encoding
content_type: observed value or null
observed_bytes: number of body bytes actually observed before redaction
complete: whether the body finished on the wire (wire-completeness only)
redacted: whether sensitive content was replaced
truncated: whether the body was withheld rather than stored (observed_bytes still full)
```

`complete` and `truncated` are independent: a wire-complete body that was withheld
(because it is a response body, which is never stored, or because a request body
exceeded the cap) is `complete=true, truncated=true` with its bytes replaced by a
`[REDACTED]` marker — do not read `truncated` as incomplete. A body cut off on the
wire is `complete=false`.

Nullable identities/statuses are not invented. A request without a durable
operation shows **No operation**; an unavailable status is **Not observed**, not
HTTP 0 or success.

## Completeness, storage and limits

- Each stored body is bounded by `requestDebugMaxBodyBytes`. A body within the cap is
  stored whole and fully inspected; a body over the cap is **withheld** entirely
  (a `[REDACTED]` marker, `truncated=true`), never stored as a boundary-cut prefix,
  because a credential crossing the cap could leave bytes no scrub can be trusted to
  remove. `observed_bytes` still reports the full length seen on the wire. Capture
  does not bypass existing endpoint validation, upload or runtime response bounds, and
  it is not an unlimited packet recorder or a new model payload-size allowance.
- A rejected request body may never have been consumed by the application. The
  public middleware does not drain it merely to fill a log. Interrupted, unread,
  failed or limit-exceeded streams remain explicitly partial/incomplete. An empty
  complete body is different from zero bytes retained from an unread body.
- The response body is **never stored** — it is withheld entirely (a `[REDACTED]` marker,
  `truncated=true`), regardless of content type or structure. This is fail closed and
  content-independent: any part of an untrusted response can carry an opaque secret — not
  just free text, but a **numeric value** (a secret encoded as digits), an **object key**
  (a secret used as a field name), or a streaming/binary payload — so no allowlist over the
  response content can be trusted. Only the true `observed_bytes` and wire-completeness are
  kept. The debugging workflow is served by the fully captured (credential-redacted)
  **request** plus non-sensitive typed metadata set from server dispatch state — `http_status`,
  `error_type`, model/tool, **`mcp_is_error`** (distinguishes an MCP tool error inside an
  HTTP 200 from success) and timing — never derived from the response bytes. The request body
  (the debugging target — customer input) is retained credential-redacted, not withheld this
  way. Response headers keep only two **structural header names**, and only when the name is
  exactly that header: `Content-Type` (value reduced to a bare, server-known MIME type from a
  fixed allowlist — an arbitrary/unknown subtype is dropped) and `Content-Length` (value
  **always redacted**, since a digit string can encode data and the true length is reported as
  `observed_bytes`). For **every other** response header BOTH the name and the value are
  redacted — an arbitrary header NAME (e.g. `X-…`) is as untrusted as its value, and
  otherwise-"safe" names like ETag, Content-Language or X-Request-Id carry opaque values.
  (Restoring response-body free-text inspection for debugging — via a governed on-demand
  audited reveal, or a keyed non-reversible correlation marker — is an open operator/owner
  decision layered on this safe default.)
- `error_detail` is a **generic, payload-independent code only** (e.g. "runtime
  operation failed"); the raw exception string is never stored, because an SDK may have
  embedded a prompt, URL or credential in it. The `error_type` and `http_status` carry
  the actionable classification.
- Capture scope (which tenant/App is recorded) is decided only from
  **server-authoritative** dispatch state after catalog/route authorization — never a
  caller-declared model or tool name from the request body — so a caller cannot spoof
  another App's scope and a denied request is not attributed to the model/tool it
  claimed.
- Sanitizing a captured body (credential redaction of the request; withholding the
  response) runs off the event loop in a worker pool with bounded concurrency; under
  overload a capture is dropped
  (best-effort) rather than delaying inference. The per-body cap
  (`requestDebugMaxBodyBytes`, max 256 KiB) also bounds sanitizer CPU/memory.
- `observed_bytes` is not necessarily the displayed length after redaction or
  base64 encoding. Public counts describe observed application body chunks;
  upstream response counts describe the decoded HTTP body iterator, not compressed
  wire bytes, transport framing, advertised `Content-Length` or GPU work.
- Authentication secrets are removed before persistence: authorization/cookie
  headers, credential-bearing query/body fields, known credential values and
  recognizable token forms are redacted. Other model/customer inputs and outputs
  remain debugging data, not anonymized data. `redacted=true` means the retained
  representation differs; it is not an exact unredacted byte replay.
- Full detail documents are encrypted in PostgreSQL using the existing payload
  cipher/keyring. Searchable summary metadata is stored separately. Preserve the
  existing keyring needed to decrypt historical records; no key material enters
  the browser. The capture path does not emit bodies, headers, query strings or
  exception-detail text to ordinary application stdout/Loki logs.
- Persistence is best-effort and bounded in time so a debug-store failure does
  not replace the inference response. Payload-free persistence warnings can occur;
  a missing row is not proof no request happened. Process failure can also leave
  missing captures. Bodies from before capture was enabled, or from failed
  persistence, cannot be reconstructed from old usage/logical-run metadata.
- Captured exchanges have a **hard 90-day TTL** (7,776,000s). The deleting purge is owned
  by the platform's central maintenance task (its own DELETE grant and schedule; this
  capture facility defines and executes no deletion). The purge removes `fs2_request_debug`
  rows older than 90 days by timestamp, under a maintenance credential that can read no
  payload, header, query or ciphertext column; **records within 90 days are always
  preserved**. It runs only after a **payload-free preflight** (oldest `started_at` + counts,
  no payload read — see `retention_preflight`) confirms the eligible set, and never in a live
  environment without explicit rollout authorization. Enabling capture still increases
  PostgreSQL/storage use within the 90-day window; disabling capture (or the ≤90-day
  capture-expiry lapsing) stops new rows but does not retroactively delete history faster
  than the TTL. Reads require ADMIN and are audited, so retention is bounded and access is
  attributable rather than open-ended.

## Verification status

The UI has passed its focused tests and the complete 217-test console suite plus
TypeScript/production build. Offline cases include upstream 422 details, operation-
less failures, historical absence, null metadata, binary/partial/redacted capture,
plaintext rendering, duplicate headers, lazy loading and JSON export. These are
synthetic technical fixtures, not evidence of arbitrary customer capture.

The following describes the **pre-remediation** deployed baseline and does NOT reflect
the current contract: on 2026-09-09, release `88520758f90a7e171abd86a4a94787a6739d6ba7`
was deployed with the old global capture enabled. [Bounded live API acceptance](../acceptance/request-debug-20260909/README.md)
verified a synthetic PhenoAge success, actual Boltz2 upstream 422 and OpenFold2
upstream 400, operationless malformed HTTP 422, MCP discovery and malformed MCP
arguments (tool error inside HTTP 200). That run observed the then-retained public bytes
and upstream error bodies. **Under the current contract those response bodies are NOT
retained** — the response body is always withheld and the MCP tool-error-vs-success case
above is distinguished by the retained `mcp_is_error` signal, not a stored JSON-RPC body.

The first OpenFold2 call used the verifier's stale archival operation name and
correctly returned 403. That failed receipt remains intact; only its unexecuted
remaining cases ran after correcting the helper to use current discovery. This
does not claim an error-free first attempt. Raw payloads stay private; the linked
credential-free summary contains IDs/counts/hashes. Actual browser inspection is
a separate release-owner acceptance gate.

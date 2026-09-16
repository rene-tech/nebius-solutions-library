# Request debug logging

This is an opt-in operator debugging facility, separate from ordinary logs, usage
counters and logical run history. It is **off by default** and, when enabled, is
governed and fail closed: the request body (the debugging target) is stored redacted
within the cap, while a response body is stored only as its structure and numbers with
every string value redacted — an unknown, malformed, incomplete or oversize body is
withheld entirely rather than stored as a prefix; captures are deleted by the platform's
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
    request_debug_models     = "boltz2"          # and/or request_debug_tenants
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

- `request_debug_tenants` — comma-separated tenant IDs to capture. Only the named
  tenants are recorded; unauthenticated/rejected requests (no tenant) are not.
- `request_debug_models` — comma-separated model (App) IDs to capture. All tenants'
  use of those Apps is recorded, including pre-admission rejections for the App.
  When both allowlists are set, an exchange must match both.
- `request_debug_expires_at` — a required RFC3339 instant after which capture stops
  even while enabled. To actually capture, set it in the future and within
  `request_debug_max_window_seconds` (default 7 days). There is no unbounded or
  "capture everything" mode.

The control plane validates this at startup: an enabled policy that is unscoped, has
no expiry at all, or sets an expiry beyond the maximum window fails fast rather than
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

Retention/purge of captured exchanges (and of transport telemetry) is owned by the
platform's central maintenance purge — its own retention settings, DELETE grants
and schedule — not by this capture facility.

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

An HTTP 200 on MCP does not establish tool success: inspect the retained JSON-RPC
response/tool result. Likewise, a public accepted response, an upstream 422, and a
logical run's eventual failure are different observations, not conflicting rows.

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
| `/admin/api/v1/requests/{exchange_id}` | One authorized full exchange |

List queries accept `from`/`to` ISO timestamps, `limit` (1–200; UI uses 50), opaque
`cursor`, and optional `operation_id`. Time filtering uses **request start**, not
logical operation acceptance or completion. Pass `next_cursor` back unchanged to
continue within the same window; it is pagination, not a request count. The UI
resets cursor paging when its selected request window changes.

List `data` is `{items: DebugExchangeSummary[], next_cursor: string | null}`.
Summaries include identities, endpoint/method/status/error type, source, attempt
numbers, observed byte counts and completeness/redaction flags. They intentionally
omit bodies, headers, query strings and error-detail text.

Detail `data` is one `DebugExchange`, adding `query_string`, header-pair lists,
`error_detail`, `request_body` and `response_body`. Each body contains:

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
(because it exceeded the cap, was an arbitrary/unstructured response, or its request
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
- Response bodies are fail closed by structure. A response is stored ONLY when it is a
  **complete, valid JSON document**; anything unknown, malformed, incomplete, streaming
  (SSE) or binary is withheld (a `[REDACTED]` marker). A stored response keeps only its
  **structure, numbers and booleans**: **every string value is redacted** to a fixed
  `[REDACTED]` marker (no reversible or forgeable hash) and dict entries whose key is not
  a safe short identifier are dropped, so no arbitrary or opaque secret in a string value
  or key is ever stored. The response is also withheld outright when its matching request
  exceeded the cap (the uninspected request tail could be echoed). The request body (the
  debugging target — customer input) is retained credential-redacted, not reduced this way.
  Response headers keep only an allowlist of safe protocol/cache values (content-type,
  content-length, cache-control, date, etag, …); every other response header value is
  redacted. (Retaining response free-text for debugging — via a keyed non-reversible
  correlation marker, an intact credential-redacted copy, or a governed on-demand reveal
  path — is an open operator/owner decision layered on this safe default.)
- `error_detail` is a **generic, payload-independent code only** (e.g. "runtime
  operation failed"); the raw exception string is never stored, because an SDK may have
  embedded a prompt, URL or credential in it. The `error_type` and `http_status` carry
  the actionable classification.
- Capture scope (which tenant/App is recorded) is decided only from
  **server-authoritative** dispatch state after catalog/route authorization — never a
  caller-declared model or tool name from the request body — so a caller cannot spoof
  another App's scope and a denied request is not attributed to the model/tool it
  claimed.
- Sanitizing a captured body (redaction/hashing) runs off the event loop in a
  worker pool with bounded concurrency; under overload a capture is dropped
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
- Captured exchanges have a **hard TTL** owned by the platform's central maintenance
  purge (its own retention setting, DELETE grant and schedule; this capture facility
  defines none of that). The purge deletes `fs2_request_debug` rows by timestamp
  under a maintenance credential that can read no payload, header, query or
  ciphertext column. Enabling capture still increases PostgreSQL/storage use within
  the TTL window; disabling capture stops new rows but does not retroactively delete
  history faster than the TTL. Reads require ADMIN and are audited, so retention is
  bounded and access is attributable rather than open-ended.

## Verification status

The UI has passed its focused tests and the complete 217-test console suite plus
TypeScript/production build. Offline cases include upstream 422 details, operation-
less failures, historical absence, null metadata, binary/partial/redacted capture,
plaintext rendering, duplicate headers, lazy loading and JSON export. These are
synthetic technical fixtures, not evidence of arbitrary customer capture.

On 2026-09-09, release `88520758f90a7e171abd86a4a94787a6739d6ba7` was deployed
with capture enabled. [Bounded live API acceptance](../acceptance/request-debug-20260909/README.md)
verified a synthetic PhenoAge success, actual Boltz2 upstream 422 and OpenFold2
upstream 400, operationless malformed HTTP 422, MCP discovery and malformed MCP
arguments (tool error inside HTTP 200). The exact observed public bytes, private
upstream error bodies, caller ownership, operation/attempt correlation and
authentication redaction passed. No key, model or capacity setting changed.

The first OpenFold2 call used the verifier's stale archival operation name and
correctly returned 403. That failed receipt remains intact; only its unexecuted
remaining cases ran after correcting the helper to use current discovery. This
does not claim an error-free first attempt. Raw payloads stay private; the linked
credential-free summary contains IDs/counts/hashes. Actual browser inspection is
a separate release-owner acceptance gate.

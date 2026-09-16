# Request debug logging

This is an opt-in operator debugging facility, separate from ordinary logs, usage
counters and logical run history. It is **off by default** and, when enabled, is
governed: each stored body is capped to a redacted prefix, captures are deleted
after a configurable TTL by the maintenance job, and reading a captured exchange
requires an ADMIN operator and is audited. Enable it deliberately for a bounded
window rather than leaving it on as a standing state.

## Enable capture

Set `deployment.observability.request_debug_enabled = true` in the existing
deployment configuration, preserving its other fields:

```hcl
deployment = {
  # Preserve the rest of the existing deployment configuration.
  observability = {
    # Preserve the other existing observability settings.
    request_debug_enabled = true
  }
}
```

The default is `false`. Enable it through the normal reviewed Terraform/release
workflow for a bounded window; it is not a browser setting, and it should be
disabled again once the investigation is complete. Reading a captured exchange
requires an **ADMIN** operator (tenant scoping still applies); customer API keys
do not gain access to the admin debug API.

**Enabling capture alone records nothing.** Capture is scoped and time-bounded so
it never records every tenant by default. In addition to `request_debug_enabled`,
name what to capture:

- `config.requestDebugTenants` — comma-separated tenant IDs to capture. Only the
  named tenants are recorded; unauthenticated/rejected requests (no tenant) are not.
- `config.requestDebugModels` — comma-separated model (App) IDs to capture. All
  tenants' use of those Apps is recorded, including pre-admission rejections for
  the App. When both allowlists are set, an exchange must match both.
- `config.requestDebugExpiresAt` — an RFC3339 instant after which capture stops
  even while enabled, so a debugging window is self-closing.
- `config.requestDebugCaptureAll` (default `false`) — explicit opt-in to capture
  every tenant/App. Use only for a deliberate full-capture window; prefer the
  tenant/model allowlists.

With `requestDebugCaptureAll` false and no allowlist entries, nothing is captured.

Two more chart values bound each retained record and are safe to leave at defaults:

- `config.requestDebugMaxBodyBytes` (default `65536`) caps the stored size of each
  captured request/response body. Only a bounded, redacted prefix is kept.
- `config.requestDebugRetentionSeconds` (default `86400`) is the TTL after which
  the maintenance job deletes captured exchanges. `config.requestTelemetryRetentionSeconds`
  (default `2592000`) bounds the metadata-only transport telemetry table the same way.

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
complete: whether the observed capture reached a complete body
redacted: whether sensitive content was replaced
truncated: whether only a bounded prefix was stored (observed_bytes still full)
```

Nullable identities/statuses are not invented. A request without a durable
operation shows **No operation**; an unavailable status is **Not observed**, not
HTTP 0 or success.

## Completeness, storage and limits

- Each stored body is capped at `requestDebugMaxBodyBytes`. A body larger than the
  cap is stored as a bounded, redacted prefix with `truncated=true`, while
  `observed_bytes` still reports the full length seen on the wire. Capture does not
  bypass existing endpoint validation, upload or runtime response bounds, and it is
  not an unlimited packet recorder or a new model payload-size allowance.
- A rejected request body may never have been consumed by the application. The
  public middleware does not drain it merely to fill a log. Interrupted, unread,
  failed or limit-exceeded streams remain explicitly partial/incomplete. An empty
  complete body is different from zero bytes retained from an unread body.
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
- Captured exchanges have a **hard TTL**: the maintenance job deletes
  `fs2_request_debug` rows older than `requestDebugRetentionSeconds` (default 24h)
  and `fs2_request_telemetry` rows older than `requestTelemetryRetentionSeconds`.
  The purge runs under a maintenance credential that can delete by timestamp only
  and can read no payload, header, query or ciphertext column. Enabling capture
  still increases PostgreSQL/storage use within the TTL window; disabling capture
  stops new rows but does not retroactively delete history faster than the TTL.
  Reads require ADMIN and are audited, so retention is bounded and access is
  attributable rather than open-ended.

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

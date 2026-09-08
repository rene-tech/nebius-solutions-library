# Apps browser acceptance — first deployed attempt

Status: **incomplete / failed inference gate**, retained without retry. This is
not a readiness sign-off. The original session has since closed; the separate
[corrective-release follow-up and cleanup](BROWSER-R02.md) preserve this failure.

Runtime source: `d21439d025c806f6a3bcb4167c57b95d84ff923f`. The browser signed in
normally at `2026-09-08T12:29:56.992Z`, using installed Chrome `149.0.7827.114`.
Its task-owned network namespace and private resolver mount isolate unrelated
host link events; host networking and DNS were unchanged. TLS verification stayed
enabled. No credentials or one-time key values are included here.

## Observed functionality

- Apps, Users and Capacity are the primary navigation; Advanced is secondary.
  The official unmodified Nebius logo and documented brand palette render.
- The seven-day Qwen app history reports **728 logical runs**, all succeeded,
  three owners and the retained last-use time `2026-09-08T09:58:53.735970Z`.
  The default last-hour zero is distinguishable from its lifetime last-use date.
  Scientific counts on this first release still include the separately diagnosed
  upload-bookkeeping defect; their displayed numbers are not accepted run totals.
- All six Qwen tabs load. Runs filtered by succeeded and
  `fs2-h100-operator-handover` return six matching original operations. Live
  queries advance their resolved time window and links preserve it across tabs.
- Metrics contain real nonzero retained concurrent-request, CPU, memory,
  container and ready-container samples. Both GPU charts show **unavailable**,
  not zero. Their reported allocation-overlap limitation is a real first-release
  defect, retained pending the backend identity-merge correction.
- App Logs returns retained Loki plaintext. Searching `cuda-criu-restored`
  returns two matching rows. Containers shows the actual original Qwen Pod UID,
  image, node, four containers, one requested GPU and zero restarts.
- Usage distinguishes logical counts from newly observed transport and leaves
  unrecorded historical request bytes and response durations unavailable.
- Existing Protenix operation `2750e2dc-c91f-42cf-a64f-fe8ba4f10c34` remains
  accessible through its Apps run URL. Its actual restore interval is
  **4.594503 seconds**, with lifecycle-source/quality caveats preserved. Published
  semantic validation and the polling-stopped state render. This retained run
  did not sample a new terminal-to-publication transition.
- Clicking its authorized Download link retrieves artifact
  `1fc22ddf-3aeb-41e5-b247-ca5e7df08307`: **689 bytes**, SHA-256
  `fc37ec74556e3e992e453536e8c256b40549e1e649d6b72fd1ebd246a7cae182`.
  The actual browser download and exact byte/hash comparison passed at
  `2026-09-08T12:38:06.216Z`.
- A second authorized click downloaded the actual full mmCIF structure,
  artifact `52fd085e-24f6-4862-bb37-61eeabf453e6`, at
  `2026-09-08T12:53:09.129Z`: **32,475 bytes**, SHA-256
  `e2dd5d54fd05bac08efec81e822b2b9333859b68dfddc9b1f52b251de04f813d`.
  This is separate from the first 689-byte preparation artifact.

## Independent serving clone and key

The real Create app flow created app
`96c9e1e5-0d82-4532-a2e0-0218cd2a99e0`, route
`app-96c9e1e50d824532a2e00218cd2a99e0`, from Qwen app
`63e4fa62-39f9-58b1-a4cc-b7e73df8fbf2`. App ID, public route and deployment are
distinct; the source model remains `qwen3-8b`. Saving minimum/maximum workers
`1/1` through Settings produced app and desired revision **2** at
`2026-09-08T12:36:46.461Z`.

The separate Pod `ef916e81-5dd4-4a92-87e7-cbd6b74f7343` was observed with vLLM
running, Ready and zero restarts at `2026-09-08T12:38:28.818Z`. Desired-state save
and observed readiness were checked independently; readiness alone did not
establish a usable public route.

A task-owned inference user and exact-clone-scoped API key were created through
Users. The **single** real key-owned arithmetic request at
`2026-09-08T12:38:36.467Z` returned **HTTP 404**, before an operation was returned.
No submission retry or correctness success is claimed. Request/response sizes
were **234 / 75 bytes**; client duration was **0.260385 seconds**. The original
error body was not retained by this bounded helper, so it cannot establish the
404 cause by itself.

The backend owner's subsequent read-only investigation found the exact cause:
the status parser rejected the existing valid 70-character Deployment name,
preventing public route publication. See
[the parser diagnosis and regression evidence](SERVING-APP-PUBLICATION.md).
The original clone is retained for corrective-release proof without renaming or
recreating it; an offline fix alone does not clear this failed acceptance.

The clone's actual transport Usage independently recorded one 4xx exchange,
the same 234/75 bytes, and server final-body duration **0.134901 seconds**.
Logical runs stayed **zero**. The server and client clocks are intentionally
different measurements. Unobserved historical buckets remained null.

The new key authenticated for discovery (200), then was revoked through the
real Users confirmation at `2026-09-08T12:40:46.052Z`. The same credential was
denied with 401 afterward. The key's secret was held only in browser-process
memory. The clone and user are retained temporarily for coordinated diagnosis;
this is not a completed cleanup claim.

## Other retained diagnostics

- One acceptance-harness time-range command used `7d`, while the real select's
  option value is `168`. Its 30-second timeout is retained. Correct deep links
  subsequently loaded the actual seven-day history in the same browser.
- Initial session 401 before sign-in is expected authentication bootstrap.
- Two `ERR_ABORTED` reads coincide with clone navigation and a run-filter change.
  One response-body capture was unavailable after navigation. These are retained
  instead of claiming a zero-event browser session.
- Through `12:40:46Z`, no page JavaScript exception or `ERR_NETWORK_CHANGED`
  occurred. Later session events must be included in the final export.
- The scientific clone lane separately found Settings 503 before inference;
  this browser's retained Protenix read/download does not clear that defect.

Private evidence is under `h100/releases/admin-apps-20260908/experience/browser-r01`:
`session-report.json`, numbered sanitized query responses and screenshots in
`output/playwright/`. Notable screenshots are `initial-ready.png`,
`qwen-metrics-verified.png`, `apps-seven-day-history-loaded.png`,
`retained-protenix-download.png`, `clone-ready-proof.png`,
`clone-usage-rejection-loaded.png`, and `revoked-key-denial.png`.

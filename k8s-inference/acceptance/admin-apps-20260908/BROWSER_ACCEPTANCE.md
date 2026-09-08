# Apps console integrated browser acceptance

Prepared 2026-09-08. **Not executed; no live acceptance claim.** Root supplies the
exact deployed source/image identities and start signal before any live calls.
Use the existing authenticated browser workflow and approved private credential
bundle; never place credentials or issued key values in arguments, screenshots,
console output, committed files or browser snapshots. Retain every failed attempt.

## Prepared runner

`node browser_session.cjs <protected-bundle-path> <fresh-private-output> <exact-40-character-source>`
starts the existing installed Chrome/Playwright after authorization. It accepts
JSON-lines commands on stdin: `navigate`, `tab`, `range`, `filter`, `snapshot`,
`verify-metrics`, `verify-publication`, `download`, and `close`, each with a fresh
`label`. No live calls are made by the offline Node tests.

Only root-authorized mutation acceptance uses the additional `--allow-mutations`
flag. Its bounded `create-app`/`set-workers` actions target only clones created by
this session; `create-user`/`create-key`/`revoke-key` manage only task-prefixed,
session-owned identities. `use-key-discovery` verifies the issued key and later
401 denial without inference. Real key-owned inference/usage remains a separate
explicitly coordinated workload check, not implied by discovery. One-time
credentials stay in memory; structural secret fields and secret dialogs are
removed or masked from evidence. Every run closes Chrome in `finally`, revokes
its keys, disables its new users/clones, and compares source settings exactly.
Disabling a clone is not proof of resource release: verify actual containers and
controller status separately. Concurrent desired revisions cause cleanup to
report an explicit conflict for root, not overwrite another edit.

## Baseline and navigation

1. Record exact release, browser version, start time, requested origin and
   existing app/settings identities. Open a fresh browser context. Capture
   console/page errors, failed requests and non-success API responses, without
   sensitive headers or credential bodies. Use the previously verified isolated
   runner network namespace if root provides one; never change the host network.
2. Sign in normally. Confirm Apps, Users and Capacity are primary, Advanced is
   secondary, official logo displays, and no blank/error frame remains.
3. Confirm all existing serving and scientific profiles have app rows. Show
   counts labeled as logical runs, a retained last-use date, and clear zero versus
   unknown. Select Last 7 days and prove actual history, then a live shorter range.
4. Keep one resolved from/to across tabs. Verify the range control reflects a
   saved fixed link correctly and live windows advance automatically.

## Six real app tabs

- Runs: inspect existing serving and scientific history. Change status and owner
  filters; verify exact app ID in each request. Open a retained original operation
  ID and check status, protocol/HTTP response, owner, timestamps and timings.
  Observe one new authorized scientific run without page reload through active,
  terminal/publication and download. Record unsampled transitions as unsampled.
  For new HTTP/MCP traffic, inspect Observed requests: actual endpoint, method,
  tool, response status, last-body-chunk duration and complete payload bytes must
  match retained client receipts. HTTP 200 with a tool error is not a success.
  Replays and polls produce separate exchanges linked to the same operation ID.
- Metrics: capture all seven actual metric charts, units, summary/aggregation,
  sample timestamps and source state. Require nonzero history on an active app,
  not merely rendered SVGs. Check available zero and unavailable series separately.
  Use `verifyMetrics` on captured envelopes; a missing source is not a passed
  populated-chart proof.
- App Logs: search an actual message and filter a real instance; verify plaintext
  log rendering, correlated app/run/Pod identity and bounded pagination. Preserve
  empty/retention-source limitations; do not substitute fabricated log samples.
- Containers: inspect actual image, Pod UID, container, node, GPU request,
  start/end, readiness and restarts. Follow its Logs/Run links. Empty cold apps
  must not imply failure or invented warm instances.
- Usage: compare logical counts against durable operation rows; verify status
  classes, owners, token units and distinct estimated/measured GPU clocks. Polls,
  replays and retry attempts must not inflate logical counts. Historical traffic
  byte counts that were not recorded must remain unknown.
  Compare observed transport counts separately, with explicit observation bounds
  and partial-body coverage; do not equate an unobserved historical window with
  zero HTTP traffic.
- Settings: read desired revision, observed revision/state, scaling and startup
  options. Scientific job-only apps must not present a batch cap as ready workers.
  Verify eligibility/assets remain reachable without an Academic top-level page.

## Authorized reversible changes (only after root approval)

1. Read and preserve the complete source app settings. Create an independent
   serving app using **Create app**, a unique task name and the selected source
   deployment. `verifyAppIdentity` requires distinct app ID, public route and
   deployment while retaining the source model. Do not count a second row alone.
2. On the task-owned clone, save a bounded worker setting through **Save settings**.
   Confirm the Apps PATCH/revision, actual deployment reconciliation and ready
   worker separately. No Terraform action is part of this UI flow.
3. Coordinate one real semantic invocation against each independent public route
   with the workload owner. Verify each app's run/usage/log attribution is separate
   and source app settings are byte-equivalent to baseline. Do not issue extra
   benchmark traffic from the browser lane.
4. In Users, use a task-owned identity and scoped key only once the observer's
   exact UI/contract is integrated. Create, use and revoke the key, preserving
   usage attribution; never capture the one-time secret. Verify revoke denial
   separately from a healthy invocation. Restore/disable the temporary identity.
5. In `finally`, restore complete original settings (including absent optional
   fields) for any existing app changed. Stop/disable the task-owned clone and
   prove its instances released; retain its history. Root owns any final removal.
   `verifySettingsRestoration` checks exact specs rather than displayed defaults.

## Completion

Record screenshot paths under private `output/playwright/`, response hashes,
operation/app IDs, real timings, nonzero chart sample counts, artifact SHA-256
and exact size, every failure, restoration/release and normal browser close.
Publish a sanitized report only after all applicable checks actually pass.
`node --test test_browser_checks.cjs` validates the evidence assertions offline;
it is not a substitute for the integrated browser test.

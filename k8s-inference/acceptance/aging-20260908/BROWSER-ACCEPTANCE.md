# Aging Apps read-only browser acceptance

Prepared on September 8, 2026. This is a protocol, not a completed live result.
The release owner supplies the exact deployed source and rollout GO before the
browser starts. The workload lane exclusively owns model requests, keys and
scaling; this lane does not save settings or create resources.

## Runner and boundary

Reuse `../admin-apps-20260908/browser_session.cjs` **without**
`--allow-mutations`, using the release owner's isolated browser network namespace
and protected credential bundle. Supply a fresh private output directory at
`h100/releases/aging-20260908/browser-r01` and the exact 40-character release
commit. The previous namespace holder was removed; a historical PID must not be
reused without the owner's current confirmation.

The Node runner accepts JSON-lines commands on stdin. Only `navigate`, `tab`,
`range`, `filter`, `snapshot`, `verify-metrics`, `sign-out` and `close` are used here. No
one-time credential dialog is opened. The ordinary authenticated sign-in session
is the sole stateful browser prerequisite, not an inference or configuration
change. Close normally after the bounded checks, targeting 15 minutes after GO;
coordinate with the release owner if active workload acceptance needs more time.

## Live checks

1. Record source, browser version, namespace and sign-in time. Confirm loaded
   Apps inventory contains distinct AltumAge and Clinical PhenoAge identities.
   Preserve their actual app ID, source model, public route and deployment.
2. Open each Settings tab. Capture a **loaded** page, not just the immediate
   navigation frame. PhenoAge must show CPU-only treatment, its CPU/RAM resource
   pair and general-CPU pool, with no fake GPU allocation or GPU snapshot option.
   AltumAge must retain its actual GPU placement. Inspect enabled inputs and
   minimum/maximum values without saving a form or changing an existing setting.
   Explicit zero is supported separately from measured elasticity qualification;
   an unqualified warning must not claim a completed cold-start benchmark.
3. Inspect Containers and Metrics for actual allocation and source state. CPU
   memory axes/summaries use binary units; a multi-day range includes calendar
   dates, not ambiguous repeated times. A missing hardware metric remains unknown,
   not invented zero. Preserve unsupported or unavailable measurements explicitly.
4. Coordinate operation/app IDs from the workload lane. Read Runs and Usage for
   those exact identities and the same resolved time window after its accepted
   requests. Record real terminal results, logical run counts, owner attribution
   and observed HTTP counts separately. The browser does not add model traffic.
5. Capture final loaded screenshots and query receipts, sign out through the
   real console button, observe the login form, then close normally.
   Record all JavaScript exceptions, failed reads and non-success API responses;
   expected unauthenticated sign-in 401s and navigation cancellations, if present,
   remain documented rather than silently deleted.

## Evidence

Raw query receipts, accessible page snapshots and screenshots remain private
under the fresh output directory (`output/playwright/` for PNGs). The final
public report includes only allowlisted identifiers, timings, results and receipt
hashes. Do not export tokens, cookies, signed URLs or credential bundles.
An API response alone is not a loaded-page visual pass; an allocated GPU alone
is not proof of model inference or snapshot restore.

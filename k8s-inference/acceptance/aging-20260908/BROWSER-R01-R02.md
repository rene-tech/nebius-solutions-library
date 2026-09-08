# Initial aging Apps browser check: settings blocked

The two Apps are visible, but this attempt **does not pass usable Settings or
public inference acceptance**. Loaded Settings pages for both Apps report
`No managed ModelDeployment is registered for this app.` The form exposes only
disabled identity fields and a disabled Save button; resource and replica fields
are absent. The workload lane stopped before submitting any model request or
changing a setting. This is an Apps registration failure, not a measured model
inference failure.

## Release and bounded attempts

Terraform source: `aecbe75ae1caa73f5d6f959432f7ab7ca3f44953`.
Backend runtime source: `faef58e16ecc266d1c42ee708161dafde748f0a9`.
UI source: `853f54868ec7efd0752c9df21201f03235b4135b`.
Chrome 149.0.7827.114 ran in the explicitly owned local isolated network namespace.
No browser-lane model, key, user, scaling or configuration mutation occurred.

The original `browser-r01` signed in at 14:06:47.426 UTC on September 8, then the
local plain-pipe runner encountered stdin EOF and closed normally at
14:06:47.686 after five captured admin queries. It is a short harness attempt,
not a completed browser check. The interactive `browser-r02` signed in at
14:07:09.909 and closed normally at **14:08:49.350**, after 45 captured admin
queries. Both source receipts remain unchanged.

| App | App ID | Loaded settings observation |
| --- | --- | --- |
| Clinical PhenoAge | `38aad847-9010-51d0-bed7-bcb404bc755d` | 14:07:35.657 UTC; disabled, no managed deployment mapping |
| AltumAge | `197fa990-6f65-539d-a1d8-240877ce861b` | 14:07:43.993 UTC; disabled, no managed deployment mapping |

## Independent chart checks passed

The loaded seven-day Metrics pages render actual observed samples with **MiB**
memory axes and summaries, and **Sep 1 / Sep 8** calendar dates on multi-day
axes. AltumAge process memory shows 495.54 MiB average / 544.4 MiB peak, and
observed GPU memory 738 MiB. PhenoAge process memory shows 29.41 MiB average /
31.52 MiB peak; its GPU utilization and memory are correctly unavailable with
an explicit no-observed-GPU-allocation explanation, not fake zero utilization.
Earlier unsampled intervals remain gaps. These are short observed idle-runtime
measurements, not per-inference benchmarks or proof of GPU acceleration.

Both Metrics API structures pass the existing offline evidence assertions, and
loaded PNGs were visually inspected. Immediate `*-navigation` screenshots are
not used as loaded-page evidence. Runs/Usage updates and CPU-only Settings
controls remain untested because of the registration blocker.

## Errors and cleanup

The interactive browser recorded no failed network reads and no JavaScript page
exceptions. The initial unauthenticated `/admin/api/v1/session` **401** and its
console message are expected and retained. The existing helper closed Chrome
and disposed its context but did not support explicit server-session logout;
this report does not claim it did. A narrow Sign out action was then added to
the acceptance helper for the corrected attempt; all **9 helper tests** pass.

Root requested retaining the one sleep-only network holder for the corrected
release. Its eventual exact-ID cleanup is recorded separately; no other Docker
or cluster resources belong to this browser lane.

## Private receipt integrity

Evidence is under `h100/releases/aging-20260908/`; screenshots are in each
attempt's `output/playwright/` directory. No credentials are exported.

| Receipt | SHA-256 |
| --- | --- |
| `browser-r01/session-report.json` | `cdaba37a5b088ca9e460749c8b2cafcd86186ce8298ffe50d7b877eba403c59a` |
| `browser-r02/session-report.json` | `99beb8a305df4a2b5048923072a88c6edce39f6045a781a6779b2032d12ae89b` |
| `browser-r02/phenoage-settings-loaded.json` | `58ec12c50cf7ef9aa07d3b72dbbaa251bf93e45002e44c205c128e056ead83a9` |
| `browser-r02/altumage-settings-loaded.json` | `206bbdf77cca6d0f318f45233f0eb3b7a73a384e6b4f00ed8738f3fb8214d02e` |

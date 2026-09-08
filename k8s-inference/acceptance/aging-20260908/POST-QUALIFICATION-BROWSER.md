# Final post-qualification browser check: passed

On September 8, 2026, the loaded Settings pages for both canonical Apps passed
after Terraform release `0b15272a63b52406dfd4cf3735b0f805c12d9a01`.
Backend runtime remains `c188ddaada3d433ecc6792a0fdadbd24b705408e` / `25c6b54c…`;
UI remains `d00976744bd7cefb93896cac3cac8585ed8d6b0b` / `d474e818…`.

| Loaded check | Clinical PhenoAge | AltumAge |
| --- | --- | --- |
| Stable public route | `phenoage` | `altumage` |
| Minimum / maximum workers | 0 / 1 | 0 / 1 |
| Idle / autoscaler cooldown / startup retention | 300 / 300 / 900 seconds | 300 / 300 / 900 seconds |
| Per-worker resources | 1 CPU core / 256 MiB; no GPU | 1 GPU requested, clearly distinguished from actual utilization |
| GPU snapshot control | Not applicable; selector absent | Only normal loading offered |
| Unmeasured scale-to-zero warning | Absent after qualification | Absent after qualification |
| Observed worker state | Cold; ready 0 / desired 0 | Cold; ready 0 / desired 0 |

The managed desired revision remains 2 and App revision remains 3. Qualification
metadata did not reset saved settings or change App identities. Settings controls
are enabled; this read-only check did not save a setting or invoke either model.
Snapshot qualification and startup-level promises are **not** inferred from
scale-to-zero qualification: both remain standard loading / level Off.

Chrome 149.0.7827.114 signed in at **15:35:56.060 UTC**. Loaded screenshots were
captured at **15:36:23.508** (PhenoAge) and **15:36:41.074** (AltumAge), and both
were visually inspected. The actual sign-out action observed the login form at
**15:37:09.001**; the browser closed normally at **15:37:09.129**.

There were 42 observed admin responses: 41 captured JSON bodies, comprising
40 HTTP200 and one expected unauthenticated session401. Explicit logout caused
one session `ERR_ABORTED` and its uncaptured response body. No unexpected
application-read error or JavaScript page exception occurred in this short
session. This does not erase the recovered context503 in [r05](BROWSER-R05.md).

## Final local cleanup

After browser closure, the exact owned network namespace contained only its
sleep process. The holder's immutable ID, name and ownership labels were checked:
`d2eb3def00570e0be4261504dba883a3158c5426d31390f048ef75ad8e4952b2`
(`fs2-aging-browser-net-20260908`). It was stopped and automatically removed;
an exact-ID all-containers query confirmed absence at **15:37:41 UTC**.

Only that disposable local holder was removed. Saved browser evidence remains;
unrelated containers/processes and all cluster/cloud resources were untouched.
The host resolver hash is unchanged. No aging browser, local holder or inference
traffic remains owned by this observer lane.

## Evidence

Private root: `h100/releases/aging-20260908/`.

- `browser-postqualification-r01/output/playwright/phenoage-settings-qualified-loaded.png`
- `browser-postqualification-r01/output/playwright/altumage-settings-qualified-loaded.png`
- `browser-postqualification-r01/session-report.json`, SHA-256
  `a7d701749bd256c9e5e64d0bd4de0eca7d9803d790490f9b4b255e8a59cbce8b`.
- `browser-net-cleanup.json`: exact ownership, process, logout and removal receipt.

No credentials or signed URLs are exported into this report.

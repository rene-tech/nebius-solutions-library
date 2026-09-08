# Aging Apps r04: UI passed; workload outcome mixed

The deployed Apps resource/cooldown UI and live Runs/Usage/Charts work in this
bounded browser test. AltumAge served both HTTP and MCP requests; Clinical
PhenoAge served its cold HTTP request but its second MCP admission failed before
an operation ID was returned. **This is not an all-model acceptance pass.**
The failed MCP request remains visible as a tool error despite HTTP 200.

## Release and browser boundary

Backend/Terraform source `55c8744f2d0e08cd2ed49e444f216ec9b572d730`, image
`sha256:5328ce31e7fc2781832163dde2a8d8a274113d0ef7f528e6b22eb04cb246a8df`;
UI source `d00976744bd7cefb93896cac3cac8585ed8d6b0b`, image
`sha256:d474e818258ca0e78a3ec6765d3a035f7ded616a5f94d89ad4252aa23aa4988a`.
Chrome 149.0.7827.114 signed in at **14:46:45.163 UTC** on September 8, 2026.
It signed out through the real console button, observed the login form at
**14:58:13.194**, and closed normally at **14:58:13.326**.

There were 236 observed admin responses. No unexpected application-read failure
or JavaScript page exception occurred before logout. Preserve the initial and
post-sign-out session401s and the session `ERR_ABORTED`/one lost response body
during explicit logout; this is not a blanket zero-error claim. The browser
submitted no inference, saved no settings, and created no keys/users/resources.

## Loaded visual checks

- PhenoAge Settings now states **1 CPU core / 256 MiB per worker, no GPU**. The
  GPU selector is absent, replaced by an explicit not-applicable explanation.
- AltumAge Settings states **1 GPU requested per worker**, explicitly distinct
  from current allocation/utilization. Only normal loading is selectable; no
  unqualified GPU snapshot is represented as available.
- Both forms expose min0/max1, the existing **300-second autoscaler cooldown**,
  separate idle300/startup900 controls, and the honest unqualified-scale-to-zero
  warning. Inputs are enabled; this read-only lane does not claim a live save.
- Both actual Metrics pages render nonzero observations, binary memory units
  and Sep 1 / Sep 8 date axes for a seven-day window. Missing intervals remain
  gaps. PhenoAge GPU metrics are unavailable, not fabricated zero. AltumAge's
  observed 0% sampled utilization does not mean its brief inference used no GPU.
- The actual AltumAge Containers row matches the independent Pod UID, node,
  exact image, one GPU and zero restarts. After natural idle cleanup, the loaded
  final page shows **zero container records** at 14:58:12.939.

The release owner independently viewed the loaded Settings and Metrics PNGs.
Private screenshot names include `phenoage-settings-loaded`,
`altumage-settings-loaded`, `altumage-metrics-week-loaded`,
`altumage-containers-hot-loaded` and `altumage-containers-cold-loaded`.

## Actual runs and attribution

| Model/request | Exact operation | Browser outcome |
| --- | --- | --- |
| PhenoAge cold HTTP | `126ad621-03d4-488c-8f47-8fdbd3e1bbd0` | succeeded; startup7.99s, execution0.38s, operation8.37s |
| AltumAge cold HTTP | `272db2c3-65a3-45a8-b6b7-cc14580ea2b5` | succeeded; startup260.12s, execution0.51s, operation260.63s |
| AltumAge warm MCP | `49f26031-3d2a-42ee-b5cf-054ac524380e` | succeeded; readiness check0.48s, execution0.4s, operation0.88s |

These backend clocks differ from the workload client's end-to-end times:
14.133s, 269.605s and 6.252s respectively. No startup is called snapshot restore.
The failed second PhenoAge MCP call is not assigned a fabricated operation ID.

The same fixed r04 usage window, **14:46:34–14:52:30 UTC**, shows:

- AltumAge: exactly two successful logical runs, one owner, 55 observed HTTP2xx
  exchanges and zero MCP tool errors.
- PhenoAge: exactly one successful logical run, one owner, eight HTTP2xx
  exchanges **and one MCP tool error**. The latter is not another successful run.

Polls/replays remain separate observed exchanges. Unobserved request-byte totals
and model token usage remain unavailable. Estimated GPU allocation is labeled
as an estimate, not measured per-run occupancy; shared idle is not fabricated as
exclusive user GPU time.

## Independent live hardware/startup witnesses

Both raw witnesses contain the actual public Pod, node and UID-filtered events;
they do not reuse the earlier direct-worker identities.

PhenoAge Pod `93a1036a-c230-4eb4-9773-68df115e052e` requests 1 CPU/256 MiB and
no GPU on the existing `batch-cpu` node. Its node was created September 6 at
22:33:00 and Ready since 22:33:31. The Pod was created/scheduled/container-started
at 14:46:47 and Ready at 14:46:49: **two coarse seconds**. The exact image was
already present according to the Pulled event; no network transfer duration is
invented.

AltumAge Pod `cc8e04b0-de1e-4703-92bd-1f90c36a7ba2` requests 2 CPU/4 GiB/one
GPU on newly provisioned node `computeinstance-e00fktj3bftp51dbaj`. A bounded
read-only `nvidia-smi` confirms H100 80GB HBM3, SM9.0, driver580.159.04. Node
creation is 14:48:38 and Ready transition14:48:57.

| AltumAge physical phase | Observed time |
| --- | --- |
| Pod created → scheduled, including new-node wait | 14:46:47→14:49:09;142s |
| Actual autoscaler scale-up event | 14:47:18; h100-1x0→1 within unchanged max2 |
| Image pull | 14:49:09→14:50:53; reported103.936s |
| Container running → Pod Ready | 14:50:56→14:51:03;7 coarse seconds |
| Complete Pod-created → Ready | 256 coarse seconds |

The pull event's image size4,167,595,352 bytes is **not measured wire traffic**.
Two expected connection-refused startup probes occurred before Ready; there
were zero container restarts. The second useful request used the same hot Pod.

The workload lane observed natural zero workers twice at 14:56:27.883543 and
14:56:43.332448; final summary was Cold. No forced scale-down occurred. The local
browser process is closed, but root requested retaining the exact sleep-only
network holder for the corrected r05 attempt; its final cleanup is separate.

## Evidence location

Private browser evidence: `h100/releases/aging-20260908/browser-r04/`, PNGs under
`output/playwright/`. Hardware evidence:
`h100/releases/aging-20260908/public-apps-r04-runtime-witness/{phenoage,altumage}.json`.
Only allowlisted findings are exported here; credentials and signed URLs are not.

| Private receipt | SHA-256 |
| --- | --- |
| `browser-r04/session-report.json` | `616ee9eaa309af3af50292f07c0fe626debd2b3e48115a9a0459ea82368273fa` |
| `public-apps-r04-runtime-witness/phenoage.json` | `150f24a56ee499845c56681a9db0dd2e78cfe2e9370f9194f3093ed3b81686e8` |
| `public-apps-r04-runtime-witness/altumage.json` | `a0e9dd3a1a9e34f91cd062050451b7a790af134771fd9606893b48ce3eed17b9` |

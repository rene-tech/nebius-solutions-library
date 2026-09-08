# Aging Apps r05: model acceptance passed; recovered admin transport error retained

Both canonical Apps completed a cold HTTP request and a distinct warm MCP
request, exact replay/result checks, and natural scale-to-zero. The read-only
browser verified real Settings, Runs, Usage, Metrics and final empty Containers.
One admin context request returned HTTP503 and recovered automatically; this is
**not an error-free browser session**. Earlier failed attempts remain documented.

## Release and bounded browser session

Backend/Terraform source `c188ddaada3d433ecc6792a0fdadbd24b705408e`, image
`sha256:25c6b54cd803d4647f8909ed4f398d5326db8bcbf5cb696a354d167891f12c6c`;
UI source `d00976744bd7cefb93896cac3cac8585ed8d6b0b`, image
`sha256:d474e818258ca0e78a3ec6765d3a035f7ded616a5f94d89ad4252aa23aa4988a`.
Chrome149.0.7827.114 signed in on September8,2026 at **15:06:04.698UTC**,
signed out through the actual console button at **15:18:49.047**, and closed
normally at **15:18:49.178**. The browser issued no inference, setting, key or
resource mutations. Only the separately authorized local network holder remains
for the forthcoming post-qualification UI check; no observer traffic remains.

There were **259 observed admin responses**, with255 JSON bodies retained
(253HTTP200 and two expected session401s). Four bodies were not captured:
two navigation-time response-body losses, the non-JSON503 below, and a session
response aborted during explicit logout. The latter also records one
`ERR_ABORTED`. No JavaScript page exception occurred. The existing helper does
not save non-JSON response bodies; absence of a saved body is not a missing HTTP
observation or a successful response.

### Recovered context503

The browser observed `/admin/api/v1/context` HTTP503 at **15:11:23.000** and
the next HTTP200 at **15:11:24.103**, without user reload or navigation. The
release owner correlated the exact Envoy request:

- Start15:11:22.953, request ID `9be9dc3f-1651-444c-9e5c-9dd32c0ac8af`.
- `upstream_reset_before_response_started{connection_termination}`, response
  flag`UC`, duration0ms, response95bytes.
- API logs show preceding context200 at15:11:07.965 and next200 at15:11:24.055;
  no application-handler503. Current API Pods remained Ready with zero restarts.

This establishes an upstream connection termination, **not its underlying
cause**. Database/model failure or a particular keepalive mechanism is not
proven. Automatic UI read retry recovered and the page remained usable. The
separate four model requests did not fail.

## Loaded UI checks

- Clinical PhenoAge Settings: **1CPU core /256MiB per worker, no GPU**; GPU
  snapshot selector absent and explicitly not applicable.
- AltumAge Settings: **one GPU requested per worker**, distinct from observed
  allocation/utilization. Normal loading only; no GPU snapshot qualification.
- Both forms retain min0/max1, idle300, editable autoscaler cooldown300 and
  startup900. The measured-scale-to-zero warning is still honest during this
  pre-promotion session. This read-only check confirms enabled controls, not a
  setting save by the observer.
- Both Metrics pages contain real nonzero CPU/memory/container observations,
  binary memory units, and dated UTC axes for the selected seven-day range.
  Missing observations remain gaps. PhenoAge GPU charts are unavailable, not
  manufactured zeros. AltumAge's sampled0% utilization does not establish that
  its brief successful CUDA inference used no GPU.
- Final loaded Containers pages show zero records: AltumAge15:18:10.471 and
  PhenoAge15:18:29.624. Natural cleanup is independent of browser logout.

Loaded screenshot evidence includes `phenoage-settings-loaded`,
`altumage-settings-loaded`, `phenoage-metrics-week-loaded`,
`altumage-metrics-week-final-loaded`, both final usage screenshots, all four
completed Run views and both `*-containers-final-loaded` screenshots. The
earlier `altumage-metrics-week-loaded` screenshot caught a refresh/loading state;
it is retained but **not** used as loaded-chart proof. The final named screenshot
was visually inspected. The release owner also viewed loaded Settings, Run and
Metrics screenshots independently.

## Four distinct successful logical runs

| Request | Exact operation | UI startup/readiness | UI execution | UI operation | Client wall |
| --- | --- | ---: | ---: | ---: | ---: |
| PhenoAge cold HTTP | `42a96c54-3592-4a44-a0ce-1d442466f4a6` |8.61s|0.37s|8.99s|14.125s|
| PhenoAge warm MCP | `d3a66635-e5a1-4569-8d2e-af500104a11d` |0.34s|0.15s|0.50s|4.710s|
| AltumAge cold HTTP | `0e20df81-c296-4a4c-8012-bd2e5771691d` |343.32s|0.43s|343.76s|348.707s|
| AltumAge warm MCP | `f3375439-b07c-4ebd-849c-3e1d1de92ba8` |0.20s|0.33s|0.54s|6.969s|

UI values are rounded backend lifecycle timings; client walls additionally
include protocol/polling overhead. Warm readiness checks are not cold startup,
and none of these measurements is GPU snapshot restore. Workload receipts record
all four succeeded on attempt1, exact replays and semantic/result parity.

The same fixed **15:06:40–15:13:00UTC** usage window shows exactly two successful
logical runs and one owner per App. AltumAge has69 observed HTTP2xx exchanges;
PhenoAge has12. Both have zero HTTP4xx/5xx, zero MCP tool errors and zero
incomplete responses within this app-scoped window. Polls and replays remain
separate transport exchanges. Unobserved request bytes and model token usage
remain unavailable. Estimated GPU allocation is not measured per-run occupancy;
shared serving idle is not falsely allocated to each user.

## Exact live worker and node boundaries

The witnesses contain the actual r05 public Pods, nodes and UID-filtered events,
not identities or cache assumptions borrowed from r04/direct-worker tests.

PhenoAge Pod `c4fcbd56-1f4c-495a-ae8f-07e000ee59f1` requests1CPU/256MiB,
no GPU, and has zero restarts. It uses existing CPU node
`computeinstance-e00tq9jk3nbvh4ceck`, created September6 at22:33:00 and Ready
since22:33:31. Podcreated/scheduled15:06:52, runtime started15:06:53,
PodReady15:06:55: **three coarse seconds**. Its exact image was already present;
no network transfer duration is invented.

AltumAge Pod `5f07f70a-c49a-41b2-ae40-9f0c9cd5738b` requests2CPU/4GiB/oneGPU,
with zero restarts. It ran on **new** node
`computeinstance-e00tdgtrwk53nd159k`, UID
`805e9188-8a90-4380-9f96-f191153865c3`. Bounded read-only hardware inspection
confirmed H10080GB HBM3, SM9.0 and driver580.159.04.

| AltumAge physical milestone | UTC time / duration |
| --- | --- |
| Pod created / Kueue admitted |15:06:52 /15:06:53|
| Existing autoscaler triggered h100-1x0→1, unchanged max2 |15:07:23|
| New node created / Ready |15:10:02 /15:10:21|
| Pod scheduled |15:10:42|
| Image pull |15:10:43→15:12:19; reported96.335s|
| Container started / Pod Ready |15:12:23 /15:12:31|
| Entire Pod-created→Ready |339 coarse seconds|

Scheduling events initially report no fitting available node, then transient
new-node taints/GPU availability, followed by successful placement. A startup
probe temporarily refused connections before Ready; it did not cause a restart.
No autoscaler-failure event was observed. The pull event reports image size
4,167,595,352bytes, **not measured network wire bytes**.

Compared with r04, scale-up-trigger→node-registration increased80→159s; this
largely explains the slower cold start. The cloud-side cause of this extra
79s is not established. Both runs actually provisioned new GPU nodes and pulled
the image; r05 is not described as a cached-node benchmark.

Workload final outcome is PASS at **15:18:11.007712UTC**, runner exit0. PhenoAge
was Cold/zero at15:12:28.848557 and AltumAge at15:18:09.616458 after two zero
samples. Temporary keys were revoked and401 verified. Original pool references,
min0/max1 and300s timers remain; no forced node/worker scale-down occurred.

## Evidence

Private root: `h100/releases/aging-20260908/`. Browser JSON/screenshots are under
`browser-r05/` (PNGs in `output/playwright/`); model acceptance is
`public-apps-r05/`; fresh hardware witnesses are
`public-apps-r05-runtime-witness/`. Only allowlisted findings are exported here;
credentials and signed URLs are not copied.

| Private receipt | SHA-256 |
| --- | --- |
|`browser-r05/session-report.json`|`40ce4a72fac607762ff86b705203dddb835ba40285fa782d42acfb41b3fb3716`|
|`public-apps-r05-runtime-witness/phenoage.json`|`d909b2ab4676709254eee4ff5aefe27a80e1abd4fad699198b34b0ee72b642e1`|
|`public-apps-r05-runtime-witness/altumage.json`|`9793f44384bec76ef7b2cb734b67113576936cf990ad6e020075355b44783e13`|

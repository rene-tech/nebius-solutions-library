# Speech public integration — 2026-09-16

This is an evidence log, **not complete onboarding or clinical qualification**.
Public origin `https://89.169.99.188`; TLS verification enabled in all clients.
Calls use temporary ordinary Rene tenant keys, scoped to the two speech Apps,
revoked after each cohort. Admin bootstrap credentials never enter test receipts.

## Full file cohort r4 — passed

Gateway image source `b66c55164`, Helm130, H10080GB, pinned NeMo/checkpoints and
model images as in `GERMAN-MULTIMED.md`/`SNAPSHOT-RESTORE.md`. Hot model replicas
use the conventional loader; production snapshots are not yet enabled.
Full FLAC conversion is lossless; no trimming/downsampling to bypass a limit.
Files above8MiB use the existing artifact upload/finalize and native async API.

| Complete recording | Audio seconds | Public request to result | Worker processing | Path |
|---|---:|---:|---:|---|
| English consultation01 |457.920|27.265s|24.184s|Multipart|
| English consultation02 |559.200|34.151s|29.990s|Artifact/native async|
| German Herzrasen |421.860|25.375s|22.725s|Multipart|
| German grippaler Infekt |629.261|41.303s|32.202s|Artifact/native async|
| German Polyarthritis |438.393|24.739s|21.918s|Multipart|

All2,506.634seconds were processed;5/5 complete, nonempty results. Each timing is
one observation, not p95 or an SLA. Includes upload/queue/polling where relevant,
excludes local FLAC encoding. Raw transcripts, operation IDs, checksums and worker
times: `public-medical-files-r4.json`. Medical accuracy is separate; see the
full German reference benchmark and English mixed-speaker quality reports.

## Other public paths

- `public-medical-live-r1.json`: complete457.920s English and421.860s German
  recordings through public WebSocket, simultaneously, replayed **unpaced**.
  Both produced partials before EOS, consumed every sample, and final segments
  exactly matched the durable result retrieved with the customer key.26.342s
  and23.483s total. Not real-time microphone latency: the client sent ahead,
  so post-EOS processing~22.5/19.2s is upload-ahead backlog, not final flush.
- `public-medical-mcp-r3.json`: discover actual typed tools using
  `get_model_schema`, then artifact references, typed invocation, operation
  polling and result retrieval entirely through MCP. Both complete consultations
  passed, English32.857s/German27.843s. Tool names end in `_native`; derive them
  from live discovery, not a guessed App-name conversion.

## Public mixed cohort r1 — all nine calls completed; scale-out defect found

Source `7e5e682d2`, Helm131, 11:46–11:55 UTC. Two full, **real-time-paced** live
sessions overlapped five file jobs and two typed MCP jobs. All completed and all
three temporary keys were revoked. No audio was shortened. Both live final
transcripts exactly matched the durable result and emitted partials before EOS.

| Paced stream | Audio | Session ready | First partial from playback start | Last audio to completion |
|---|---:|---:|---:|---:|
| English consultation01 |457.920s|1.600s|3.429s|0.184s|
| German Herzrasen |421.860s|2.022s|9.028s|0.615s|

First partial is measured from playback start, **not annotated speech onset**.
The German model aligns its first word at8.4s; that is not independent ground
truth. Do not describe9.028s as processing latency.

File completion times were155.627/37.031/227.407/61.399/25.022s in the order of
the table above. MCP English/German completed in231.119/214.666s. These include
waiting under mixed load; they are not the isolated processing times. The busy
worker fix kept admitted jobs alive without duplicate submission. However,
long waits remain a poor experience and this is **not a passing scaling gate**.

Replica observation confirms English burst desired0→1 and ready0→1. The German
burst was requested but repeatedly evicted while unpacking NeMo into the2Gi
`/tmp` emptyDir. The hot replica eventually served the waiting jobs. Prepared
fix routes model unpacking to the existing8Gi `/cache` (`TMPDIR=/cache`), without
increasing resource or cloud limits. Apply through additive template registration
and normal App drain/preview/apply, then rerun; do not patch controller-owned
Deployments manually. Post-fix results are below.

Receipts: `public-medical-live-paced-r1.json`, `public-medical-files-mixed-r1.json`,
`public-medical-mcp-mixed-r1.json`, `public-mixed-scaling-r1.jsonl`, and
`multilingual-pods-before-scratch-fix.json`. The original observer's Pod `ready`
field only checked container status; use retained Pod Ready conditions and
Deployment ready counts. The observer source now checks the actual Ready gate.

## Scratch fix and mixed cohort r2

Helm132 installed additive templates while retaining previous qualified digests
and all sibling model entries. Gateway3/3 and controller2/2 rollout checked.
After all r1 traffic finished, both speech Apps were drained through their normal
API and observed Cold, then new template references applied and min1/max2 restored.
Both fresh hot Pods and both requested preemptible burst Pods reached Ready.
New templates use `TMPDIR=/cache`; resource requests/limits are unchanged.

All nine r2 calls completed, including full paced streams with exact durable
transcript parity. Post-EOS finalization was0.282s English/0.195s German; first
partials3.422s/9.035s from playback start. German session-ready wait28.713s included
the new hot worker finishing conventional initialization. English files/MCP
waited342.733s/401.223s respectively for their first result while burst capacity
was unavailable and a concurrent long-file test occupied the new worker. These
waits remain explicit limitations, not erased by the successful scaling result.
The English burst was initially Unschedulable, then scheduled and loaded; the
German burst no longer suffered the2Gi unpack eviction. Initial and final Pod
events/specs and ten-minute replica history are retained in `*-scratch-r2.json`
and `public-mixed-scaling-r2.jsonl`. Later image-cached repeat r3 is a separate
cohort, not a controlled cold-start comparison.

The exact Pod-create→Ready single observations were36s English hot (image
cached),81s multilingual hot (cached),85s multilingual burst (cached), and287s
English burst (not cached). The English burst breakdown includes71s before
scheduling,170.483s image pull for9,718,733,705bytes, and45s container-start→Ready.
These are conventional-load observations, not snapshot startup or p95. A snapshot
does not eliminate a missing runtime-image pull; image pre-caching remains a
separate required optimization for new nodes.

## Unchanged-release mixed cohort r3

All nine calls passed on the same Helm132/backend/runtime/template release.
Both paced full streams again matched durable results and completed after EOS
in0.260s English/0.351s German. Session-ready waits2.011s/1.104s. First partials
5.234s/9.058s from playback start: English was slower than r2, so this is not an
all-latency-gates pass or a p95 estimate. File results took27.308/33.529/92.558/
36.763/26.210s; MCP English86.776s/German41.383s. Concurrent long-file and private
restore checks are recorded separately; no controlled paired speedup is claimed.
All keys revoked. Receipts `public-medical-{files,mcp}-mixed-r3.json`,
`public-medical-live-paced-r3.json` and `public-mixed-scaling-r3.jsonl`.

r2/r3 establish two consecutive unchanged-release **mixed protocol cohorts**,
not completion of the larger feature, fairness, priority, drain/preemption,
per-GPU accounting or production snapshot acceptance matrix.

## Public 30+ minute recordings

`public-medical-long-files-r2.json` passed both ordinary-key artifact/native
async operations and downloaded/verified the complete result artifacts:

| Fixture | Full decoded duration | Request to result pointer | Worker processing | Result artifact |
|---|---:|---:|---:|---:|
| Complete English consultation01 repeated4times |1,831.680s|125.614s|103.729s|139,589bytes|
| Complete German Herzrasen repeated5times |2,109.301s|121.132s|115.202s|153,373bytes|

These are repeated teaching recordings for duration/boundary testing, **not**
independent30-minute consultations or extra accuracy samples. Every decoded
sample count matches; full transcripts include the final English goodbye and
German final exchange. A unit test verifies lossless repetition of every source
sample. Times include mixed-load waiting/upload/polling where applicable, exclude
local FLAC preparation and final result-artifact download. The results were then
downloaded with the customer key and byte count/SHA-256 verified. No higher limits.

The first long-file test successfully transcribed but its driver mistook the
standard large-result artifact pointer for an empty transcript. Negative driver
receipt `public-medical-long-files-r1.json` is retained. The fixed driver follows
`operation-artifact-result/v1` and authenticates artifact retrieval;5helper tests
pass, including digest-mismatch rejection. The speech skill now describes this
path. No server result-size increase or inference retry workaround was required.

## Failures retained and fixes

1. r1 fileHTTP500: read-only gateway had no writable multipart spool directory.
   Chart6a7c84193 mounts512Mi Pod-local `/tmp`;145chart/route tests passed;
   Helm129 applied,3/3 gateway replicas checked. Negative receipt retained.
2. r2 fileHTTP403: onboarding helper incorrectly put Rene into the operator
   tenant's legacy principal allowlist. Both task Apps now follow ordinary
   API-key model grants, matching shared platform Apps. No global policy or
   unrelated grant change. Repair was through preview/apply API, not direct DB.
3. Multilingual publication: KEDA rejected66-character generated HPA name.
   Renderer b66c55164 bounds derived names to63 with collision-resistant suffix;
   short existing names unchanged.88selected controller/render tests passed;
   Helm130 deployed, both Apps Ready, gateway3/3 and controller2/2 checked.
4. r3 file driver stopped before the second recording because FLAC remained
   above8MiB. The driver was corrected to use artifacts instead of truncating
   audio or raising limits. r4 above passes both paths.
5. MCP r1 driver guessed the tool name without `_native`; switched to actual
   `get_model_schema` discovery. This was a test-client error, not absent tools.
6. MCP r2 overlapped a live English session: runtime429 `runtime_busy` became a
   failed admitted job. **A real shared-capacity bug.** Deployed7e5e682d2 waits
   only for the exact speech no-work-accepted response, keeps the operation ID
   and attempt, refreshes artifact handles on each wait, allows a new Service
   endpoint, and honors cancellation and the existing queue/deadline policy.
   It does not replay accepted audio.124targeted tests passed, plus78runtime/
   usage tests after adding decoded audio duration to file usage. Full regression
   passed2,106tests with15skipped; public mixed-cohort results are above. Native files otherwise
   had missing audio modality usage, although transcript duration was present.

The capacity wait currently occupies an executor and is shown as Running after
dispatch; this is not a completed fairness/priority scheduler. The duration
attributed to the eventual runtime invocation excludes these busy waits. Burst
scaling, user-visible waiting state, sustained fairness and exact GPU-placement
accounting still need final qualification. Do not describe this as10/10 from
the single successful sequential cohort.

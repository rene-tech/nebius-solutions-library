# r03 customer admin experience

Status: **functional admin checks passed; two recovered browser transport failures retained** on deployed source `bc264980f3fedc33c8fdc6d59c93096d8db9ccaf`. This is not an error-free browser cohort. The independent [scientific campaign](../workload/results-r03/aggregate.json) completed 14/14 requests successfully at 08:24:52.683487 UTC; ordinary traffic is measured separately by the observer lane.

The existing bootstrap operator browser signed in at 07:55:36.946 UTC on September 8 and closed normally at 08:26:16.547 UTC. It only navigated, let the UI issue its own queries, and downloaded authorized artifacts. No scientific invocation, model setting, key or policy was changed by this observation lane. The standalone [preflight](admin-preflight-20260908.json) already restored the exact original Cosmos configuration before the cohort. The [redacted observation receipt](r03-browser-observations.json) contains 294 captured UI queries, eight observed run details, 39 successful browser actions, six publication checks and six verified downloads; the owned browser process exited successfully.

## Automatic results and artifact access

Six observed workflows automatically published validated outputs and passed real browser download size/SHA-256 checks. Each was opened while running using one initial navigation. No manual reload was used to recover results; polling stopped after publication.

| Workflow | Download bytes | Download seconds |
|---|---:|---:|
|RFdiffusion sequential|20,368|0.265|
|Protenix|32,475|0.440|
|Mosaic|16,204|0.577|
|ESMFold2|26,292|0.296|
|Second BindCraft|89,594|0.708|
|RFdiffusion four-shard bulk|25,728|0.462|

These are one authorized browser artifact per observed workflow, not model startup times or a claim that the browser downloaded every bulk output. The independent scientific runner validates the complete required result set.

Protenix operation `1001f32b-4ebf-4f1f-98bb-137a3ed30041` reproduced the exact previous publication race:

- At 08:02:03.257 the controller status was succeeded, but artifacts were empty and semantic validation was not-run. The captured UI explicitly showed **Finalizing results**.
- At 08:02:08.394 the next automatic detail query returned nine artifacts and passed semantic validation. The same page displayed download actions without navigation.
- The downloaded 32,475-byte structure matched SHA-256 `d1a5dcd788270be8856cd70cf006f2b426353ebdf5dce857ccedc47576f4249d`.

The other five workflows completed automatically, but their sampled queries did not expose a delayed-publication interval. They are not claimed as additional reproductions of that race.

The new Protenix restore phase is 4.184542 seconds, rendered as 4.18s with `lifecycle-signal-boundaries` and estimated evidence. Independent retained container start 08:01:36Z and actual restored-request marker 08:01:40.184542014Z match it. The separate GPU ledger reconciles 35 occupied GPU-seconds as 17.815458 active plus 17.184542 occupied-idle, without using GPU-seconds to manufacture the wall-time phase value.

## Mixed-batch observations

The ledger showed nine submitted runs, including simultaneous BindCraft/BoltzGen workloads and five completed runs. ESMFold2 used the preemptible H100 pool. The second BindCraft operation `65fe6dbd-3982-48cc-9454-bf7174292272` was admitted on the reserved H100 pool, completed, and published 12 artifacts automatically, unlike the earlier impossible single-GPU CPU-fit case. Its authorized download passed and the page stopped polling. The final ledger showed all 14 cohort runs succeeded.

RFdiffusion bulk operation `b6d302d9-3f24-40ae-906b-7373def0afab` displayed actual mixed shard admission: at 08:18:37.824, one shard was running, one admitted, and two pending. The waiting admitted shard displayed its full-Pod CPU-fit reason rather than pretending all shards were running. At 08:19:45.199 the view retained one preempted shard alongside running/admitted/succeeded siblings; at 08:23:26.540 it automatically showed the replacement attempt running with three successful siblings. This progressed through the CPU finalizer to succeeded with 23 published artifacts at 08:24:54.264. The 74 detail queries stopped there and remained unchanged through the 08:26:14 polling-stop capture. There was no manual cancellation, retry, reload or recovery.

## Browser transport events remain visible

Two real failed reads were retained; neither is silently removed from the failure count:

| Failed GET | Observed recovery | Host correlation |
|---|---|---|
|08:01:40.717, initial Protenix detail|Successful automatic query 08:01:42.121, 1.404s later. The snapshot at 08:01:41.218 showed the full shell and “Loading scientific run detail…”, not a blank page or error alert at that instant.|systemd-networkd: `veth6ff58d7` gained IPv6 link-local at 08:01:40.713372, 3.628ms earlier.|
|08:10:34.348, second BindCraft detail|Previous success 08:10:29.294; next automatic success 08:10:35.756, 1.408s after the failure. No navigation, reload or error action.|systemd-networkd: `veth64556cf` gained IPv6 link-local at 08:10:34.345244, 2.756ms earlier.|

Both failures were `net::ERR_NETWORK_CHANGED`. The [independent host/Chromium investigation](BROWSER-NETWORK-20260908.md) supports a client-host network-change explanation; it is not a captured Chromium NetLog causal trace. No host-network, Chrome or production setting was changed to suppress the events. There were no application exceptions or failed helper actions. The only HTTP error was the expected unauthenticated session check returning 401 before sign-in; the three console errors mirror that check and the two failed reads. This report does **not** claim zero browser transport failures, or establish the cause of the earlier unrelated blank-page preflight.

## Independent experience verdict and limits

The repaired admin workflow is usable in this observed cohort: live progress, delayed result publication, authorized downloads, real restore-phase duration, accounting and mixed-shard recovery all worked without intervention. No new product blocker was observed. The two browser transport failures recovered automatically, but prevent calling this a strictly zero-error browser run; a stable client network environment is needed for the remaining clean-cohort acceptance gate, not a speculative production workaround.

This is an operator-experience proof using known fixtures and existing bootstrap credentials, not unaided customer onboarding or proof that every scientific model was submitted through the browser. Scientific API correctness, ordinary HTTP/MCP traffic and resource cleanup have separate receipts. The original failed cohorts and blank-page evidence remain intact. No owned browser remains; no next cohort was started by this lane.

Observation JSON SHA-256: `e89d127ee4277ff87994ea753da5a84091051acba5c4d9ed04a38bccd1567451`.

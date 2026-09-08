# r05 customer admin experience

Status: **admin experience checks passed, including recovery; zero unexpected browser failures**. Exact deployed source remained `c85aa26e46f84ca5ae0a85b2454e86522b65cead`, unchanged from the [completed r04 browser cohort](R04-EXPERIENCE.md). The parent accepted r04 across all lanes before authorizing this fresh browser and workload cohort. The separate [r05 scientific report](../workload/REPORT-r05.md) records 14/14 passed clients with all resources released; the parent combines all lanes for the whole-cohort acceptance gate.

The parent-owned isolated network holder was confirmed running before launch. The same installed Chrome 149, user, private-DNS mount prefix and unchanged `browser_session.cjs` (SHA-256 `fd0e641d53334367c83b8830ec0c2a5165b27c2bdf4d6a740e625e01cd36ef07`) were used. The browser started at 09:32:37.885 UTC and signed in at 09:32:39.530 on September 8. Initial readiness at 09:32:54.364 had six UI queries and no failed reads/application exceptions; the initial unauthenticated session 401 was expected and retained. Scientific START was 09:34:48.229446 UTC and exact END was 09:56:40.942742. Browser closure followed recovery at 09:59:10.560 with process exit 0. The [complete redacted observation receipt](r05-browser-observations.json) contains 272 UI queries, eight observed run details, 40 successful actions, seven publication checks and seven verified downloads.

This lane only observed actual UI queries, progress and authorized artifact downloads. It did not submit scientific jobs or change settings, models, keys or policies. No manual reload was used to recover publication. Known fixtures and bootstrap operator credentials do not establish unaided customer onboarding or customer-role administration.

## Automatic publication and artifact access

Seven workflows passed automatic publication, browser download byte/SHA checks, and stopped polling after publication, each with one initial navigation while active.

| Workflow | Public operation | Published artifacts | Download bytes | Download seconds |
|---|---|---:|---:|---:|
|RFdiffusion|`925af8ac-b475-4fa5-805d-db4ca3e467bc`|8|20,368|0.265|
|Protenix|`2750e2dc-c91f-42cf-a64f-fe8ba4f10c34`|9|32,475|0.281|
|Mosaic|`63e62ca9-02ba-412c-8948-79fffd5116d5`|9|16,204|0.265|
|ESMFold2|`de4fef87-f5c1-4162-bc59-4b45c933cec3`|8|26,292|0.281|
|Proteina|`f2dc32c9-0275-4b0b-8ab8-9aaf856e301a`|17|132,762|0.524|
|Second BindCraft|`400bb974-80c1-4cd8-bbc2-c124a147b236`|12|241,461|0.605|
|RFdiffusion four-shard bulk|`d0bf3a01-d206-4fc4-bd65-0caefc784409`|23|25,728|0.282|

Each browser check downloads one authorized structure artifact and compares its exact bytes and SHA-256 with published metadata. These are client download times, not startup or inference latency. Full required outputs, native structural semantics, idempotent replay and resource release are checked separately by the scientific runner.

RFdiffusion reproduced the exact delayed-publication boundary in automatic UI queries: 09:36:09.463 showed succeeded, zero artifacts and semantic validation not-run; the next automatic response at 09:36:14.710 showed eight artifacts and passed validation. The same page recovered without navigation, then stopped polling. This 5.247-second sampled interval is real UI-query evidence, not a claim that a screenshot was taken during that brief interval. The other six workflows did not sample an unpublished-terminal interval; they prove automatic completion, not additional reproductions of the race.

Protenix's observed restore phase is 4.594503 seconds, matching independent container start 09:36:52Z and actual restored-request marker 09:36:56.594503293Z. The separate GPU ledger reconciles 33 occupied as 15.405497 active plus 17.594503 occupied-idle GPU-seconds. Clock and accounting quantities are not conflated.

## Mixed-stage and shard progress

First BindCraft was observed computing on reserved H100, without a claim that its entire publication path was followed. ESMFold2 was opened while admitted/node_pending on `h100-1x`, with CPU prepare already succeeded; the same page advanced through artifact loading and computation to eight published artifacts at 09:41:35.787. Proteina was opened during evaluation after generation/filtering, advanced into analysis, then published 17 artifacts at 09:43:25.434. Its browser structure matched 132,762 bytes and SHA-256 `1ef2e91afb375307fcaf6307e5c5ff30619fe27a29d5818995355039cad3a21f`. No new scientific submission was added for these observations.

Second BindCraft was followed from active compute on reserved H100 through CPU aggregation and 12 published artifacts at 09:51:42.646. Its 241,461-byte browser artifact matched SHA-256 `0d596a5fe7a5847037458bcea4d7858e95fda2efa53ade0422bf02ab29631ca2`, and the 89 detail queries stopped after publication.

RF four-shard bulk was followed from one initial navigation. At 09:52:45.682 all four shards were running, with active compute distinguished from artifact loading across reserved/preemptible pools. At 09:53:52.975 one shard had succeeded, two were running and one reserved-pool attempt was preempted. By 09:55:45.697 three shards had succeeded and the replacement attempt 2 for shard003 was computing; its original preempted attempt remained visible. All stages and 23 published artifacts completed automatically at 09:56:38.553. The 46 detail queries stopped there through 09:57:18.199; the 25,728-byte browser structure matched SHA-256 `7fd068c8acdd879a8c7a52ad6f5b6a4dd12cbc18388f13c0199277c71c2b61ce`. No manual retry, cancellation or recovery was performed. Final BoltzGen was already terminal before any new navigation, so no extra active-to-published browser check is claimed for it; r04 retains that separate proof.

## Recovery, failures and scoped verdict

The independent observer confirmed the fourth complete post-END cluster cycle at 09:58:13.860608, more than 92 seconds after exact END, and five successful ordinary recovery requests through 09:58:28.998375. The final Qwen hot Pod and baseline model policies were unchanged. The browser remained open beyond that gate, with its last run-ledger response at 09:59:09.392 returning HTTP 200 and all 28 visible r04/r05 rows succeeded. It closed normally at 09:59:10.560; no owned browser remains. Parent ownership of the isolated holder and its final cleanup is unchanged.

The full browser window contains zero failed requests, zero page exceptions, zero unexpected HTTP errors and zero failed helper actions. The expected initial unauthenticated session 401 and matching console message remain visible in the record. The two prior r03 host-correlated read failures, earlier failed cohorts and unexplained historical blank preflight remain intact; they were not silently retried, deleted or relabeled as successful.

The admin workflow passed on two consecutive unchanged-release browser cohorts, including real output-publication delay, authorized downloads, accurate restore clocks and accounting, stage progress and automatic shard recovery. No customer-blocking product defect was observed in this browser lane. This is scoped operator-experience evidence, not an unaided onboarding study or a new proof of key lifecycle (the explicitly dated existing key acceptance remains separate). The parent subsequently confirmed the final combined r04+r05 PASS: r05 had 14 scientific, 64 ordinary and 260 admin checks passed, 46 subjects released/reconciled, and complete publication windows with zero route withdrawals. No further browser, model request or configuration change was started.

Observation JSON SHA-256: `2858a8c1fb6b7ed4aa9801d1087f5fa36652233bad6be1e83979d242dab2d0ed`.

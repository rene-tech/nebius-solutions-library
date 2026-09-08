# r04 customer admin experience

Status: **admin experience checks passed, including recovery; no unexpected browser failures**. Exact deployed source remained `c85aa26e46f84ca5ae0a85b2454e86522b65cead`. The independent [scientific cohort](../workload/REPORT-r04.md) ran from 09:01:13.093375 to 09:27:30.125076 UTC, with all 14 clients passed and resources released. The parent combines the separate browser, scientific and ordinary-traffic evidence for the whole-cohort verdict; this report covers the browser lane.

The unchanged `browser_session.cjs` started at 08:59:08.942 UTC on September 8, signed in at 08:59:14.077, and closed normally at 09:30:17.667 with process exit 0. The initial ledger screenshot at 08:59:29.668 captured successful readiness. The complete [redacted observation receipt](r04-browser-observations.json) contains 299 UI queries, eight observed run details, 41 successful actions, seven publication checks and seven verified downloads. No browser remains owned by this lane.

The same installed Chrome 149 and user run in the parent-owned network namespace `net:[4026538794]`, separate from host namespace `net:[4026531840]`. The holder's DNS configuration is bound only inside a private mount namespace before launching the unchanged browser harness; no host-global network or Chromium notification setting was changed. The preceding [read-only isolation preflight](isolated-network-preflight-20260908.json) passed. That earlier short preflight is not part of this cohort's outcome, and the prior r03 read failures remain preserved in [their original report](R03-EXPERIENCE.md).

This lane observed actual UI queries, progress and authorized artifact downloads after receiving coordinated scientific operation IDs. It did not submit scientific work or mutate models, settings, keys or policies. Known fixtures and existing bootstrap operator credentials were used; this is not a claim of unaided customer onboarding or customer-role administration.

## Automatic publication and artifact access

Seven workflows passed automatic publication, real browser download size/hash checks, and polling-stop checks. Each used one initial navigation while active and no reload to recover results. None of these seven sampled a succeeded/unpublished interval; they prove automatic completion, not another reproduction of the delayed-publication race observed in r03. The helper explicitly retains that limitation.

| Workflow | Public operation | Published artifacts | Download bytes | Download seconds |
|---|---|---:|---:|---:|
|RFdiffusion|`04ecc0e7-e2e6-4a4c-aec8-e20850d07afe`|8|20,368|0.354|
|Protenix|`1b0d573b-0ce9-440c-9141-1f0156bbad36`|9|32,154|0.323|
|Mosaic|`c0d69dae-5177-4f1d-9c53-c96960a16559`|9|16,204|0.375|
|ESMFold2|`e248acb4-0b41-4be5-abf0-f0f95c468aab`|8|26,292|0.433|
|Second BindCraft|`f99f408d-384c-48f9-b86d-9d057983a842`|12|240,813|1.743|
|RFdiffusion four-shard bulk|`f7963270-82af-4878-8942-96bdd1841a7a`|23|25,728|0.369|
|Second BoltzGen|`1c36c830-09c6-4241-b6b9-9d8bad38e858`|23|137,923|0.716|

Download times are client download durations, not startup or inference latency. One authorized structure artifact per observed workflow was downloaded in the browser and checked against the published byte size and SHA-256. Complete required scientific outputs, structural semantics, idempotent replay and resource release are checked independently by the workload runner, not inferred from these individual browser downloads.

Protenix restore is 4.315065 seconds with `lifecycle-signal-boundaries`, matching the independent actual container start at 09:03:31Z and restored-request marker at 09:03:35.315065492Z. Its separate ledger reconciles 37 occupied GPU-seconds as 17.684935 active plus 19.315065 occupied-idle. This does not equate GPU-seconds with observed wall time.

## Mixed-stage and shard progress

The first BindCraft was observed computing on the reserved H100 pool and subsequently completed according to the independent workload runner; its full result-publication path was not observed on this page. ESMFold2's CPU prepare completed, its GPU attempt was admitted but waiting for a preemptible node, and the same page automatically moved through artifact loading and active compute on `h100-1x` to eight published artifacts at 09:11:39.174. This is a recovered pending transition, not a failed run. Second BindCraft was followed from active compute on reserved H100 through 12 published artifacts at 09:18:07.688, with polling stopped afterward.

RF four-shard bulk `f7963270-82af-4878-8942-96bdd1841a7a` was opened once at 09:19:34. Its 09:19:44.087 state distinguished one running reserved-pool shard, one preempted `h100-1x` attempt, and two pending/unadmitted siblings. By 09:22:45.653 the same page showed three successful shards plus the real replacement attempt 2 running on reserved H100. The original preempted attempt remained visible. The independent observer also retained a physical Pod recreation under shard002's original attempt 1; it must not be misreported as another logical attempt 2. Publication completed automatically at 09:23:32.482 with 23 artifacts, and the 47 detail queries stopped there through the 09:24:07 snapshot. The browser structure download matched published bytes/SHA; the separate workload runner verified the complete bulk results and released resources.

Second BoltzGen was opened once while design-folding was active at 09:24:37.239, with four prior stages succeeded. By 09:26:41 it had automatically advanced to analysis with filtering pending, then all seven stages and publication completed at 09:27:25.318. Its 33 detail queries stopped there; the 137,923-byte browser artifact matched SHA-256 `6a827af549b5eff98a45390cc1733afe320bb75099f5f619ea91e77c08448515`. No outcome is inferred merely from admission or an available snapshot option.

## Recovery, failures and scoped verdict

The browser remained open after exact scientific END at 09:27:30.125076. The independent observer confirmed six scheduled recovery cycles through 09:29:50.108746, more than 139 seconds after END, with no scientific Pods remaining. The UI ledger continued returning HTTP 200 and all 14 cohort rows succeeded through its final captured query at 09:30:12.654. Browser closure followed at 09:30:17.667, not before the recovery gate.

Across the complete browser window there were zero failed reads, zero page exceptions, zero unexpected HTTP errors and zero failed helper actions. The single expected unauthenticated `/admin/api/v1/session` 401 before sign-in remains in the receipt and its matching console event is not hidden. The earlier r03 host-network read failures and unexplained historical blank preflight remain preserved; successful isolation here does not rewrite them.

The observed operator workflow is usable without manual reload or recovery: automatic progress, stage/shard state, result publication, downloads, restore timing and accounting worked. No remaining product blocker was observed in this browser lane. This is one completed browser cohort on the final release, not evidence that the second required unchanged cohort has already run. The parent owns that decision and the next start; no r05 browser or workload was launched here.

Observation JSON SHA-256: `cf8b2a172da0888cb5a022251384647b256b27ebd521e4a0839be3d2e058adb3`.

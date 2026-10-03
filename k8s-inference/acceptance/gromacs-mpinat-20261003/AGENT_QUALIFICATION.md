# Agent-path reconciliation and client candidates — 2026-10-03

This is actual saved-agent evidence, not a claim that every MPINAT workload or
the newly implemented MPI shapes are qualified. No customer's key or instance
was used. Internal identity: system/qa. Historical scientific runs used the old
single-GPU runtime; replaying their result retrieval does not retest simulation
on the new runtime.

## Coverage before recovery

Evidence root: `/home/tux/secure-handoff/fs2-gromacs-mpinat-20261003`.
Run `reconcile_agent.py` against saved agent-a/b/c folders and the old isolated
QA workspace. Optional live calls are operation GETs only. The script refuses
to overwrite output folders and does not submit scientific or agent work.

The saved corpus had 11 actually submitted chats and 12 distinct operations.
All 12 operations were terminal when reconciled. Agent-b definitions without
submission receipts are not runs. Full actual prompts, idempotency keys,
study states, hashes, report contents and live observations are retained in
`agent-reconciliation-02/reconciliation.json`; `COVERAGE.md` gives the matrix.

| Scientific cases | Count | What remains |
| --- | ---: | --- |
| Native-complete, old agent reports empty/incomplete | 5 | Recover originals and report real timings; no GPU replay |
| Failed scientific operations | 5 | New qualified executions after the reader-first runtime rollout |
| Never submitted | 14 | Actual qualified agent execution |
| Expected-negative diagnostic | Separate | Qualify failure delivery; not a scientific benchmark success |

The failed cases are benchMEM (two operations), benchSFC, ligand-cmet-eq,
ligand-cmet-ti and hif2a-ti. The 19-case remaining manifest includes those five
and the fourteen not-yet-executed cases. Parent must coordinate its start with
runtime rollout and shared capacity. Candidate parameter files are under
`agent-reconciliation-02/candidate-parameters`, not automatically installed in
the QA workspace. Large benchPEP/PEP-h files use a 24 GiB per-request output
budget within the existing contract, without changing cloud quotas.

## Five real analysis-only recoveries

Candidate r1: client source `0700a6fd81bcae77be229d33e79c9fbb097dbe7f`,
image `lc@sha256:73a3340a2c021641b1e4b8ffef2298bf788d5820aede746d07fe18e94a8843c0`.
Actual seeded Kimi-K3/high agent; two chats maximum in parallel. All five
completed and retained full traces. No new GPU work was submitted.

| Case | Original operation | Three native ns/day observations |
| --- | --- | --- |
| benchSFI | `fb0c2f48-fe9c-47d1-97b5-c8efc79992fb` | 63.680, 63.307, 64.365 |
| benchSNC | `2f4fdcb5-aae3-4b7e-acf9-07bdc8196a9a` | 966.255, 958.697, 906.862 |
| benchSNI | `c71db1d0-ff59-496e-9b78-4bc4ade9f0a1` | 685.568, 694.224, 705.943 |
| benchSTC | `925bed89-2914-4977-9535-a1295f4ea4cc` | 412.552, 432.068, 413.682 |
| benchSTI | `292d2dd1-68aa-4084-afdc-74f96b2dec03` | 355.934, 379.647, 359.152 |

Each is a 10,000-step identical-start performance repeat, including native
initialization/tuning; these are not independent scientific replicas or a
convergence assessment. Runner wall durations, checkpoint progress and exact
source log hashes remain per repeat in
`agent-recovery-candidate-r1-verification-v2.json`, with native files and
agent-generated CSV/Markdown under the r1b workspace. `verify_agent_recovery.py`
rechecks native result hashes/size/schema/operation/job, original requested
steps, actual command timing records, CSV values and every cited log hash.

The r1 acceptance found a genuine delivery gap: `native-md` delivery required
`receipt.json`, while read-only recovery intentionally publishes
`recovery-receipt.json`. The SNC agent copied a byte-identical receipt to make
delivery work. That is recorded, not hidden. Generic delivery is now fixed to
read/link the recovery receipt without touching it. Eighty focused client
tests passed, with a persistence-launcher regression and four-engine receipt
coverage. The original normal-receipt and hash/identity checks remain intact.

Remaining prose limitations are not hidden by numeric verification: the SFI
Markdown labels a non-native job identifier instead of native `benchmark`;
some reports leave available system details unexplored. CSV timing verification
does not certify every prose statement or scientific correctness. The original
bad historical reports and r1 reports remain unchanged.

## Immutable r2 and isolated delivery regression

Client source: `59789c88d53a5270c7463181f169975b642fb29d`, including receipt fix
commit `7912e5bcef6e2a38c7362f91460f222696630754`.

Published image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:74b578936eb911320e1ddcec733fa9b26c7c9575b4aee586f977c2b0f3d5d1db`.
Build metadata and registry independently agree. The base is the current
`873be148...` default, with an additive skill/helper/artifact/delivery overlay.
Assembly and final stages add one layer each to avoid the inherited layer-depth
ceiling; no OpenFF reinstall or old UI rollback. Exact overlaid file hashes
were verified inside the running r2 image.

Isolated container: `fs2-default-release-mpinat-candidate-r2-20261003`, local
port 13205, separate persistent `/data` and `/workspace`; same system/qa key,
synthetic QA login, Kimi-K3/high. Old QA state is preserved. No cloud/customer
instance/default selector was changed. Its one analysis-only regression uses
the original SNC operation and explicitly requests native-MD delivery plus
CSV/Markdown, without copying/renaming receipts or resubmitting MD.
Evidence is in `agent-recovery-candidate-r2`. Actual outcome: **failed**. Native
retrieval succeeded, but the agent repeatedly inspected the same result and
made three malformed ad-hoc Python attempts, exhausting its unchanged 25-tool
budget before creating CSV/Markdown or invoking native-MD delivery. A terminal
chat with no transport error is not successful acceptance. Conversation:
`f3ba2395-91f2-528e-9eef-d0ebf98d113a`. The task-owned local r2 container was
stopped after terminal evidence; its state/workspace and reports remain intact.
No guided follow-up or GPU replay was used to turn that failure into a pass.

## Deterministic reporting and immutable r3

Client source `0ba9ee5177684749e38e2577059e955d89021285` adds a small installed
native-MD timing reporter, reusing the same verified receipt, result hash,
operation/job identity and log-hash checks as native result delivery. It writes
machine JSON, CSV and Markdown with actual native simulation command rows.
Missing metrics are explicitly unknown; missing timings/repeats exit incomplete.
Checkpoint positions are not fabricated executed/durably completed work counts.
It makes no API call and never submits simulation work. The GROMACS skill calls
it once after recovery, avoiding unnecessary ad-hoc parsing and receipt copies.

Published image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:6eb9ee53b2cef53ef604a9315147fef894943a132cf36d8c9f31e8606048359b`.
The immutable current `873be148...` base is unchanged. Build metadata and registry
agree; source, skill bundle `2026.10.03.2` and all six overlaid hashes match
inside the isolated running container. 188 combined client tests and 13 offline
reconciliation/verification tests pass, with bundle and style checks.

R3 first-attempt acceptance runs the five original recovery prompts unchanged
on the actual seeded Kimi-K3/high agent, at most two chats at once, with the
same existing system/qa identity. No larger tool budget, replacement model or
manual reporting recipe is supplied. Manifest, traces and new output folders
are retained under `agent-recovery-candidate-r3*`; local container
`fs2-default-release-mpinat-candidate-r3-20261003` uses port 13206 and separate
retained `/data` and `/workspace`. All five passed independent verification:
15 actual native timing rows, zero native-MD delivery errors and zero submission
flags. Each used the reporter once and delivered the original recovery receipt
without copying it. R3 tool counts were 17/20/20/23/15 for SFI/SNC/SNI/STC/STI;
elapsed chat times were 69.4/78.4/57.3/66.3/57.2 seconds. Two incidental inspection
commands failed (SNC `sed`, STI guessed manifest shape), and are retained in the
actual traces. Successful final delivery does not erase those errors. R3's
task-owned local container was stopped after all chats finished; state remains.
This local seeded-agent path does not claim Serverless UI/browser, complete
MPINAT coverage, customer default promotion or new-runtime MPI qualification.

## R4 recovery discovery and final first-attempt check

Source `a09a40c1a099bcb2e99c7e03caa644b30f78e86b` and immutable image
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:6d8b2038097b180d5edd997d7346a1b56c879a5f00b4890fcc08b81c2a96b2da`
preserve r3 helper/runtime behavior while adding truthful default/recovery CLI
help and the complete read-only recovery example in skill bundle 2026.10.03.3.
Submission, authentication, model, tool budget and MD physics are unchanged.
The QA launcher additionally supports explicitly authorized, original-file
read-only binds verified against a private dispatch plan; it does not copy
private structures into Git, a shared bucket or a new customer workspace.
196 combined client tests pass, including CLI/example execution and launcher
hash/readonly/no-copy regressions. Registry and all in-container hashes match.

An unchanged first-attempt SNC recovery prompt passed on exact r4 in 51.2 seconds
and 12 tool calls, with three independently verified native timing rows and
successful native-MD/CSV/Markdown delivery. Conversation:
`592fc20f-7dc3-5262-a7e7-607bf7f3accf`. There were no source-search detours,
nonzero inspection commands, delivery errors, receipt copies or new GPU work.
`agent-recovery-candidate-r4-verification.json` is the numeric/hash receipt;
`agent-recovery-candidate-r4` retains full traces and ordinary prompt.

The local r4 instance `fs2-default-release-mpinat-candidate-r4-20261003` uses port
13207, system/qa and separate persistent state. It remains running for the
parent-authorized actual/assumed Lynx CPU acceptance. No customer endpoint or
default release selector has changed. Remaining hosted scientific cases and
multi-GPU shape qualification belong to the coordinated parent benchmark.

## Hosted r4 continuation: admission is not report completion

The restored QA limit remained two. The coordinator waited for the parent REST
and MPI lanes to drain, then ran the separate hosted alanine example and started
the remaining MPINAT cases in pairs. Ordinary isolated-browser login is refreshed
between pairs; an operator-owned pause file holds only new batches and never
cancels accepted work or changes the inference key/policy.

The first MPINAT pair exposed a real report-integration gap. benchMEM operation
`7c59f87a-d62c-46aa-bd1c-14cb1a5cf0e6` and durable study
`eed9d37a-4a03-5040-8275-cdcbb1a3f316` completed, but the agent planned the generic
`operation-timing` report against the platform envelope instead of the native
MD timing helper. Its initial chat truthfully acknowledged admission only.
The benchmark is not considered delivered merely because Runs says completed.
The original plan, report, files and trace remain unchanged in
`agent-remaining-r4`; the next pair is held pending exact report verification and
a generic durable-study integration fix.

benchPEP initially failed local study validation (missing a declared report)
before inference. Its subsequent `-upload` identity created operation
`bf43bdec-b64b-47a0-b55e-b2f3423c47cb`, protocol
`scientific-artifact-upload-v1`, not a native MD job. As of the retained first
observation it was still transferring; its accepted upload is observed without
creating a second reservation or replaying simulation. No PEP completion or
throughput is inferred from this state.

## R5 durable native reporting and the remaining eighteen cases

Client runtime source `61b4c3bf6c3412746e76afc6ce3bdc1521b9a8c6`, image
`lc@sha256:b948ecca1d7f8d47ede578bbe7733b13714a4625269977014e77f602049ab927`,
adds a typed `native-md-timing` saved-analysis method, explicit upload compression
metadata, and non-secret monotonic upload-phase receipts. The retained r4 PEP
upload eventually succeeded in 688.125 seconds, of which 672.151 seconds was
inside the PUT-call envelope. Network/body/response contributions are unknown.
Its wrong `compression=none` metadata is preserved, not relabeled or reused as
gzip. It never admitted native MD.

Exact r5 first-attempt MEM recovery conversation
`12eaad5a-6940-539d-8701-2a410a30ce64` took 57.223 seconds and ten tools. It used
the read-only CLI route instead of a saved study, so qualifies only that route:
three native repeat measurements, hash-verified sources/logs and working report
downloads, no replay. The distinct saved-analysis conversation
`46ba7ce6-a076-55ae-b471-37a0352815e1` created study
`96b89a35-9e48-5986-92ce-5bcdab33dad6` in 15.077 seconds/seven tools. Its chat
truthfully acknowledged queued admission; the automatic worker subsequently
published all Markdown/CSV/JSON deliverables. Independent authenticated readback
verified nine downloads, three exact native rows and the original MEM source
hash. See `agent-native-study-candidate-r5-verification-v2.json`. Neither path
submitted GPU work, and original r4 failed reports remain untouched.

`verify_agent_study.py` independently checks successful native command identity,
actual repeat set, requested-step provenance, timing values, source/log hashes,
CSV/JSON consistency and promised download contents. Empty generic tables cannot
pass. `prepare_agent_continuation.py` stages only the exact 18 unexecuted native
cases; completed MEM plus the original five are excluded. Original physics and
public input bytes are preserved; only isolated output/idempotency namespaces
change. Small input archives go first and PEP/PEP-h last; this ordering is a
scheduling heuristic, not measured runtime. Seventeen targeted harness tests and
Ruff pass.

At 14:57 UTC the root-authorized remaining lane started with benchSFC and
ligand-cmet-eq, after parent MPI/REST drain, restored QA concurrency two, and
empty live QA inventory. Evidence: `agent-remaining-r5`; staging manifest:
`agent-staging-r5/remaining-mpinat-r5-manifest.json`. The supervisor follows actual
saved studies to terminal publication and independently verifies native reports,
even if the initial chat returns a legitimate asynchronous admission handoff.
It refreshes only browser login between pairs, never changes inference keys or
policy, and observes an operator pause only between batches. This is an active
campaign, not a complete benchmark or customer-ready verdict.

The same task-owned r5 instance was narrowly restarted before admission to add
two original hash-pinned read-only Lynx fixture mounts for separate CPU checks.
Its users, both conversations, completed study, agent, `/data`, `/workspace` and
image were preserved and rechecked. The stopped predecessor and private Docker
configuration remain recoverable. Receipt: `client-r5-input-binding-restart`.
No customer instance or default selector changed.

### Independent selected-case and physics binding

The report verifier is supplemented by `verify_agent_case.py`, a read-only
observer of this existing cohort. It verifies each selected case against the
frozen study plan and input hashes, original staging receipt, batch-helper
request identity, exact normalized native recipe, original upstream TPR bytes,
and three requested 10,000-step timing repeats. A valid completed report about
prior MEM therefore cannot pass another case. The first SFC and ligand-cmet-eq
studies passed this binding as well as report/download validation:

- SFC: operation `c966e97d-6d68-4a72-8281-d8d2cba1a808`.
- Ligand CMET equilibration: operation `7c73ac3f-0400-4c41-a472-8d010398e882`.

Receipts are `agent-selected-case-verification-r5-v2/<case>.json`; the independent
observer watches later terminal studies without changing the running supervisor,
client, policy or model requests. Eleven combined selected-case/report tests
and Ruff pass. The first observer version incorrectly treated the intermediate
`observation_expired` study state as terminal; that observer-only false failure
is retained in `agent-selected-case-verification-r5`, corrected and regression
tested. It did not resubmit or cancel native work. Original r4 failures, the r5
CPU advice error, recovery-only results and this fresh native cohort remain
separate evidence rather than being relabeled as a clean all-path release.

### Supported direct MCP delivery and safe campaign continuation

At 15:43 UTC, thirteen of the eighteen fresh cases were independently verified.
CMET-TI conversation `7883b04e-1088-5321-b146-b0940a0258dc` used the installed
batch helper, hosted MCP and deterministic timing reporter directly rather than
creating a saved Runs study. Its original operation
`cb68970e-f515-4047-87a0-83a3b05c704f` completed and delivered the native files,
Markdown, CSV and JSON. Three exact 10,000-step repeat rates were 73.570, 72.144
and 79.251 ns/day. The final Markdown and authenticated downloads matched the
verified native report. The proof truthfully has `agent_path=direct-batch-mcp`
and `study_id=null`; it does not invent a saved-study record.

The first supervisor stopped because it incorrectly required a Runs study for
every successful delivery. Its original `batch-06.json`, summary and traces are
retained. This was an acceptance-path assumption, not a failed simulation.
`verify_agent_case.py` now recognizes the actual verified delivery tool output,
downloads and report text, then applies the same original bundle, parameter,
recipe, TPR, native timing and log-hash checks as the saved-study path.

`run_agent_cases.py --resume-verified-cases` resumes only after a contiguous
prefix of exact-image, exact-case, no-replay proofs with the original chat and
native operation identities. Existing unverified case directories, mismatched
images or proofs after a gap are rejected rather than accidentally resubmitted.
It preserves the original cohort and failure snapshot and appends only never-
admitted cases. Twenty-one focused regressions and Ruff pass. Supervisor
PID 3133305 and independent verifier PID 3133716 resumed the six remaining
cases; CMET-EQ subsequently passed as operation
`70540e7b-9955-407f-998d-252e491b9ee1`. Client image, system/qa concurrency two,
agent, physics and customer/default deployments are unchanged.

The earlier BTI chat had one recovered GET status 503 (`SERVER_NOT_READY`,
Retry-After 1). It remains in the original transport-warning evidence; the
healthy container had no restart or OOM. The precise readiness branch is
unknown. Neither final native success nor this acceptance fix relabels the
entire cohort as warning-free. Final joined accounting waits for all eighteen
terminal outcomes and keeps the six older native operations/recoveries distinct.

### SHP2-TI: correct delivered result with retained customer friction

SHP2-TI conversation `466f822a-7776-5266-95ca-fc93fe0da885` completed operation
`b7d84d7b-d2cd-46cd-9e53-0b75e4055450` and delivered the correct native report
on its first chat, after 259.370 seconds and 26 tool calls. Its three measured
rates are 60.773, 56.154 and 58.150 ns/day. This is **not a clean customer-path
pass**: the agent selected a short helper observation window (exit 75), then
passed unsupported wait/poll options to read-only recovery (exit 2), then tried
to recover into the original nonempty receipt directory (exit 1). It finally
used a new recovery directory, reported the original operation and delivered
the verified files. No explicit tool-budget abort occurred and no native work
was replayed. The full trace and failed `batch-07.json` remain unchanged.

The independent verifier now binds the original submission receipt to the
separate verified recovery receipt by the same operation, endpoint and caller;
it checks original request/bundle/parameters, exact native recipe and upstream
TPR, logs, timing rows, final report text and authenticated downloads. It does
not copy or invent request metadata in a recovery receipt. The proof records
`recovery_binding`, all three `observed_tool_errors`, `customer_path_clean=false`
and `delivery_outcome=verified_after_tool_errors`. Scientific-result verification
and friction-free client qualification are explicitly different outcomes.
Twenty-two focused tests and Ruff pass, including wrong-operation/caller and
changed-input recovery rejection.

At 15:48 UTC, fourteen native cases had verified selected-case results. Only the
four never-admitted cases resumed: SHP2-EQ, RIB, PEP and PEP-h. Supervisor
PID 3229213 and read-only verifier PID 3223363 retain the same r5 release and
QA concurrency two. SHP2-EQ/RIB are the active pair; no new client candidate is
substituted into this cohort. Avoiding the short-wait/direct-recovery detour is
an outstanding client UX improvement, not a reason to rerun the completed MD.

### Sixteen verified; final large pair admitted — 16:12 UTC

RIB operation `88bb0e31-2e28-478b-bee4-cba3c2f2eab6` and SHP2-EQ operation
`76083dd7-1fbc-4118-b500-ec73fdfcedeb` passed selected-input/physics, native report
and authenticated-download checks. SHP2-EQ's frozen plan omitted the optional
`compression` field. The installed schema explicitly permits contract-and-source-
magic binding; its actual request descriptor and artifact manifest are `gzip`
with the original SHA. The verifier's first explicit-field requirement was too
strict. Omission now receives the same semantic validation while an explicitly
wrong encoding, or a native request that is not gzip, still fails. The original
`mpinat-shp2-eq-failure.json` remains beside the successful proof; no receipt or
request was rewritten. Thirty-two combined verifier/supervisor/accounting tests
and Ruff pass.

The actual PEP and PEP-h chats admitted studies
`f25a69fa-b4e7-5340-9e89-08a9fd90ec3e` and
`187b5a78-eab7-5c39-b1a7-574f11c4d6bb`, respectively. They took 42.211 and
39.193 seconds with 10 and 9 tools, truthfully returning asynchronous admission.
They are not yet native/report completion claims. Supervisor PID 3229213 and
read-only verifier PID 3519854 continue unchanged r5/QA2. A temporary observer
pause marker was removed after the SHP2-EQ semantic check; its diagnosis is
retained in `agent-remaining-r5/observer-compression-pause.json`. The final chats
were already admitted before that marker; no cancellation occurred.

Actual native concurrency is more limited than chat concurrency: the current
one-owner saved-study worker repeatedly selects the oldest nonfinal study until
it has also finished analysis/publication. During RIB, SHP2-EQ stayed queued.
This healthy but serial behavior is not advertised as parallel native execution.
Prepared follow-up TaskDeck card
`fs2-librechat-bounded-study-interleaving-r20261003` specifies a bounded fair
cohort under the same owner, complete ambiguity/cancellation barriers and
exact-candidate overlap/restart tests. Its source-only successor is now committed
as client `75461cad5bfda0dfab175199a8c1b05e4fe558e1`: default cohort one, explicit
opt-in two, one owner, global receipt ambiguity/cancellation barriers, and 117
passing offline tests. It is not packaged or deployed. The release recipe still
needs to overlay/hash the changed worker as well as its study module, and actual
two-study overlap/restart remains unqualified. The active r5 benchmark is not
modified to conceal the limitation.

### Checkpoint segment identity in selected-case acceptance

The report verifier already validates each actual native segment independently
and counts distinct logical repeats. The selected-case verifier now uses the
exact requested `(job_id, step_id)` set rather than requiring exactly three
timing rows. Multiple unique checkpoint segments remain one original repeat;
duplicate segments, missing/extra repeats, changed job/step identities and any
row not bound to the original 10000 requested steps fail. This is an acceptance
contract correction tested with synthetic segmented reports, not a claim that
PEP or PEP-h has resumed. Their original operations and immutable images remain
unchanged; successful native logs, recipe/input hashes and downloads must still
pass the existing independent report checks.

### All eighteen fresh r5 cases verified — 17:57 UTC

The supervisor and independent selected-case verifier both exited zero. All
eighteen fresh cases now bind their original uploaded bytes, exact normalized
native recipe and upstream TPR, three requested 10000-step repeats, successful
native/log measurements, nonempty deterministic reports and authenticated
downloads. The last two cases each verified eleven downloads:

| Case / original native operation | Three native ns/day measurements | Saved study elapsed |
| --- | --- | ---: |
| PEP-h / `4e41fae6-fc50-4fce-a18f-fd2eb3860672` | 2.891, 2.888, 2.940 | 3116.474 s |
| PEP / `7ba11493-fdac-4267-8df5-b7c888407ac6` | 2.075, 2.026, 2.099 | 6331.017 s |

Both used one L40S and one native segment per repeat. Saved-study elapsed
includes queueing, upload, native work, collection and reporting; it is **not**
GPU occupancy or a phase decomposition. PEP waited behind PEP-h under the
documented r5 whole-study serialization. Neither case was shortened or replayed.
The final native PEP operation was already successful while the client was still
collecting its large output set; backend completion alone was not counted as
customer-facing delivery. These finite timing repeats do not establish ensemble
convergence, sixteen-GPU throughput, a native checkpoint-resume trial or a
friction-free customer path.

Evidence: `agent-selected-case-verification-r5-v2` and unchanged actual chats
under `agent-remaining-r5` in the task's private handoff. The two direct-MCP
cases remain distinct from sixteen saved-study cases; SHP2-TI's three errors,
BTI's recovered503 and all false acceptance-observer failures remain retained.
The six older native operations and their later report recoveries are **not**
renamed as r5 admissions, repeated, or double-counted as new GPU work.

The explicit final accounting gate opened at17:57:10UTC. Revalidation checked
234 metadata files and all eighteen exact identities; ready manifest SHA-256
is `740e99977963833132eca5be2ec95184cb480263b906190853251bc1629936ca`.
One combined snapshot is being collected using the existing ledger/cost model,
with old failures and original-image/recovery associations retained. Task-owned
QA containers and telemetry stay available until that join completes and local
state exports verify. Customer/default image promotion remains held, and the
source-only bounded-study successor still requires immutable-image/live gates.

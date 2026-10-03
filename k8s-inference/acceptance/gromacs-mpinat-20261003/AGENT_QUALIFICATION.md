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

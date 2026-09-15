---
title: 'Supervise BioIR evaluation and publish complete per-model recommendation report'
status: done
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: ''
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/report'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'Evaluation complete and published to main: 12 models, four snapshot studies, all negative findings preserved. No production promotion; task resources reclaimed.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:38:35Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own inventory completeness, capacity allocation, benchmark contract, worker integration and final review. Run/supervise child tickets until each required model has current baseline and sourced BIR feasibility, measured A/B where support exists, snapshot/feature results or explicit actual blockers. Implement result aggregation. Do not claim unsupported as speedup. Review once tests finish. Start bounded workers in parallel, launch queued protenix/snapshot when slots free. No production promotion.

Read the complete shared contract FIRST: /home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md. It defines exact authorization, models, node assignments, comparator/quality rules, output paths and cleanup.

## Goal

Evidence-backed model-by-model adoption decision for selling inference per request, with no customer-serving disruption or misleading speedup/cold-start claims.

## Definition Of Done

- [x] Read shared contract and freeze exact live runtime/environment/input identities.
- [x] Run meaningful matched real-GPU tests on authorized idle capacity (or CPU where appropriate); smoke alone insufficient.
- [x] Preserve all attempts/failures; validate scientific output and required feature behavior.
- [x] Record wall time, compute/allocated-GPU seconds per valid request, cold/warm/batch results, snapshot implications and uncertainty.
- [x] Save runnable code, sanitized raw evidence, result.json and report.md in lane directory.
- [x] Update this ticket with concrete results, source commit, resource IDs and cleanup disposition.
- [x] Surface genuine capacity/access/license blockers without bypassing permissions or claiming success.
- [x] Do not alter production routing/models/drivers/quotas, and remove only task-owned test resources after collecting evidence.
- [x] Manager checks scope completeness and integrates final report; no autonomous promotion.

## Plan

1. Inventory assigned model(s), fixtures, exact live images and capacity.
2. Reuse supplied harness/artifacts where valid and prepare isolated comparator.
3. Execute representative cold/warm/batch/feature/quality tests; coordinate snapshot lane.
4. Aggregate actual results, document limitations and recommendation, clean up test resources.

## Current Status

Management active in current conversation. Running collaboration agents /root/bioir_boltz2, /root/bioir_openfold, /root/bioir_coverage. Root also owns Protenix H100 baseline and snapshot preparation. Their GPU Pods are isolated on the assigned unused nodes; Evo2 uses only two verified unallocated slots on the shared eight-H100 node. Collaboration sessions are not tmux sessions.

22:04UTC update: all11BioNeMo baselines plusProtenix measured. Evo2 clone deleted, all13existing modelDeployments Ready. Coverage worker now owns Boltz+OF2 snapshot cycles; Boltz worker takes Protenix snapshot after final L40S matrix, in separate snapshot/protenix directory. Root completes Protenix native batch comparison and report integration. Boltz H100 actual BIR vs resident+optional-kernels2.81–3.00x; OF2 mixedL40S1.44–5.47x but precision/quality gates; OF3 FP32 no meaningful broad warmgain, close pairedstructure agreement after RNG correction. Protenix currently1.66–1.75x vsresident but referencequality drift persists, so no adoption approval. Actual fresh Boltz snapshot restores and newshape works, but firstrestore113.6s slower than normalmodel-load34.76s. Snapshot compatibility is not startup acceleration.

## Live Activity

22:31UTC: all baseline and main Boltz/OF2/Protenix matrices complete, all8nonmapped model baselines complete. OF3 now has a genuinely graph-enabled FP32 prototype and same-H100 lifecycle control; final candidate sweep runs before its dedicated snapshot task. Protenix fresh restore passed initial/new shapes, but output parity requires matched history and remains under test. Boltz/OF2 snapshot lanes complete3freshrestores+3normal controls each: corrected comparable container-to-observed-ready medians93.60/40.04s and98.23/30.89s. Earlier113.6/model-load34.76 observations used different boundaries and are not the final startup comparison. Both snapshot variants are technically compatible but slower; no default enable recommendation. Protenix quality regression persists over18seeded/sample outputs, so adoption held. Complete report draft at report/report.md,12required models plus related applicability; final integration waits for two snapshot children. All released benchmark GPUs clear. One deleted Boltz cache claim left a ReleasedPV with CSI delete timeout; documented without bypass. No user decision needed for benchmark completion.

## Work Log

- 23:19 UTC: OpenFold3 fixed snapshot/control/fallback matrix and cleanup complete. Three low-level restores and three ordinary controls all fail on reused graph keys; seven valid artifacts across 25 attempts, so graph serving and snapshot prototype held. Its exact PVC/PV and all test pods/ConfigMaps are removed; healthy GPU has 0 MiB and no processes. Final Protenix cohort on existing non-preemptible y0jt has 3/3 valid fresh restores; matched controls/fallback remain. Two preemptible-node interruptions retained independently, with no false causal attribution. Raw evidence will be published in checksum-indexed archives without changing local bytes. Eight report aggregation/archive unit checks passed; final acceptance review waits for the last fixed matrix.

- 22:57UTC: core OpenFold complete312/312valid outputs. FP32 graph OF3 gives1.83–3.34x versus same-H100 resident-native control with pairedCA-lDDT1.000; mixedOF3 held. Snapshot2/2fresh OF3 low-level restores reachedhealth but first inference failedCUDAillegalaccess; remaining planned thirdrestore/normalcontrols/fallback ongoing. Protenix originalfkt nodebecameNotReady thenproviderSTOPPED (causeunknown),2restores beforeinterruption; fresh independent xjaw cohort now3/3execution-validrestores, normals/fallbackrunning. Originalfailedtrialretainednotpooled. Read-only storageprovenance confirmsRWO NetworkSSD64/128GiB withdocumented30/60MiB/s ceilings, notsharedRWX andnotoptimalcacheperformance. Noadditionaltuningrounds. Consolidatedreport reflects allnegativefindings. Localfiles/TaskDeckretained; finalbenchmark-onlypublicationwaitsforfixedcontrols/cleanup.

- 2026-09-15: created from current user authorization.

## Test Evidence

Initial artifact-valid counts were reported at21:25–21:29UTC. Important correction from deeper checks: MolMIM21valid envelopes yielded0new decoded molecules; allinput fallbacks, and requested4returned1. This is not product-success. Raw failures and negative results retained. Initial public image1bd717... lacked a compiler; corrected candidate base is sha256:2fcd9e8fff2e1c20e676657bedc56689ded0abb2fc9863618398390ed22805ce. Per-lane result.json files and report/aggregate.py now cover all12required models, plus separately labelled relatedOpenBind. Final review/report notyetcomplete.

## Deployment Notes

Evaluation-only isolated resources; production remains untouched.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

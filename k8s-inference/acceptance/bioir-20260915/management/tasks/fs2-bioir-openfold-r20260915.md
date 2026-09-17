---
title: 'Benchmark OpenFold2 and OpenFold3 against BioIR'
status: done
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: 'fs2-bioir-manager-r20260915'
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/openfold'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'Evaluation complete: OF2 mixed precision conditional; OF3 graph reuse fails with or without restore. Hold OF3 prototype. GPU resources reclaimed.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:38:35Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own openfold lane. Match the deployed OpenFold2 checkpoint and OpenFold3 Preview2 version, not a convenient alternative. Share BIR base with boltz worker. Test current digest vs BIR, monomer and supported complex/templates/MSA/ligand coverage. Preserve HTTP result schema and distinguish any current wrapper feature gaps from BIR regressions. Record H100/L40S results and output equivalence. Review OpenFold3 OpenBind separately for version compatibility without replacing Preview2.

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

Core and separate OpenFold3 snapshot evaluations are in manager review. Collaboration worker `/root/bioir_openfold` is not a Task Deck tmux session. Both assigned nodes were verified idle before allocation and after all core pods were deleted; the subsequently reused H100 is again fully idle after snapshot cleanup. Production remains unchanged.

## Live Activity

- Final 23:19 UTC: separate OF3 snapshot matrix completed negatively. Three low-level restores reached readiness but produced no valid inference; all three ordinary-load controls reproduce the reused-key fault after valid warmup. Incompatible-identity normal-load fallback passes three distinct keys only. All snapshot GPU/CPU pods, two ConfigMaps, temporary 64 GiB PVC and backing PV removed after SHA256 inventory; H100 again has 0 MiB / 0%, no allocations/processes. Both cards are in manager review; no new variants, commits or pushes.
- 23:07 UTC correction: the snapshot child card's first two ordinary-load controls pass warmup, then fail with CUDA illegal memory access when reusing the warmup query key. Follow-on requests fail in the poisoned context. This occurs without restoration: HOLD OF3 graph serving and snapshots, not merely snapshot adoption. No graph-cache repair is implemented or measured.
- Core cohort: 312/312 inference outputs pass sequence/finite-coordinate checks. Same-H100 graph candidate versus upstream with identical residency change gives 3.34x/2.86x/2.35x/1.83x/3.05x ratios for 46/129/214/complex287/full-MSA95. Paired CA-lDDT is 1.0 on all five, RMSD 0.020–0.138 A. Those graph requests varied query IDs and recaptured graphs; they show potential, not cached-graph reuse qualification. OF3 mixed also remains on hold, and the L40S complex pose needs further qualification.
- All20 core pods and3 source ConfigMaps removed, no PVC created. `cleanup.json` records H100 and L40S each0MiB/0%, no GPU allocations/processes before H100 snapshot reassignment. Source, checkpoints, manifest and script hashes remain in evidence; no commit or push.
- Historical execution milestones below are superseded by the final state above.
- 22:20 UTC: L40S graph+resident FP32 Preview2 finished all 23 requests; actual diffusion graph state GRAPH_VERIFIED, no eager fallback. GPU released. Same-H100 upstream-resident native control is compiling first request; same-H100 graph sweep follows. Lifecycle savings will not be mislabeled as BIR-only gains.
- 266 successful outputs have offline sequence/finite-coordinate validation. OF3 mixed precision shows substantial drift on some no-MSA examples and remains on hold. FP32 native-RNG comparisons agree closely; complex variability is recorded separately.
- `fs2-bioir-openfold/openfold3-baseline-l40s`: exact Preview2 image pulled in 179.34 seconds; first real request compiling native CUDA attention.
- `fs2-bioir-openfold/openfold3-bir-float32-r6-h100`: primary model-seam comparator preserves native seed-42 global RNG stream. Prior r5 independent private seed-42 stream is retained as exploratory, not silently treated as matched diffusion noise.
- OF3 H100 baseline and exploratory BIR each completed 23 valid requests including protein complex/full MSA. OF2 baseline, float32, mixed and environment-control cohorts completed. Finished pods were collected and removed with named ownership-checked cleanup.
- Initial BIR image lacked a C compiler for Triton; failed startup evidence retained and that task-owned pod removed before retry.
- Both OpenFold2 baseline pods removed after collecting 15 valid requests each. Full responses and CA lDDT/reference quality evidence are under the lane directory.
- Exact live OpenFold2 digest: `sha256:9fc70e781b18f4f547da237e2eb81387df3c38d092bb44c46145db6347e7e164`.
- Exact live OpenFold3 Preview2 digest: `sha256:1e35247f0de8be59119ddf875c2631173a8c533c90f7b21e1c01ef70a069c00c`.
- H100 assignment `computeinstance-e00j20a9hkb508cn4a`; optional L40S `computeinstance-e00dczh75qcnbj0bx8`.

## Work Log

- 2026-09-15: created from current user authorization.

## Test Evidence

312 valid fresh requests across exact H100/L40S baselines, OF2 FP32/mixed/environment control, OF3 exploratory private-RNG and corrected native-RNG FP32/mixed, both-GPU graph+resident, and same-H100 exact upstream-resident control. Four startup failures retained separately. 80 deliberately invalid feature requests received expected400. `quality.json` retains every output's CA-lDDT/RMSD and confidence; `measurements.json` records all timings and6888.8–7513.9 allocated GPU-seconds (includes failures/init/idle/cleanup). `result.json` is complete; `report.md` contains qualified model-specific verdicts. Exact public BIR source401c6fcc4a43925bcf1342b6c0979b060130b396, not a private EA bundle. Independent snapshot results are linked, not inferred.

## Deployment Notes

Evaluation-only isolated resources; production remains untouched.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

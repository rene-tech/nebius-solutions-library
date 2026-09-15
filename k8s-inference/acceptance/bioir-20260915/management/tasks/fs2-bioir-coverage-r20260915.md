---
title: 'Inventory all BioNeMo models and benchmark non-folding baseline coverage'
status: 'review'
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: 'fs2-bioir-manager-r20260915'
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/coverage'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'All eight current-runtime baselines complete; full raw evidence and nullable BIR fit results ready for manager review. MolMIM generated zero new molecules; Complexa valid workflows passed zero native binder filters. Owned workers removed.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:37:00Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own coverage lane. Freeze live catalog and deployment digests with current attribution list. Assess BIR module applicability of diffdock,evo2-40b,genmol,molmim,msa-search-pdb70,proteinmpnn,rfdiffusion,proteina-complexa using upstream source/docs. Run representative CURRENT runtime baselines on isolated assigned GPU(s), CPU-only for MSA if current runtime is CPU. Explicitly identify Evo2 two-GPU limitation; do not change checkpoint/count to fake parity. For justified module-level BIR candidates provide bounded integration feasibility and share with manager before scope expansion. Cover related scientific-model exclusions and no-fit conclusions with sources, not assumptions.

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

Ready for manager review, collaboration worker `/root/bioir_coverage`. All eight current-runtime baseline suites completed on H100 `computeinstance-e00r9tdjfszjs3angk`, L40S `computeinstance-e00krzha55t0sg3t56`, or the explicitly authorized two-GPU Evo2 clone on `computeinstance-e00m0hsph76ajt9sdb`. All owned benchmark pods were collected and removed; three owned client ConfigMaps removed. Source worktree baseline `83bcb2d6c7f4dc112e414e00596e0d6b03e22712`; public BIR reference `401c6fcc4a43925bcf1342b6c0979b060130b396`. No production resources changed. Snapshot qualification continues under its separate ticket.

## Live Activity

- Worker `/root/bioir_coverage` is a collaboration session, not a deck-managed tmux session.
- Completed ProteinMPNN 28/28, DiffDock 21/21 corrected-route requests plus 21 retained harness 404s, GenMol 21/21, Evo2 21/21, PDB70 30/30 including nine repeated genuine searches, RFdiffusion 12/12. Removed each exact owned pod after collection.
- MolMIM 21 HTTP/schema-valid responses are all input fallbacks (`model_decoded=false`); zero new model-generated successes, GPU-seconds per new result undefined.
- Complexa: nine timed full generate/filter/evaluate/analyze workflows over two targets and batches 1/2. Initial three omitted-initialization failures retained. A final exec-stream reset lost its emitted timing; original artifacts/truncated stream retained and only the affected trial repeated with durable JSON. All 104 saved PDBs and 26 native AF2 evaluation rows were finite, but native binder-success CSVs contain zero passes. No efficacy claim.
- Exact image/checkpoint and source inventory saved; raw JSONL includes public input/output artifacts and every attempt. DiffDock incorrect-route first attempt and Evo2 stale ConfigMap client launch retained as failed harness attempts, then corrected.

## Work Log

- 2026-09-15: created from current user authorization.

## Test Evidence

`k8s-inference/acceptance/bioir-20260915/coverage/result.json` uses the shared array-of-model-records schema; `report.md`, `applicability.md`, raw JSONL, pinned public sources, manifests and lifecycle evidence accompany it. Valid counts: ProteinMPNN 28, DiffDock 21, GenMol 21, Evo2 21, PDB70 30, RFdiffusion 12, Complexa 9. MolMIM has 21 valid response envelopes but zero model-decoded successes; cost per new generated result is undefined. MSA genuine misses (~2.2 s) are separate from cached hits (~1–2 ms). Complexa actual generation is 25 steps from the current fixture; evaluation metadata incorrectly repeats its default 400 and is preserved. Ligand/AME variants remain unmeasured. All unsupported BIR latency/speedup values are null/N/A; RFdiffusion OPM and Complexa pair operations are conditional source-level leads, not measured acceleration.

## Deployment Notes

Evaluation-only isolated resources; production remains untouched. Full Complexa checkpoint hashes verified from read-only mounts after timing. Fresh audit confirms all six original customer GPU pods on the shared Evo2 host remain ready. Exact-name cleanup receipts in `coverage/lifecycle`; artifacts remain reproducible from retained manifests. Empty evaluation namespace retained, no coverage PVC created. Manager owns final review and repository commit; this worker made no commit or promotion.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

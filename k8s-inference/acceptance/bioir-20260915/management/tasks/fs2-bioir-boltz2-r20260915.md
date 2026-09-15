---
title: 'Benchmark Boltz2: current serving vs persistent upstream vs BioIR'
status: done
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: 'fs2-bioir-manager-r20260915'
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/boltz2'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'Evaluation complete: eight matched matrices, 2.66–3.49x beyond the stronger resident control. Chain-ID gate prevents promotion; all test resources reclaimed.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:38:35Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own boltz2 lane. Deploy task-owned clone of current deployed Boltz2 digest; implement persistent upstream and BIR workers preserving required API behavior. Record full matched benchmark matrix on assigned H100 and (if compatible) L40S. Build/pin reusable public BIR base and share image/digest/install recipe early with openfold/protenix/snapshot workers. Measure warm/cold/batch and scientific-output parity. Existing current wrapper subprocess and --no_kernels must be separately attributed. No BIR affinity pipeline claims.

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

Ready for manager review from collaboration session `/root/bioir_boltz2` (not a deck-managed tmux session). Current, resident, optional-kernel resident and public BIR each completed the full matrix on both assigned GPUs. All startup/adapter/native failures retained: 228 completed prediction attempts, 191 valid, 37 failed; 81/81 expected-error schema probes; 218 CIFs audited with zero sequence mismatches and ten BIR heteromer chain-ID failures. Scientific noninferiority remains unverified. Recommendation: conditional prototype, not promotion.

## Live Activity

- Worker: `/root/bioir_boltz2`; session field intentionally blank.
- Assigned H100: `computeinstance-e00fkt1bsa4ec657sn`; namespace: `fs2-bioir-boltz2`.
- Evidence: `k8s-inference/acceptance/bioir-20260915/boltz2/`.
- No Boltz benchmark GPU pods remain. Both assigned GPUs verified at 0 MiB, 0% and no compute processes. Snapshot worker separately owns snapshot pods in this namespace; do not delete the namespace or evaluation-cache PVC while it is mounted read-only. Cleanup of the cache is handed to the manager/dependent snapshot lane after explicit release.
- Public BIR image: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-evaluation/bioir-public@sha256:2fcd9e8fff2e1c20e676657bedc56689ded0abb2fc9863618398390ed22805ce`.
- L40S has 64GB host RAM; all its comparators use explicit 48Gi request/56Gi limit instead of the live H100 96Gi/192Gi. No model/sampling reduction.
- L40S DCGM exporter was already crash-looping, so pre-existing process telemetry is unavailable from it; confirm GPU runtime/process status from task pod before inference.

## Work Log

- 2026-09-15: created from current user authorization.

## Test Evidence

Current H100 HTTP medians (three repetitions each): T1031 29.89s, T1038 30.19s, T1096 36.15s. Public BIR L40S approximately 2.48s/2.60s/5.43s; these are NOT a cross-GPU speedup claim. Current H100 cold artifact preparation 350.76s. Resident upstream first model load 19.75s, subsequent loads approximately zero. Final report will separate current-wrapper reload savings from BIR gains. Native current repeated-sequence multimer fails upstream MSA-path validation; diagnostic and failed requests retained.

H100 current / resident / resident-with-kernels / BIR HTTP medians: 95 aa 29.891 / 6.534 / 6.367 / 2.123 s; 199 aa 30.189 / 6.920 / 6.824 / 2.271 s; 464 aa 36.148 / 12.625 / 8.815 / 3.138 s. BIR is 2.81–3.00x faster than the stronger resident+kernel control, not solely faster because of residency.

L40S current / resident / BIR: 36.412 / 8.253 / 2.481 s; 36.723 / 8.711 / 2.595 s; 50.765 / 22.196 / 5.429 s. Through-client-validation medians and ratios are separately retained. No formal noninferiority, DockQ or tail-latency claim. Interim `result.json`, `report.md`, `statistics.json` and raw output/phase/quality/allocation evidence are present. Public BIR source and image were shared with OpenFold, Protenix and snapshot lanes.

## Deployment Notes

2026-09-15 final cleanup update: all snapshot consumers released `evaluation-cache`; manager-authorized coverage worker deleted the PVC. PV `pvc-cf75a701-6ede-4d91-935e-af6116917554` initially remained Released due to CSI `VolumeFailedDelete/DeadlineExceeded`, but final read-only `report/final-cluster-state.json` confirms it is now absent through normal reclamation. No forced deletion/finalizer changes; no GPU allocation remains. Historical evidence retained: `snapshot/lifecycle/storage-reclamation-exception.json`.

Evaluation-only isolated resources; production deployment spec byte-equivalent before/after. Task PVC `fs2-bioir-boltz2/evaluation-cache` (PV `pvc-cf75a701-6ede-4d91-935e-af6116917554`) remains solely for snapshot read-only reuse. Registry evaluation images retained by digest for reproducibility. No production changes, git commit or push. Evaluation repository source commit `83bcb2d6c7f4dc112e414e00596e0d6b03e22712`.

Final files: `result.json`, `report.md`, `statistics.json`, `verification.json`; `python3 verify_evidence.py` passes all eight matrix/accounting/hash/cleanup checks. L40S optional-kernel medians 8.318 / 9.046 / 14.419 s, versus BIR 2.481 / 2.595 / 5.429 s. Optional kernels are not uniformly faster on the small L40S cases; retain that distinction.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

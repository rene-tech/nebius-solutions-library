---
title: 'Evaluate Protenix v2 BioIR module integration and measured speedups'
status: done
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: 'fs2-bioir-manager-r20260915'
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/protenix'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'Matched H100 evaluation complete: 1.66–1.75x beyond residency, but quality regression prevents adoption. Allocation and separate snapshot studies documented.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:38:35Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own protenix lane. BIR has model module but no complete pipeline: retain current Protenix v2 featurization and output path, map exact checkpoint and sampling semantics to BIR forward. Validate custom adapter first. Run current vs BIR matched cases H100 and eligible L40S including complex predictions and batch. If incompatible, isolate exact missing API/checkpoint/feature support and retain current baseline. No unsupported replacement under existing model identity.

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

Root manager (/root) owns this lane until a worker slot frees. Rechecked dedicated H100 computeinstance-e00xjaw5jqexvvnpat: no allocated GPU pods, 0MiB used, driver580.159.04, Ready. Exact current Protenix image and localized public artifact identities confirmed against its adapter. Manifest prepared; no results claimed yet. Existing snapshot evidence is historical context only, not BIR qualification.

Update 21:54UTC: current conventional9/9 and resident12/12 artifact-valid; BIR graph12/12 artifact-valid on the SAME GPU. Conventional medians86.9–88.6s, resident warm9.2–10.2s, exploratory BIR warm5.6–6.1s. Keep residency separate from BIR gains. Paired reference scoring found divergent structures (ubiquitin CA-lDDT about0.605current vs0.486candidate): NOT scientific equivalence. Investigating diffusion RNG semantics and retaining all exploratory outputs. Eager BIR comparator running. Added multi-seed/multi-sample driver and graph telemetry for follow-on. Snapshot lane owns fresh restore qualification. Production untouched. Strict current wrapper permitsSM90H100 only, so no fake L40S baseline.

## Live Activity

22:25UTC: benchmark lane complete and awaiting final manager integration. Primary global-RNG BioIR graph candidate, native resident and conventional baselines, eager/private-RNG exploratory controls, and six-structure multi-seed/sample batches retained. Total66prediction attempts:65valid and one retained pre-ready failure; all65valid have expected sequences/finite coordinates. Native resident warm9.2–10.2s versus BioIR5.6–6.1s; conventional86.9–88.6s includes per-request reload. Batch25.531s versus13.367s. Across18 seeded ubiquitin outputs, reference CA-lDDT median0.633current versus0.492BioIR: hold candidate, not production-ready. Native strictSM90 wrapper makes L40S unavailable as a matched comparator; no check removed. All root-owned Protenix pods/configmaps deleted; xjaw GPU verified0MiB/0% with no compute processes. Fresh snapshot testing continues separately in fs2-bioir-protenix-snapshot-r20260915 on fkt. Reports/results/raw commands in protenix/; source commit pending final combined report.

## Work Log

- 2026-09-15: created from current user authorization.

## Test Evidence

Raw outputs, commands, image/weight identities and analysis are in the lane directory. One prematurely sent pre-ready request failed and is retained separately. Candidate is not deployable/qualified merely because structures are valid.

## Deployment Notes

Evaluation-only isolated resources; production remains untouched.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

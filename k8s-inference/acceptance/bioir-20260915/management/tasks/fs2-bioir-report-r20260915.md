---
title: 'Consolidate measured BioIR results and per-model adoption recommendations'
status: done
epic: nim-fast-start-platform
agent: codex
parent_id: fs2-bioir-manager-r20260915
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: /home/tux/worktrees/fs2-bioir-evaluation-20260915
code_path: k8s-inference/acceptance/bioir-20260915/report
branch: fs2/bioir-evaluation-20260915
session: ''
summary: 'Final report published to main. Single cross-lane review complete; stale aggregate and cleanup wording corrected. Twelve models and four snapshot studies covered.'
created_at: '2026-09-15T21:21:00Z'
updated_at: '2026-09-15T23:38:35Z'
---
## Original Description

Complete test results and recommendations for every hosted BioNeMo model,
measured against current serving, with snapshot and feature impact and economics
for selling inference by request.

## Refined Task

Read the shared contract at
/home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md.
After the model and snapshot lanes finish substantive tests, independently check
coverage and calculations once, resolve discrepancies with workers, and write
the consolidated Markdown report plus machine-readable comparison data. User
values real customer feature coverage, not smoke-only validation. Show all
failures and incomparable cohorts. Do not rerun experiments just to hide negatives.

## Goal

A decision-ready report: model, actual runtime/checkpoint, GPU, BIR feasibility,
current and candidate latency, measured speedup, throughput, GPU-seconds per
valid request, startup/restore changes, scientific-output parity, API/feature
parity, required implementation effort, recommendation and remaining limits.

## Definition Of Done

- [x] Every eleven BioNeMo catalogue models plus explicitly included Protenix v2 accounted for.
- [x] Fresh matched real-GPU data or explicit unsupported/blocker verdict; no substituted models.
- [x] Snapshot, caching, batch and feature results linked to raw evidence.
- [x] Cost assumptions explicit; valid-request allocation includes loading and idle time.
- [x] Evidence and scripts integrated/pushed by manager without touching unrelated edits.
- [x] Task-owned resources cleaned up; customer workloads retained.
- [x] Final report delivered; no production promotion and no incomplete test labelled complete.

## Plan

Collect lane result.json/report.md, verify exact comparators and exclusions,
check calculations, document per-model recommendations and summarize for user.

## Current Status

Root manager owns integration. All12model results and3of4snapshot-model studies exist; final Protenix non-preemptible controls/fallback remain. Consolidated report at report/report.md distinguishes residency, precision, scientific-quality and serving failures. Final acceptance review is deliberately deferred until substantive tests finish; do not interpret a complete model inventory as scientific equivalence.

## Live Activity

## Work Log

## Test Evidence

Fresh H100/L40S results cover all11BioNeMo-branded models plusProtenix. Boltz2 gains2.66–3.49x versus optional-kernel resident control; OF2 mixed gains are conditional, FP32 is slower. OF3 unique-ID graph gains1.83–3.34x are held because all3ordinary cached-key controls fail CUDA. Protenix gains1.66–1.75x but structural-quality regressions block adoption. Eight unmatched models have fresh native baselines and N/A BioIR gains. Completed Boltz/OF2 snapshot restores are slower; OF3 reacheshealth but failsinference. Allnegativecases retained. Eight offline aggregation/archive tests pass; final review notyetapproved.

## Deployment Notes

Report-only; no customer-serving changes.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

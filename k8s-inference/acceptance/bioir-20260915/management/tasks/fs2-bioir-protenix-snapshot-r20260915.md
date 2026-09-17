---
title: 'Qualify fresh-pod GPU snapshot restore for the Protenix BioIR prototype'
status: done
epic: nim-fast-start-platform
agent: codex
parent_id: fs2-bioir-snapshot-r20260915
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: /home/tux/worktrees/fs2-bioir-evaluation-20260915
code_path: k8s-inference/acceptance/bioir-20260915/snapshot/protenix
branch: fs2/bioir-evaluation-20260915
session: ''
summary: 'Final non-preemptible cohort complete: three restores, three controls, fallback and 18 valid predictions. Restore 1.538x slower; numerical/scientific parity unqualified.'
created_at: '2026-09-15T22:06:00Z'
updated_at: '2026-09-15T23:38:35Z'
---

## Original Description

Measure whether BioNeMo Inference Runtime changes GPU snapshot compatibility or startup time. This is part of the user's all-model evaluation, not permission to promote a candidate.

## Refined Task

Read the shared evaluation contract at /home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md. Own only snapshot/protenix/. Coordinate with /root/bioir_coverage; shared snapshot helpers are read-only for this worker. Reuse the fully recorded, matched-RNG candidate from the model lane.

## Goal

Determine fresh-pod restore compatibility, real startup benefit or regression, and behavior on subsequent different inputs. CUDA Graph capture alone is not a GPU snapshot.

## Definition Of Done

- [x] Record exact image, source, model/checkpoint, GPU and driver binding.
- [x] Capture only task-owned worker; delete donor before fresh-pod restore.
- [x] Run three restore cycles and matched normal-start controls, with real outputs on two distinct shapes.
- [x] Retain graph replay/recapture, output validation, failures and fallback evidence.
- [x] Separate image/init/restore/first-request clocks and storage overhead; do not hide slower restores.
- [x] Clean up only task-owned resources and verify final available-cohort GPU memory is released; stopped-node final memory remains explicitly unobserved.
- [x] Write result.json/report.md and update this card with resource disposition.

## Plan

Finish active model matrix, freeze candidate source, run isolated donor/restore/control experiments, record conclusions and clean up.

## Current Status

Worker /root/bioir_boltz2 completed the final manager-authorized fresh cohort on existing idle non-preemptible computeinstance-e00y0jttwekyghrznp. Technical evidence verified; numerical and scientific parity unqualified, no promotion. Earlier fkt/xjaw infrastructure-interrupted cohorts remain separate and unpooled. Raw evidence frozen, no local benchmark runner. Manager review/publication integration pending. Collaboration session, not tmux. No production routes, nodes, drivers or security-policy changes.

## Live Activity

- 23:33 UTC closure: y0jt completed donor, 3 fresh restores, 3 matched normal controls, and guarded fallback; all 18 predictions passed native/sequence/finite-coordinate/graph validation. Restore container→HTTP-ready median 88.908739s vs normal 57.820205s (1.537676× slower); pod-create medians 122.908739s vs 89.568478s. Matched outputs were 0/6 byte-identical, paired CA-lDDT 0.752308–0.908322; repeated normal and restore dispersion retained without causal attribution.
- Fallback identity guard rejected before executing any restore command, started a new PID 55 (captured PID 174), loaded model and served two validated new predictions. Observed 13.421452s container→HTTP-ready with donor executable-cache reuse; n=1 separate cached-start path, not an extra matched control or a qualified cache-only speedup. `y0jt/fallback-verification.json` proves live HTTP readiness, fresh model binding, and clock boundaries.
- Normal cleanup now confirmed: all fkt/xjaw/y0jt task pods, PVCs, PVs and immutable ConfigMaps absent. Final y0jt GPU 0 MiB, 0%, no compute processes; stopped-node final GPU memory was not directly observable. Prior pending receipts retained. No force/finalizer/provider changes. Root `report/final-cluster-state.json` independently agrees.
- Current node-state correction from saved manager receipt: fkt and xjaw are Ready=True again at closure (Ready transitions 23:17:17 and 23:16:58 UTC). Earlier provider STOPPED observations remain historical incident evidence, not current state. No additional diagnostics or GPU-memory probes were run after the final stop boundary.
- 23:09 UTC: xjaw stopped being Ready at 23:00:34 during ordinary-load control3, before observed model readiness or inference. Preserved 3 successful fresh restores and 2 matched normal controls, 13 validated predictions; fallback unrun. Descriptive 3v2 container→ready medians 87.741s restore / 60.411s normal (1.452× slower), not an acceptance pass. Read-only provider follow-up confirms subsequent STOPPED state but does not establish the initiating cause. Normal exact-name deletion requested for its pod/PVC and immutable ConfigMaps; no force or finalizer changes.
- Manager authorized ONE final independent cohort on a different capacity class: existing non-preemptible H100 `computeinstance-e00y0jttwekyghrznp`, GPU `GPU-9885f9c6-110a-10b5-2c26-c256e895d575`, driver 580.173.02. Fresh launch-time Ready/zero GPU allocations/empty compute processes check passed. New `bir-protenix-y0jt-*` pods, own `fs2-bioir-protenix-snapshot-y0jt` PVC and `y0jt/` evidence. Fresh donor/capture, no old UUID remapping. If this node faults, stop with actual partial evidence; no further alternatives authorized.
- 22:39 UTC: original fkt cohort interrupted during restore3 when node became NotReady (22:35:29); provider subsequently reported VM STOPPED/spec.stopped=true. Initiating cause not inferred. Two valid fresh restores and all CPU-preparation/harness failures retained; no same-GPU normal controls completed, so no fkt startup ratio is claimed. Original pod/PVC retained pending coordinated cleanup, not force-deleted.
- Manager explicitly reassigned existing idle H100 node `computeinstance-e00xjaw5jqexvvnpat`, GPU `GPU-ca98bcbc-cf06-a4c2-e3bc-c27a0cba95bc`, driver580.159.04. New independent donor/capture/cohort uses prefix `bir-protenix-xjaw`, new PVC `fs2-bioir-protenix-snapshot-xjaw`, evidence under `snapshot/protenix/xjaw/`. No old-checkpoint device remapping. Three restore and three normal controls use matching76→129→76 request history, with seed101; first-output clocks are not divided across unlike first-input histories.

- Worker `/root/bioir_boltz2`, collaboration session (no tmux): frozen candidate source in immutable `fs2-bioir-protenix-snapshot-app`; exact candidate image `bioir-protenix@sha256:0c391bdc2ab0c5260a222ec7df5e5adc5ea9bfdbc2e158b386a97aac5dc72e94`.
- Rechecked fkt H100 node Ready, zero compute processes, no allocated GPU pods and no pending customer GPU pods. Task-owned donor `fs2-bioir-protenix/bir-protenix-snapshot-donor` allocated one real GPU; own 128Gi RWO PVC `fs2-bioir-protenix-snapshot` retains capture only.
- Requests retain 10 cycles, 200 steps, seed 101, BF16, no MSA/templates, exact native featurizer/dumper/validator. Two shapes: ubiquitin 76 aa and lysozyme 129 aa. Native Python HTTPServer requires no event-loop change.
- Technical snapshot compatibility cannot establish scientific parity: model lane reports lower experimental-reference quality on ubiquitin. No promotion.

## Work Log

- 2026-09-15: created to execute independent snapshot branches in parallel.

## Test Evidence

Overall entrypoints: `snapshot/protenix/result.json` and `report.md`. Final protocol check: `y0jt/verification.json` (9 checks, technical-only pass), `y0jt/fallback-verification.json`. Exact phase/quality samples: each cohort's `analysis.json`, raw outputs, immutable manifests and lifecycle. Prior partial evidence: `fkt-result.json`, `fkt-report.md`, `xjaw/result.json`, `xjaw/verification.json`; 5 and 13 valid predictions respectively, never pooled into final 3v3 ratio. All 12 failed original CPU-preparation stage records and two pre-inference harness failures remain. Cleanup: `cleanup.json` and each cohort's normal deletion receipts. No commit/push or Slack from worker.

## Deployment Notes

Evaluation only. Do not promote: snapshot readiness is slower, numerical parity is unestablished, and the underlying Protenix model-lane experimental-reference quality regression remains disqualifying. No task GPU/storage resources remain. Evaluation checkpoint binaries were deleted with their PVCs (no external backup); source, raw predictions, logs, manifests and timing receipts are retained for reproducibility. Manager integration/review pending.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

## Publication Receipt

Published to `rene-tech/nebius-solutions-library` **main** in commit `0b980615c326ae613d503fe81a5fb71ae652248c`. Report: https://github.com/rene-tech/nebius-solutions-library/blob/0b980615c326ae613d503fe81a5fb71ae652248c/k8s-inference/acceptance/bioir-20260915/report/report.md

This closes the requested evaluation, not the adoption gates. The nine evaluation cards are done; no GPU experiment or benchmark runner remains active. Recommendations require separate implementation direction. No remote worker branches were created. Final closure-card snapshots are added in a documentation-only follow-up commit.

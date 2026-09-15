---
title: 'Test BioIR GPU snapshots, cold starts and serving-feature compatibility'
status: 'review'
epic: 'nim-fast-start-platform'
agent: 'codex'
parent_id: 'fs2-bioir-manager-r20260915'
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: '/home/tux/worktrees/fs2-bioir-evaluation-20260915'
code_path: 'k8s-inference/acceptance/bioir-20260915/snapshot'
branch: 'fs2/bioir-evaluation-20260915'
session: ''
summary: 'Boltz2/OpenFold2 snapshot qualification ready for review: compatible but slower. Owned probes removed; one Released Boltz cache PV awaits CSI reclamation. Peer Protenix/OpenFold3 extensions integrate through manager.'
created_at: '2026-09-15T21:15:00Z'
updated_at: '2026-09-15T23:37:00Z'
---
## Original Description

User requested all hosted BioNeMo models evaluated for BIR acceleration, actual measured speedups versus current serving, snapshot and other feature implications, real benchmarks and a complete report. Use currently unused H100 or L40S Scientific AI capacity. Separate task-deck tickets, start and supervise.

## Refined Task

Own snapshot lane. Begin by inventorying actual snapshot support and reusable tooling for exact current runtimes, then test fresh-pod restore for BIR candidates from other lanes on assigned isolated H100/L40S. Measure no-snapshot vs snapshot, graphs off/on/recapture where relevant, same-shape and changed-shape valid requests, 3 repeat restore cycles, batch/cancellation/fallback/error semantics. Capture only task-owned PIDs; no driver/security/host changes. Compare image cache, weights, process residence and snapshot cohorts honestly. Provide per-model implications even when unsupported. Wait for candidate worker artifacts without claiming completion.

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

Boltz2/OpenFold2 portion ready for review, collaboration worker `/root/bioir_coverage` (not tmux). Three successful fresh-pod CUDA+CRIU restores per model, donor deletion confirmed before each, distinct shape requests validated, three matched normal-load controls, and incompatible-identity fallback measured. Boltz2 asyncio H100 median container-to-ready 93.60 s restore versus 40.04 s normal; OpenFold2 mixed L40S 98.23 s versus 30.89 s. Paired normal/restored structure text is exact across all measured cases. Compatibility passed, startup acceleration rejected for these configurations. Parent directed no more GPU tests in this worker lane and accepted the documented CSI cleanup residual as a separate follow-up. Delegated Protenix (`/root/bioir_boltz2`, `snapshot/protenix/`) and OpenFold3 (`/root/bioir_openfold`, `snapshot/openfold3/`) extensions remain independently owned and are integrated by manager before overall evaluation completion.

## Live Activity

- All Boltz2/OpenFold2 GPU pods removed; final read-only bundle fingerprints retained and exact owned PVC/ConfigMap cleanup recorded in `snapshot/lifecycle/final-cleanup.json`.
- H100 `computeinstance-e00y0jttwekyghrznp`, GPU `GPU-9885f9c6-110a-10b5-2c26-c256e895d575`; L40S `computeinstance-e00sa78kng1kwhej6q`, GPU `GPU-5fad413e-4ffa-5ca2-281f-f532c1df2bae`; both driver 580.173.02. No cross-driver/device migration claim.
- Parent approved deletion of the now-unmounted task-owned Boltz evaluation-cache PVC in addition to snapshot-owned temporary storage. No production/shared-model PVC deletion.
- Cleanup residual: `pvc-cf75a701-6ede-4d91-935e-af6116917554` remains Released with Delete policy after its claim was deleted; `mounted-fs-path.csi.nebius.ai` emitted `VolumeFailedDelete` / `DeadlineExceeded`. No finalizer bypass, force-delete, controller change or backing-directory deletion. Both snapshot PVs are confirmed gone; both assigned GPUs have no allocated workload/process. Exact residual evidence is in `snapshot/lifecycle/storage-reclamation-exception.json`.

## Work Log

- 2026-09-15: created from current user authorization.

## Test Evidence

`snapshot/result.json` and `report.md` contain complete Boltz2/OpenFold2 measurements. Boltz2: 21 validated requests across all schedules; OpenFold2: 18. Every restore produced valid same-shape/new-shape outputs. Boltz native batch 2 after restore returned both samples; actual graph telemetry is GRAPH_VERIFIED/captured=true with new-shape recapture. OF2 has no enabled CUDA Graph backend in this public BIR registry. Default Boltz uvloop capture failed on io_uring, then CUDA recovery served another valid request; the separately qualified asyncio variant passed. A scratch-cache initializer failure and oversized initial fixture ConfigMap are preserved as harness failures. Both identity-mismatch tests fell back to ordinary loading and returned valid outputs; malformed/missing requests returned 422 (Boltz) or 400 (OF2), wrong routes 404, and health remained ready. Cancellation/idempotency and graph-off snapshot cohorts remain unqualified, not inferred.

Pinned BIR image `sha256:2fcd9e8fff2e1c20e676657bedc56689ded0abb2fc9863618398390ed22805ce`; snapshot tools `sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4`. Frozen application/source ConfigMaps, capture runtime identity, exact checkpoint/source references, all lifecycle boundaries, structures, logs and failed attempts are retained. Bundle inventories hash 6,175,988,629 bytes (Boltz including runtime cache) and 3,002,741,753 bytes (OF2) before removal. GPU allocation accounting includes capture/restore failures, initialization, collection and idle time rather than presenting it as optimized production cost.

## Deployment Notes

Evaluation-only isolated resources; production remains untouched. Container-scoped existing checkpoint capabilities only; no host PID/network namespaces, driver change, GPU reset or customer process checkpoint. Snapshot storage is recreatable from retained scripts/digests; raw result evidence and bundle fingerprints remain. Namespaces are not deleted by this worker. Manager alone commits/pushes, integrates delegated candidate snapshots, closes this shared ticket and sends the final Slack outcome.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

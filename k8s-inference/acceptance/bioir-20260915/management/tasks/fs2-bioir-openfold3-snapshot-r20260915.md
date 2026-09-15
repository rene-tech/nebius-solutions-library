---
title: 'Qualify fresh-pod GPU snapshot restore for OpenFold3 BioIR'
status: review
epic: nim-fast-start-platform
agent: codex
parent_id: fs2-bioir-snapshot-r20260915
private: false
repo_url: 'https://github.com/rene-tech/nebius-solutions-library.git'
repo_path: /home/tux/worktrees/fs2-bioir-evaluation-20260915
code_path: k8s-inference/acceptance/bioir-20260915/snapshot/openfold3
branch: fs2/bioir-evaluation-20260915
session: ''
summary: 'Complete negative qualification: 3 low-level restores ready, 0 serving passes; all 3 normal controls also fail on reused graph keys. Fallback passes 3 new keys. HOLD graph serving/snapshots. All owned pods/ConfigMaps/PVC/PV removed; GPU idle.'
created_at: '2026-09-15T22:06:00Z'
updated_at: '2026-09-15T23:37:00Z'
---

## Original Description

Measure whether BioNeMo Inference Runtime changes GPU snapshot compatibility or startup time. This is part of the user's all-model evaluation, not permission to promote a candidate.

## Refined Task

Read the shared evaluation contract at /home/tux/dashboard/data/epics/nim-fast-start-platform/docs/bioir-evaluation-20260915.md. Own only snapshot/openfold3/. Coordinate with /root/bioir_coverage; shared snapshot helpers are read-only for this worker. Reuse the fully recorded, matched-RNG candidate from the model lane.

## Goal

Determine fresh-pod restore compatibility, real startup benefit or regression, and behavior on subsequent different inputs. CUDA Graph capture alone is not a GPU snapshot.

## Definition Of Done

- [x] Record exact image, source, model/checkpoint, GPU and driver binding.
- [x] Capture only task-owned worker; delete donor before fresh-pod restore.
- [x] Execute three restore cycles and matched normal-start controls with real requests on distinct shapes; failed restored output validation is explicitly a negative qualification, not a pass.
- [x] Retain graph replay/recapture, output validation, failures and fallback evidence.
- [x] Separate image/init/restore/first-request clocks and storage overhead; do not hide slower restores.
- [x] Clean up only task-owned resources and verify GPU memory is released.
- [x] Write result.json/report.md and update this card with resource disposition.
- [x] Manager accepts/integrates the measured negative result; no automatic promotion.

## Plan

Finish active model matrix, freeze candidate source, run isolated donor/restore/control experiments, record conclusions and clean up.

## Current Status

Completed, awaiting manager review. Worker `/root/bioir_openfold` used only authorized node `computeinstance-e00j20a9hkb508cn4a`, after core-lane cleanup and an idle-capacity check. Collaboration session, not a tmux task-deck session. No production routes, nodes, drivers or security-policy changes. Final allocation/process/memory receipts show the GPU released.

## Live Activity

- Final 23:19 UTC: all eight GPU pods and the CPU-only inventory pod removed. Bundle/PVC inventory fingerprinted 9,173,344,679 bytes across 2,356 files. Only `fs2-bioir-of3-snapshot-app`, `fs2-bioir-of3-snapshot-source`, `fs2-bioir-of3-checkpoints` and backing PV `pvc-16d2d25a-6319-4ecc-be38-96d19c12ae28` deleted; namespace retained. Final node healthy, no GPU allocations/processes, 0 MiB used / 0% utilization. Raw outputs/logs/manifests/hashes retained; deleted temporary checkpoint pages require recapture. No commits or pushes.
- 23:16 UTC: fixed matrix complete. Three low-level restores ready, none produce valid inference; all three normal controls pass warmup then fail the reused graph key. Seven valid artifacts / 25 inference attempts overall; six first-fault process events plus twelve poisoned-context follow-ons. Incompatible-identity normal-load fallback passes three distinct keys and seven expected HTTP400 negative probes. Median container-to-ready 292.779 s restore versus 65.349 s normal; no restored output parity or valid-result startup benefit. All eight GPU pods removed. CPU-only read-only bundle hashing precedes exact ConfigMap/PVC deletion and final GPU-memory audit.
- 23:07 UTC: all three fresh restores reached readiness but failed real inference. The first two ordinary-load controls pass initial warmup, then the same key fails with CUDA illegal memory access and follow-ons see a poisoned context. This reproduces without restoration, so graph serving itself is on HOLD; the core varied-ID timings are potential, not deployment qualification. Normal control 3 is active on healthy j20; only planned fallback and cleanup follow. No new runtime variant or tuning.
- 22:52 UTC: restore 1 low-level CUDA+CRIU passed and reported ready after292.58s, but all three requests returned500. Initial restored prediction hit CUDA illegal memory access; subsequent requests are poisoned-context follow-ons, not independent faults. Node stayed healthy and restored pod was collected/deleted. Planned restore2 is active; fixed3+3+fallback matrix only, no reset/invalidation tuning or extra retries. Application snapshot qualification is currently failed, regardless health200.
- 22:41 UTC: donor warmup valid, actual diffusion graph verified with no eager fallback. Capture passed: CUDA checkpoint1.148s, CRIU dump4.525s, durable flush290.634s. Donor deleted before `fs2-bioir-of3-restore-1` creation. Full three-restore/three-normal/fallback matrix now active with node-Ready guard; node failure stops and escalates, no host changes or unplanned retries.
- 22:32 UTC: core312-output matrix completed and both GPUs proven empty. Starting `fs2-bioir-snapshot/fs2-bioir-of3-donor` on j20 H100, own PVC `fs2-bioir-of3-checkpoints`, exact frozen graph+resident/native-RNG app and helper source ConfigMaps.
- Initial client-side apply exceeded annotation size with full public A3M; no GPU was allocated by that failure. Direct immutable ConfigMap creation fixes the harness without dropping MSA content; failure retained in inventory.
- Own `snapshot/openfold3` control/request/matrix wrappers prepared; shared snapshot helpers remain unchanged. Candidate is FP32 native-RNG diffusion-graph worker, subject to completed H100 output checks. No snapshot capture has yet been claimed.
- Planned same-node fresh donor delete, three fresh restores, three identical-harness normal starts, three real shapes including full MSA, and incompatible-identity normal-load fallback.
## Work Log

- 2026-09-15: created to execute independent snapshot branches in parallel.

## Test Evidence

`snapshot/openfold3/result.json` is complete, status `measured_negative`; `report.md` separates low-level restore success, failed application inference, absent startup benefit/output parity, and untested cross-node portability. Fixed 3 restore + 3 normal + fallback matrix: 7/25 valid inference artifacts; six first-fault process events and twelve poisoned follow-ons. Three normal warmups pass, all reused-key measured requests fail; no restoration-only causal attribution. Three distinct fallback keys pass and seven unsupported-feature probes return expected400. Allocation 2,353.040 GPU-seconds including initialization, capture, failures, collection and idle gaps. Exact image/checkpoint/source/GPU/driver bindings, init and request timings, actual CUDA+CRIU phases, node conditions, bundle SHA256 inventory and cleanup receipts retained. No historical native snapshot measurement is relabeled as fresh BIR evidence.

## Deployment Notes

Evaluation only. No automatic adoption.

## Manager Acceptance — 2026-09-15

The bounded evaluation is accepted, including negative and unsupported results. Final report and hash-bound acceptance are in `k8s-inference/acceptance/bioir-20260915/report/`. One final consistency review resolved stale aggregate/cleanup wording without new experiments. All 12 model results and four snapshot-model studies are accounted for; no candidate was promoted. All task pods/PVCs/ConfigMaps are absent, and all 27 model deployments have desired replicas ready in the final receipt. Historical capacity interruptions remain documented; current node state is separate from earlier observations. Publication receipt follows after Git push.

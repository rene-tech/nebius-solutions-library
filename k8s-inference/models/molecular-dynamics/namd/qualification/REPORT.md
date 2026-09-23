# Native NAMD qualification — 23 September 2026

In progress; **not customer-ready**. Hosted REST/MCP, tenant artifact delivery,
L40S, full repeated performance cohorts and persistent GPU snapshots are separate
gates. A native restart is not a GPU process-memory snapshot.

## Exact identity and protocol

- Worker source: `ca59d2fcd`; qualification tooling continued in `203e6ee4a`.
- Candidate r4: `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/namd-worker@sha256:1af3abab5c794c38ef19d557bd5ca0e754fd078343f36b7e41a9eeee2ac7354f`.
- NVIDIA amd64 engine: `nvcr.io/nvidia/namd@sha256:e1ebab672b968e0b287ba91c3dc19cdad9b693e8b59e74d7782cb753d6a7460f`, NAMD 3.0.2, CUDA 12.9, Charm++ 8.0.0.
- `namd3` SHA256: `291a5c63e159b668913453c9569c80b4d19445d711d48652322c7d8152254f3f`.
- H100 80GB HBM3, compute 9.0, driver 580.173.02, UUID `GPU-e8da8985-f0cc-43f6-9a6e-78f4ababcd10`.
- Pool `h100-ondemand-1x`, node `computeinstance-e00rwacqdngp6accx6`, one GPU,
  8 allocated vCPU, 32 GiB RAM, 64 GiB bounded scratch; native `+p4 +devices 0`.
- Public UIUC ApoA1 (92,224 atoms) and STMV fixtures, native force-field files;
  2 fs timestep, source MTS settings, 1,000 minimization steps and 20,000 NPT
  equilibration steps, then finite production. DCD every 10,000, XST every 1,000,
  native restart every 10,000 steps. These are output-enabled protocols, not the
  original output-suppressed `benchmarkTime` screen.
- Explicit qualification seed schedule: 314159 for native minimization;
  `314159 + fs2_first_step` for managed segments. Performance repetitions reuse
  the same schedule and are not an independent scientific ensemble. Native RNG
  state is not serialized and stochastic continuation is not bitwise.
- No MPS daemon is started. Every reported native throughput is single trajectory;
  multi-process aggregate NVIDIA MPS throughput is a different experiment.

Raw artifacts, immutable input bundles, native logs, DCD/XST, coordinate/velocity/
cell/bias checkpoints, inventory hashes and 1-second GPU telemetry are retained at
`/home/tux/namd-r20260923-evidence/`. `runtime_receipt.py` binds each actual result
file and scientific validator hash to the exact running image and GPU. An initial
receipt can explicitly choose one repetition per case for hosted acceptance;
that does not complete the planned three-repetition performance cohort.

## Initial final-image cohort and native recovery

The exact r4 image passed one 400 ps production each of ApoA1 NVE, NPT and
ungridded radius metadynamics, each retaining 20 finite/readable DCD frames.
The first native throughput confirmations were 289.67, 215.57 and 190.64 ns/day,
respectively. NVE maximum relative total-energy deviation was 0.0345%; NPT mean
temperature was 299.29 K with volume 885,821–893,238 Å³. These finite-run
observations do not assert ensemble or free-energy convergence.

The source Pod was deliberately removed while native dynamics had advanced past
timestep 137,000. Its externally preserved, inventory-verified checkpoint was
generation 4 at timestep 121,000. A new Pod (different UID) restored that checkpoint,
including the original 100 bias hills, and replayed the uncommitted segment to
221,000. Original and native loaded bias states were byte-identical before
advancing. The final state retained all old hills, grew to 200 hills and retained
all 20 readable DCD frames. The lost uncommitted computation is not hidden.

- Source Pod UID: `7777cdf5-a6a5-4395-b704-b2ead051c3d1`.
- Fresh Pod UID: `d7f35d0b-0ed2-4c69-a622-babe5b793627`.
- Checkpoint archive SHA256: `5b825bb0aeaea026d1a85391ce74fbb5b0705bfc146ae55f039f595a75480436`.
- Original/loaded bias SHA256: `43363b7c5f25d48d8a3497b02e7680582d969e901595544b6e4c5c94a50a541e`.
- Restored result SHA256: `ec6580dbcf17e127bd614c9d35dec175d5b8184fb40ee4e039c4708c1e491d12`.
- Recovery validator SHA256: `758c17ef0dfbdb5e48a15eb828ae546c911d822dd3751e701e164a3943518839`.

Combined `runtime-receipt-r4-hosted-input.json` SHA256
`330cd0a2bec61c5352a1d73207e434b3ea2cc95412d57545cc4f7060888ef248`
contains four flat passed test rows, exact GPU/image identity, real result and
validator hashes, and raw evidence paths. `customer_ready:false`,
`benchmark_cohort_complete:false` and `gpu_snapshot_qualified:false` are explicit.
The checkpoint was copied through the local companion-handshake harness; this
does not qualify the hosted Object Storage transport. Neither GPU process memory
nor PRNG state was serialized. A complete repeated H100 cohort is now running in
the fresh Pod, with additional preparation, GPU-offload and STMV cases.

## Scientific failure found and repaired

All three r3 ungridded radius-metadynamics runs returned native exit zero and
correct coordinate/cell/bias timesteps, but FAILED scientific history validation:
the second process logged that it read zero explicit hills and its final bias
state contained only the new 100 hills, losing the preceding 100.

The shipped Colvars version is `2024-06-04 (patch 1)`. Its ungridded state writer
omits a `keepHills` state marker that the reader uses when deciding whether to
retain old explicit hills. The public Colvars implementation's
[state-reader condition](https://github.com/Colvars/colvars/blob/6299b82c2abb805de1cc2e89403bc8b19a18f6ff/src/colvarbias_meta.cpp)
is consistent with the observed behavior. This is not evidence that NAMD 3.0.3
fixes this issue. No vendor binary was patched or replaced.

The version-scoped r4 compatibility adapter preserves the original state bytes
and creates a separately inventoried restart copy containing only the missing
state marker. It does not change `useGrids`, the native scientific configuration,
seeds or bias parameters. After native startup, `cv savetostring` captures the
loaded state without changing output prefixes. Every explicit hill's step,
weight, center and width is checked before resumed dynamics may advance.
No-grid, grid, empty, already-marked and unsupported-state regressions are covered.

The first substantive r4 ungridded run completed 200,000 production steps (400 ps):
100 old hills at timestep 121,000 were restored, the native round-trip state was
byte-identical to the original, and the final 200 hills at 221,000 preserved the
complete earlier prefix. All 20 DCD frames and 202 Colvars records were finite and
readable. Native throughput was 190.64 ns/day. This is functional bias-state and
trajectory evidence, not a converged free-energy calculation.

Evidence retained separately:

- Failed r3: `campaign-apoa1-colvars-r3-failed-history/validation.json`, SHA256
  `6897f43cc97980475b2e1a2c796d624e9cee5348df2260af921c923d2f11d1ad` (0/3 scientific successes).
- Repaired r4 first result: `campaign-apoa1-colvars-r4/rep-1/result.json`, SHA256
  `3884ae40801b81fb27c9628dabacfff75e715b08060a5391d05f8bb628054e16`.
- Its initial validator: SHA256
  `c10e782a418cf0dd28bfe998d8bacc89a2ef9b4dd1bbe2a4fb0c570f25b02600`.
- Earlier r2 ApoA1 NVE: three 400 ps productions, median 290.44 ns/day,
  289.97–290.49 range, maximum relative total-energy deviation 0.0335–0.0373%.
  These are historical r2 results, not final-r4 release evidence.

## Startup, resources and interpretation

Observed r4 image pull took 2.028 s on an existing node with cached NVIDIA base
layers. Container started at 14:30:13 UTC and the qualification process at
14:30:15 UTC. Native first-energy-log observation was 1.2–1.3 s per process.
These boundaries exclude customer API queueing, new-node provision, uncached
base download and tenant Object Storage transfer; no full cold-start claim.

The repaired first Colvars job took 209.11 s wall time, versus 207.61 s summed
native process wall time. The remainder includes input unpacking, closed-file
inventory/hash and checkpoint bookkeeping. Production used 730.64 CPU-user
seconds over 184.18 s process wall, approximately four cores. Whole-job 1-second
telemetry averaged 87.9% GPU utilization, 323 W and 1,013 MiB device memory.
All-stage telemetry is not mislabeled steady-state production-only telemetry.
The resulting workspace inventory was 117,119,541 bytes.

The validator now explicitly distinguishes the native timing-tail rate (median
of each process's last ten `TIMING` wall seconds/step), production-process rate
(including process startup/shutdown), and local-workflow rate (including
preparation, equilibration, input extraction and local checkpoint bookkeeping).
The local-workflow boundary excludes cloud queueing, Pod pull/scheduling,
Object Storage transfer and the initial local input-bundle copy. Raw process
timing and total job timing remain available; no MPS aggregate is inferred.

The complete r4 H100 ApoA1 offload cohort passed all three 200 ps productions:
30 DCD frames read, matching 121,000-step final checkpoints, median 44.09 ns/day
(43.98–44.26), maximum relative total-energy deviations 0.0194–0.0284%. Each
whole job took approximately 499–501 seconds. Whole-workflow GPU utilization
averaged 18.5–18.7% at approximately 168 W, consistent with the much greater
CPU-side work of offload mode at the selected four-thread shape. This is a
measured shape-specific comparison, not an architectural peak-throughput claim.

## Remaining boundaries

The initial ordinary hosted three-job NVE request failed before NAMD started:
the shared completion runner selected `python`, while this image provides
`python3`. Kubernetes OCI startup events identify the executable lookup failure;
the native worker and checkpoint protocol were not reached. The release owner
was given a shared-adapter fix that keeps the r4 worker image unchanged. Failed
operation `39460065-edbb-4eaa-a316-f3d48ded823f` and its retained events are kept
separate from native scientific passes. No customer-path pass is claimed yet.

The exact-r4 SPDX SBOM contains 188 packages (Syft 1.43.0). Trivy 0.70.0 with
the 23 September 2026 01:09 UTC vulnerability database reports zero critical,
11 high package findings (two unique CVEs), 236 medium and 75 low. The high
findings are inherited GnuPG 2.4.4-2ubuntu17.3 packages (fixed in 2.4.4-2ubuntu17.4)
and OpenSSL/libssl3t64 3.0.13-0ubuntu3.6 (fixed in 3.0.13-0ubuntu3.11). These are package findings,
not demonstrated application exploitability or an approved exception. Per the
release owner's instruction, they are documented without opening a hardening
lane or silently rebuilding the scientifically tested image.

- `worker-r4.sbom.spdx.json` SHA256:
  `412194f65dee0b9d38496f2d16e69fee6495a53a4068e33b302a3d20589cc49e`.
- `worker-r4.trivy.json` SHA256:
  `461487fe77f77cac7d8b4154c47b2d8eece217cced9ea58aa0d984486fff1de2`.

NAMD 3.0.3 fixes for extended-Lagrangian spinAngle and GBIS GPU-offload behavior
are absent from this NVIDIA artifact; affected modes remain unavailable. The
guards do not qualify arbitrary dynamic Tcl or all other Colvars algorithms.
The present profile is single-node multicore CUDA, not multi-node Charm++/MPI.
Preparation uses the bundled psfgen only; no separately gated VMD or proprietary
source/binary distribution was silently introduced. Operator-confirmed NVIDIA
agreement coverage is recorded without claiming independent contract review.

Only task-owned registry probes, mirror Job and temporary mirror credential copy
were deleted after preserving receipts. The regional mirror and qualification
artifacts remain available. Customer workloads, quotas and global GPU settings
were not changed.

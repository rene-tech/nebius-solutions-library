# Native NAMD qualification — 23 September 2026

In progress; **not customer-ready**. Hosted REST/MCP, artifact delivery, exact-pool
native protocols, full repeated performance cohorts and persistent GPU snapshots
are separate gates. A native restart is not a GPU process-memory snapshot.

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

The first final-image STMV NVE repetition also passed: 1,066,628 atoms, 100 ps
production in two continued segments, five finite DCD frames, 71,000-step final
checkpoint and maximum relative total-energy deviation 0.0090%. Native timing
was 31.38 ns/day; including production process startup/shutdown gives 29.14
ns/day, while the 524.24 s full local workflow gives 16.48 ns/day of production.
Its 83-file/1,333,635,918-byte inventory independently matched the request,
result hashes and every recorded file hash. The complete STMV NVE cohort then
passed all three repetitions: median 31.376 ns/day (31.261–31.430), 15 finite
DCD frames total, maximum relative energy deviations 0.00868–0.00949%, and
523.2–524.2 s complete local workflows. Whole-workflow GPU utilization averaged
80.4–80.8% and power 457.9–459.1 W. NPT and further resident/bias repetitions
continue separately; no ensemble convergence is asserted.

The released GROMACS L40S node was reused with explicit parent authorization
after a fresh zero-GPU-allocation check. The additional bounded worker is
`fs2-namd-r20260923-l40s-worker`, UID `def31fbe-1477-4da6-8905-a852837a9e3e`, on
`computeinstance-e00krzha55t0sg3t56`, pool `l40s-1x`. GPU is NVIDIA L40S,
driver 580.173.02; exact r4 image and immutable H100 fixtures are unchanged.
This first Pod started 10 s after creation; Kubernetes reported 9.156 s image
pull for 586,744,068 image bytes. Existing-node base-layer cache status is not
known, so this is not called an uncached-node cold start. The first L40S NVE
400 ps production passed with 20 finite DCD frames, a matching 221,000-step
checkpoint and 216.02 ns/day native timing. Its independent 78-file artifact
audit passed; result SHA256 is
`276cc728f61a7221f2ec44e09d36201ada75ed0f0d7bdc7131cda84b3cc17ab7`.
The initial L40S NPT and repaired ungridded-bias 400 ps cases also passed:
155.98 and 132.59 ns/day, respectively, each with 20 finite DCD frames and a
221,000-step checkpoint. Bias restored all 100 previous hills before advancing
to 200; original/native-loaded state bytes matched at SHA256
`595818463ebb387b2c210119b8ac58cba8356e678a85070e097a3a667657929d`.
All native output inventories passed the independent downloaded-file auditor.
The three-case `runtime-receipt-r4-l40s-initial.json` SHA256 is
`645683a49f0eb49fdcc03a2255247a32f01920a602f7f42b472e3acb7fccb174`.
Its chosen initial cohort is passed and the original receipt remains unchanged.
The subsequent full L40S core cohort passed nine runs: three 400 ps repetitions
each of NVE, NPT and repaired ungridded metadynamics. All 180 DCD frames were
finite/readable, final steps were 221,000, and every original input member and
native inventory hash passed independent checks. Native medians were 215.368,
155.982 and 132.804 ns/day, respectively. All three bias runs restored 100 old
hills and retained their complete prefix in the final 200 hills. The nine-test
`runtime-receipt-r4-l40s-core.json` SHA256 is
`3d11ecd8e6c1fb46bf8799162c1d05fbbaadf129c4f5c442e7178b484c85cc16`.
Its chosen repeated core cohort is complete; matched grid, preparation, offload
and STMV controls continue. No hosted L40S or full customer-readiness claim.

The binary was independently inspected with local CUDA 12.8.90 `cuobjdump`
without changing either worker. The copied bytes match the recorded `namd3`
SHA256. `namd3-r4-binary-audit.json` retains full target lists and SM86/SM89/SM90
symbol tables: 24 ELF modules each for SM80/86/90/100/103/120, plus six library
modules each for SM50/60/70/75/89/101/121. The selected NAMD bonded/nonbonded
symbol markers occur in SM86 and SM90 (213 entries each), not SM89. SM89's 116
entry symbols include 111 cuRAND markers. A blanket claim that this binary has
no SM89 code would therefore be incorrect; so would treating its library code
as proof of SM89-specialized NAMD force kernels. This is a compiled-code
inventory, not a profiler proof of live dispatch.

Read-only environment records are retained inside each packaged-runtime evidence
directory. Both nodes run driver 580.173.02/kernel 6.11.0-1016-nvidia and expose
16 VM CPUs, but the H100 host uses Xeon Platinum 8468 while L40S uses Xeon Gold
6338. Each Pod reserves eight vCPUs and native runs use four Charm++ threads.
Cross-pool results therefore compare these complete tested node shapes, not an
isolated GPU-only hardware ratio. Neither clocks nor power limits were changed.

## Remaining boundaries

The initial ordinary hosted three-job NVE request failed before NAMD started:
the shared completion runner selected `python`, while this image provides
`python3`. Kubernetes OCI startup events identify the executable lookup failure;
the native worker and checkpoint protocol were not reached. The release owner
was given a shared-adapter fix that keeps the r4 worker image unchanged. Failed
operation `39460065-edbb-4eaa-a316-f3d48ded823f` and its retained events are kept
separate from subsequent passes.

After the shared launcher fix, the release owner downloaded and hash-verified
237 artifacts from the ordinary public MCP/customer-client NVE batch. Independent
native output auditing passed all three requested jobs, all 234 native files
(350,668,887 bytes), all 60 DCD frames and the exact 221,000-step final checkpoints.
The three 400 ps NVE maximum relative energy deviations were 0.0293%, 0.0352%
and 0.0423%; native median was 290.275 ns/day (289.736–290.390). These are native
single-trajectory timings, not cloud end-to-end or aggregate throughput.
Downloaded evidence is retained in
`/home/tux/secure-handoff/fs2-md-engines-20260923/namd-hosted-nve-02-materialized/`.
Its `hosted-validation-receipt.json` SHA256 is
`9b5320a27b3d8c99c73b5ef01efd87f11e1040a4ecccda3d72e30d063eb09e05`.
The receipt explicitly leaves credential, submitted-input binding, public API,
materialization and exact worker-image provenance to the parent client/release
receipt; downloaded native files alone do not prove those properties. This
NVE output pass does not qualify all hosted protocols or full customer readiness.
An additional, separately retained audit also compared every immutable input
member against the original compressed fixture: all 15 files/20,781,382 input
bytes matched in each downloaded job. No archive was extracted or executed by
this auditor. `hosted-validation-inputs-receipt.json` SHA256 is
`6e5280af34c468c76010e23a4431431e6d660ba76d19aeb01d016817b5e2df38`;
the earlier receipt and its referenced audit files remain unchanged.

The subsequent ordinary hosted NPT batch also passed all three requested 400 ps
jobs and all 237 downloaded artifacts. Independent validation covered 234 native
files, every immutable input member, 60 finite DCD frames, paired 221,000-step
checkpoints, mean temperatures 299.29–299.41 K and finite variable volumes.
Native median was 216.287 ns/day (215.949–216.424). Operation
`09c69586-3b70-41cb-86c0-bb9559a2b5d8` used the same scientific fixture and the
explicit transport-only `platform-artifacts` override; its exact derived request
SHA256 is `2809707a5485b8e2a0bf4cb01a337128fadcfd6438de40962ff7cc4d1ef450e6`.
The output validation receipt is
`/home/tux/secure-handoff/fs2-md-engines-20260923/hosted-namd-npt-01-materialized/hosted-validation-receipt.json`,
SHA256 `c9a205bf8afe4019af56135ad8a741ba0a04fa5bf9ea85edcfc84e72214e352e`.
Filtered runtime-provenance snapshots in the client receipt directory cover
all three exact-r4 H100 workers, driver 580.173.02, without recording secrets or
native payloads. The recorded collector/backend image was
`sha256:3f4408c3ae860fba456d51ad6af8a2294166d5964bb89c4fcb06bdbc360b5050`.
Hosted Colvars is a separate pending cohort; NPT does not qualify it.

## Native warning review

No warning was suppressed and no source scientific setting was silently changed.
`warning-inventory-r4-01.json` records 84 native logs across completed H100/L40S
and hosted cohorts, their exact hashes, ten distinct warning lines and zero
unclassified warning text. SHA256:
`a47637263a1202683fe5954556d90d0c70484c53d6e9eb0738398fb434d9b54c`.
This is an inventory with explicit interpretation, not automatic release approval.

- Deprecated aliases (`1-4scaling`, `CUDASOAintegrate`, `DeviceMigration`,
  `CUDAForceTable`) are present in the unchanged native benchmark configuration;
  their replacements are identified by the native messages.
- `GPUAtomMigration on` and `GPUForceTable off` are explicit experimental
  benchmark options. The latter uses direct non-PME-step calculations and does
  not support every force variant. They remain experimental: our finite-run
  energy/trajectory checks do not establish long-timescale conservation or
  qualify other force variants. See the [native GPU option documentation](https://www.ks.uiuc.edu/Research/namd/3.0/ug/node102.html).
- The two Langevin warning lines report differing per-particle damping and
  extra rigid-bond work. These inputs deliberately specify
  `langevinHydrogen off`, leaving hydrogens uncoupled while other atoms are
  thermostatted; no thermostat parameter was rewritten to silence the warning.
  This interpretation follows the [native Langevin settings](https://www-s.ks.uiuc.edu/Research/namd/3.0/ug/node38.html).
- The GPU-resident lone-pair capability warning occurs before structure loading.
  Both actual benchmark PSFs have no `NUMLP` section; all 92,224 ApoA1 and
  1,066,628 STMV atom records were independently parsed and have mass at least
  1.008 amu (no zero-mass sites). The [public PSF reader](https://www.ks.uiuc.edu/Research/namd/doxygen/Molecule_8C_source.html)
  distinguishes absent/zero lone-pair hosts. This establishes that these listed
  fixtures do not request explicit lone-pair sites; it does not qualify arbitrary
  Tcl, lone-pair force fields or other native inputs.

## Other remaining boundaries

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

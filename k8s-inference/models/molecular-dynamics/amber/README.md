# AMBER26 private academic native runtime

PMEMD26, not an AmberTools substitute. The operator has confirmed the academic
agreement and supplied the official archive. Its SHA256 is
`0478ccce892f3525e995e9c85458d552c6060b73dd28acd03c366e61ecf23a14`;
published MD5 `ceeabc133e772c115183d5cf6a87676b` also matches.
Source, bundled test inputs, binaries and build logs remain private. Git contains
only the wrapper/build recipe and the permitted build-configuration patch.

The initial recipe builds native SM89 and SM90 CUDA binaries in both SPFP and
DPFP, plus CPU PMEMD, from pinned CUDA 12.8.1 Ubuntu 24.04 images and GNU13.
It retains GTI for later alchemical qualification, single-process execution,
upstream tests, exact package versions, CMake options and cubin inventories.
Only the architecture list is patched; no scientific source or precision is
silently changed. CPU build success does not establish GPU compatibility.

Build on the authorized private local builder, passing the already-acquired
archive as the sole `pmemd26.tar.bz2` file of a private directory through
`--build-context pmemd_source=/PRIVATE/archive-context` and using
`runtime/Containerfile` with this directory as context. The context allowlist
excludes all licensed inputs; the named context is read-only on the local private
builder. BuildKit secrets were unsuitable because their 500 KiB limit is below
the 333 MiB archive size; that failed attempt is retained. Do not push an image to a public registry or export
its test/source assets into this repository. Package versions are captured for
this bootstrap; rebuilding with changed distribution packages requires a new
image identity and qualification.

The license is not a general entitlement for arbitrary platform tenants.
Parent integration must restrict use to the authorized organization/license
scope and gate third-party use by their valid AMBER26 entitlement. There is no
new agreement question or access blocker for this confirmed internal build.

Proposed worker contract follows the shared native-MD bundle, ordered stages,
full stopped-workspace checkpoint and final-commit-before-result protocol. Native
mdin, topology, coordinates, restraints, reference coordinates and TI context
remain explicit files. PMEMD CUDA SPFP, CUDA DPFP and CPU are distinct selections;
typed filenames map to fixed native flags, not a new arbitrary shell API.
Clean stage exits are the initial checkpoint boundary. Native coordinate/restart
files alone do not imply exact thermostat, random-stream or alchemical state.
Signal-driven or wall-time continuation must be qualified against real PMEMD
before being enabled. GPU process snapshots are a separate capability.

Private engine artifacts now exist in
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform`:

| Artifact | Immutable SHA256 |
| --- | --- |
| PMEMD26 base | `744b8d42a846f93118d9288efed67546236cfafd2f7d2377e5c99bc7b9a6dcca` |
| Combined `amber26-engine`, PMEMD plus locked166-package AmberTools26 | `cea619dcb5f8a8577a17edd7ce0fdd70e6718838f9d5af47d7731ca8388fab48` |
| Candidate `amber-worker` v2, source `a31877583` | `ee61f03b0d9a4161ec33f383ffa57b7d6097068237101d671a5194e2bcb633c5` |
| Candidate `amber-worker` v3, source `3a486b5f01eb7d8efda9972f692c46017cb5becc` | `1ca8115d4b4f899a282e449e05c59c1e6a6897fbb3ab8f889965ec73677d11fc` |

`ENGINE_ID` binds the actual combined OCI bundle, not the licensed source-archive
hash. The source hash is separate provenance. Tool subprocesses use
`AMBERHOME=/opt/ambertools`, isolated tool PATH/library paths; PMEMD uses
`/opt/amber26`. Both precisions contain native SM89/SM90 code. Actual GPU
execution, not cubin inspection alone, determines supported pools.

The v3 typed contract is `pmemd`, `tleap`, `cpptraj`, `parmed`, `antechamber`,
`parmchk2`, and `mmpbsa`. Native file inputs, bounded explicit flags, and expected
outputs are used throughout, without adding a generic shell-command API.
PMEMD dynamics requires an exact `expected_nsteps`; native echoed `nstlim`,
final step, finite restart coordinates/velocities, atom count and time are checked.
The real CUDA final summary differs from older saved CPU logs; v1's overly
narrow completion parser was corrected in v2, with the original failed wrapper
receipt and successful native output retained.33 offline tests pass. Native
closed-stage checkpoints are not partial-restart or GPU-process snapshots.

H100 upstream comparisons on v1, v2 and the exact final v3 pass13/14 without
changing upstream tolerances. The sole exception is sodium TI NVE SPFP:
four printed temperature fields differ by0.01K, maximum relative2.83e-5,
against the original1e-5 gate. The same DPFP test passes1e-7, and SPFP softcore
MBAR passes its own upstream gate. The entire upstream cohort remains **failed**,
not relabeled passed. These short regressions do not establish sustained
throughput or free-energy convergence. Larger explicit/implicit SPFP and
TI-DPFP sustained workflows are separate qualified cohorts when completed.

The full preparation example uses actual LEaP ff19SB/OPC, ParmEd, minimization,
heating, density equilibration, production and CPPTRAJ. Its original10Å-padded
box hits PMEMD CUDA's fixed neighbor-cell-grid limit during NPT density
relaxation; the original and shorter-stage retry remain failed evidence.
An explicitly new15Å-buffer fixture passed on exact v2, including the actual
LEaP/ParmEd preparation, minimization,100ps heating,100ps density equilibration,
2ns production and full CPPTRAJ analysis. That is a changed box,
not a matched performance improvement or a silent alteration of the original.
The engine recommends explicit closed restarts or CPU density equilibration;
the worker never treats the partial grid-error output as success.

The v3 native tool extension explicitly specifies molecular input format,
AM1-BCC charge/multiplicity, GAFF or GAFF2, charge equivalence and a charge-sum
assertion. It never silently alters residual charges. Each Antechamber stage
has its own directory, retaining SQM and parameterization intermediates.
MMPBSA has explicit topology/trajectory mappings and exact analyzed-frame
assertions. Native `use_mdins` requires the complete generated context, including
the distinct PB ligand `.intermediate_pb.mdin2`, when applicable. A missing-file
fixture failure remains retained; package/help presence is not qualification.
The synthetic benzene path completed actual AM1-BCC/SQM, GAFF2, LEaP and ParmEd
with zero total-charge residual. Official Sustiva output has a-0.002e residual
and fails the default0.001e gate; no charge renormalization is performed.
On identical published Ras–Raf Cartesian inputs, current SANDER GB reproduced
all18 component/energy-term comparisons across five frames at printed precision.
Realignment and ASCII rounding in newer CPPTRAJ outputs are not an identical-
coordinate accuracy control; that earlier failed comparison remains separate.
The complete-context exact-v3 typed workflow passed all five stages, both GB/PB
models and all eight component tables with exactly five finite frames each.
Every original input hash remained unchanged; per-frame binding subtraction
and energy totals agree within1.4e-12kcal/mol. This carries no entropy correction,
force-field suitability, binding-affinity or convergence conclusion inferred.
CPPTRAJ's packaged build lacks HDF5 compression; classic NetCDF is tested.

Exact v2 receipts in `qualification/receipts/` cover nine H100 sustained
classical/TI cases, preparation and distinct fresh-Pod closed-stage recovery.
The selected sustained TI control samples lambda0.3 and reports MBAR neighbors
0.2/0.3/0.4. Original full0..1 eleven-state attempts all failed due to a printed
distant-lambda overflow; they are not accepted by dropping failed state values.
Six v2 L40S classical repetitions also passed. These are separate images from
v3: the final v3 executed sustained DHFR (0.6ns), GB8 (1.2ns) and nearby-state
TI-DPFP (100ps) on both H100 and L40S, one repetition per case/pool, all six
passing. The full ff19SB/OPC15Å preparation through2ns production also passed on
exact v3 H100. No old-image receipt or its repetition count is promoted as
new-image execution evidence. Native throughput is input- and precision-specific:
the nearby-state DPFP TI test is substantially slower on L40S than H100; it is
not silently switched to SPFP to improve performance.

The canonical four-engine acceptance is a separate ff14SB/TIP3P master with
6598 atoms, not the ff19SB/OPC preparation example. Native zero-optimization CPU
PMEMD on exact v3 preserves master coordinates exactly. With PME64³/order4,
1e-6 electrostatic target and all bonded terms enabled, the potential is
-18426.9425 kcal/mol; turning only analytic LJ tail off gives-18358.2862,
an actual tail contribution of-68.6563 kcal/mol. Both native outputs and the
original legacy-field-name validator failure are retained. Production uses
the documented single-GPU Langevin LFMiddle and stochastic cell-rescaling
barostat (`ischeme=1,ithermostat=1,therm_par=1,barostat=1,baro_stochastic=1`),
not plain Berendsen. It computes molecular-virial pressure and retains matching
coordinate/velocity frames every1ps. Monte Carlo's placeholder PRESS=0 in older
fixtures is never interpreted as measured pressure.

The canonical default1Å CUDA neighbor margin failed during initial NPT box
contraction; that failed attempt is preserved. The separately frozen `skin2`
fixture sets `skinnb=2.0,skin_permit=0.5` with the same10Å physical cutoff,
force field, seeds, tolerances, durations and full native small-box guard.
It is not a relaxed neighbor check or an altered thermodynamic protocol.
On H100, the exact-v3 `skin2` run completed minimization,100ps NVT,100ps NPT
and1ns production after deliberate active-production interruption and distinct
Pod replacement. The closed generation5 preserved all preceding stages;
production was retried from that acknowledged NPT boundary, not resumed as an
exact random-stream/GPU-process snapshot. All6598 master atoms and original
input bytes match; production has1000 finite1ps frames and matching velocities.
Genuine molecular-virial pressure has1000 samples, mean-12.3712bar and sample
SD268.1900bar; these fluctuations do not establish long-run pressure convergence.
Native H100 production was729.853ns/day for this single run. The L40S canonical
continuation and common four-engine ensemble analysis are separate gates.

Private raw evidence lives under
`/home/tux/secure-handoff/fs2-pmemd26-build.FtlWsT/`. Source, native inputs,
upstream reference data, failed attempts and full logs remain private; do not
copy them into Git. Current status is qualification in progress, not a public
App, fully supported pool, customer-ready release or qualified GPU snapshot.

Sources: [official acquisition/license](https://ambermd.org/GetAmber.php),
[Ubuntu installation](https://ambermd.org/InstUbuntu.php),
[AMBER26 manual](https://ambermd.org/doc12/Amber26.pdf).

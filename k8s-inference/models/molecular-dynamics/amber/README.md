# AMBER26 private academic bootstrap

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

Status: build/bootstrap in progress; no runtime App, supported pool or customer
readiness is claimed. Explicit/implicit solvent, minimization, equilibration,
production, restart, TI/FEP, trajectory validation and sustained measurements
still require real-engine qualification.

Sources: [official acquisition/license](https://ambermd.org/GetAmber.php),
[Ubuntu installation](https://ambermd.org/InstUbuntu.php),
[AMBER26 manual](https://ambermd.org/doc12/Amber26.pdf).

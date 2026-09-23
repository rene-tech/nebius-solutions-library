# Reproducing the four-engine alanine case

The delivered case contains `case.json`, `inputs/<engine>/input.tar.gz`, the
matching `request.json`, complete native outputs, validation receipts, plots and
videos. Read its qualification status and result report before treating it as a
passed comparison. Prepared inputs alone are not a completed simulation.

## Dependencies and access

Use Python 3.11+ and Docker on the submitting machine. The hosted path needs an
ordinary Scientific AI API key with access to the requested App and artifact
storage, plus the deployment's MCP URL. Credentials are supplied by environment
or a private JSON file containing a `secret` field; they are never part of the
case archive. The operator must supply access to the immutable registry images.

Native reproduction additionally needs NVIDIA Container Toolkit and one
compatible GPU. These exact runtime builds were evaluated on H100 and L40S;
unavailable hardware, licences, registry access or executables are errors, not
permission to substitute another engine. AMBER uses the approved academic
PMEMD 26 build with AmberTools 26, **not an NVIDIA AMBER NIM**. NAMD and LAMMPS use
their NVIDIA HPC container runtimes. Exact versions/build identities and native
commands are preserved in each run's engine metadata, logs and `result.json`.

The original host had no native `gmx`, `namd3`, `pmemd` or `lmp` installation.
Engines therefore run inside the recorded containers. Preparation/conversion
used AmberTools 26, ParmEd 4.3.1 and pinned InterMol with the retained Python 3.12
configuration-parser compatibility patch. Analysis used MDAnalysis 2.10.0,
NumPy 1.26.4, SciPy 1.16.3, Matplotlib 3.10.7 and FFmpeg 6.1.1. Blender, VMD and
PyMOL were unavailable and were not claimed as used. See `inventory.py`, the
preparation/conversion receipts and `analysis/README.md` for the full inventory.

## Run any of the four engines

`run_case.py` verifies archived input hashes before execution and refuses to
overwrite another run. The same output directory is the resume boundary: it
reuses the original operation/idempotency key instead of duplicating GPU work.
First inspect a command without submitting it:

```bash
python3 run_case.py --case . --engine gromacs --mode hosted \
  --output ./reproduced/gromacs --describe
```

Configure the real MCP URL and API key through your shell's secret mechanism,
then run the four independent commands:

```bash
python3 run_case.py --case . --engine gromacs --mode hosted --output ./reproduced/gromacs
python3 run_case.py --case . --engine namd    --mode hosted --output ./reproduced/namd
python3 run_case.py --case . --engine amber   --mode hosted --output ./reproduced/amber
python3 run_case.py --case . --engine lammps  --mode hosted --output ./reproduced/lammps
```

Hosted mode reads `SCIENTIFIC_MODELS_MCP_URL` and `SCIENTIFIC_MODELS_API_KEY`.
Alternatively use `--mcp-url` and `--key-file /private/credentials.json`.
The commands can run concurrently within your authorized concurrency/capacity.
After a bounded 30-minute polling window a still-running operation is retained;
rerun the **same command/output directory** to continue observing it. A process
exit code alone is not a scientific validation. Successful receipts must show
`state=verified`; engine-specific duration, topology and trajectory validators
must also pass. Failed native diagnostics remain under `failed-attempt/` and
never become successful trajectory artifacts.

For native reproduction use the same engine command with `--mode native` and
one GPU index or UUID, for example:

```bash
python3 run_case.py --case . --engine amber --mode native --gpu 0 \
  --output ./native-reproduced/amber
```

Native mode runs the identical worker/image and request with local checkpoint
storage. It does not emulate cloud queuing, customer-bucket export, preemption
recovery or GPU-state restoration; those have separate platform receipts.
Preserve each output directory and its `.fs2` metadata when resuming. Do not run
two processes against the same native workspace concurrently.

## Shared physical specification

One canonical ACE–ALA–NME structure was built by AmberTools LEaP with ff14SB
and TIP3P. There are 6,598 atoms: 22 peptide atoms and 2,192 waters. The master
box is 44.537351 × 44.358839 × 44.629192 Å, orthogonal, with measured minimum peptide
clearance greater than 10 Å. The net charge is zero within stored-decimal
rounding. Every engine begins from this same unminimized master, not an
independently built solvent box.

| Setting | Value |
|---|---|
| Minimization | Up to 5,000 native minimization iterations; retain convergence/actual count |
| NVT | 50,000 steps = 100 ps at 300 K |
| NPT equilibration | 50,000 steps = 100 ps at 300 K, 1 bar |
| Production | 500,000 steps = 1 ns, NPT |
| Timestep | 2 fs |
| Coordinates/thermodynamic output | Every 500 steps = 1 ps |
| Cutoffs | 10 Å, no LJ switching or potential shift |
| Long-range electrostatics | PME, or LAMMPS PPPM; 64³ grid/order 4, requested tolerance 1e-5 |
| Static diagnostic tolerance | 1e-6; no minimization/integration/constraint projection |
| Constraints | Bonds involving H, rigid TIP3P; native constraint solvers disclosed |
| Langevin friction | 1/ps, all atoms |
| Seeds | NVT 20260923; NPT 20260924; production 20260925 |

The same integer seed does not make the engines' RNGs or trajectories
identical. Native Maxwell initialization occurs after independent minimization.
NPT uses each engine's native isotropic barostat; these algorithms are not
claimed identical. LAMMPS `units real` uses 0.9869232667160128 atm for 1 bar.
GROMACS LINCS (order 8, 2 iterations)/SETTLE and other engines' native SHAKE/rigid
water solvers target the same distances. All solver and barostat inputs remain
in the archives. Do not replace them with guessed defaults.

## Conversion and comparison are separate gates

NAMD and AMBER consume the canonical Amber topology. ParmEd writes GROMACS;
the pinned InterMol conversion produces LAMMPS. The audit re-reads actual
parameters: masses/types/charges, bonded potentials, LJ coefficients and
combining rules, exclusions, and SCEE 1.2/SCNB 2.0 (1–4 Coulomb 5/6, LJ 1/2).
Semantic negative controls prove that altered parameters are rejected.

Canonical TIP3P encodes OH/OH/HH harmonic bonds. The LAMMPS SHAKE adapter keeps
those force-field terms, adds a zero-energy HOH angle with the exact implied
104.49060265846° geometry, and constrains OH+angle. Native force/energy and all
constraint-distance checks cover this adapter. The optimized LAMMPS recipe
represents the zero-tilt cell as orthogonal before PPPM setup: no cell dimensions,
coordinates, topology, mesh, timestep or pressure target are changed.

There are small, measured engine numerical differences. Read
`analysis/STATIC-REPORT-20260923.md` for Coulomb constants, mesh convergence,
native table/precision limits and attractive-only versus full LJ-tail
conventions. Never hide those differences by silently rescaling charges.
AMBER's LFMiddle printed kinetic-temperature estimator differs from its saved
current-velocity estimator; both measurements and source-backed definitions
are retained in `analysis/TEMPERATURE-REPORT-20260923.md`.

## Analyze and render

Follow `analysis/README.md` with the retained common analysis specification.
Raw files remain untouched. The analysis unwraps the peptide, centers it and
uses one heavy-atom alignment reference and fixed camera for every engine.
Water oxygens are rendered as smaller transparent points within the disclosed
viewing radius. Every actual 1 ps frame is shown once at 40 fps, giving 25-second
clips and the synchronized 2×2 video. Nothing is interpolated or fabricated.

The report compares measured temperature, pressure, density, native throughput,
backbone φ/ψ time series and conformational distributions. Report queue/startup,
artifact I/O and end-to-end time separately from native ns/day. These four 1 ns
trajectories are not an equilibrium-convergence proof or a free-energy estimate.

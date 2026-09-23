# Four-engine alanine-dipeptide acceptance workload

Status: all four engines completed two full canonical hosted workflows and
native scientific validation. The final report, raw files, four clips, 2×2 video
and phi/psi plots are assembled in the full delivery bundle. Read
[the measured results and limitations](qualification/RESULTS-20260923.md).
Topology conversion and native numerical differences are checked separately;
this does not establish identical forces or converged ensembles. This is a
separate fixture from the earlier AMBER ff19SB/OPC preparation campaign.

The complete server bundle is
`/home/tux/fs2-alanine-comparison-20260923/delivery-02`. Runtime/client delivery
and workbench replacement have separate release receipts; native completion is
not evidence that a later API/UI release was deployed or passed.

The [complete archived-delivery receipt](../qualification/full-delivery-20260923/README.md),
[final-client recovery](../qualification/final-client-recovery-227/README.md),
[large-stream acceptance](../qualification/large-artifact-streaming-227/README.md)
and [actual admin UI checks](../qualification/admin-ui-reference-227/README.md)
all passed. The replacement workbench's separate
[customer-path report](https://github.com/rene-tech/serverless-ai-cookbook/blob/agent/scientific-ai-client-general-20260920/templates/hcls-librechat/MD_REPLACEMENT_RESULT_20260923.md)
records successful account/preview restoration but a blocked real-agent check:
the unchanged owner-selected GLM-5.3-Flash returns upstream HTTP404. GLM-5.2
passes diagnostic streamed tool calls, but changing the conversational default
awaits the owner's explicit choice. This does not invalidate the four native
scientific runs, and their success does not make the blocked client ready.

## Requested scientific protocol

One canonical capped alanine dipeptide (ACE–ALA–NME), prepared with AmberTools
using **Amber ff14SB and explicit TIP3P water**, in a periodic box with at least
1.0 nm peptide-to-boundary clearance. Preserve the master coordinates and atom
order where practical. Run the following independently with GROMACS, NAMD,
AMBER and LAMMPS:

1. Minimize.
2. Equilibrate 100 ps NVT at 300 K.
3. Equilibrate 100 ps NPT at 300 K and 1 bar.
4. Produce 1 ns using a 2 fs timestep, hydrogen-bond constraints, PME (PPPM for
   LAMMPS), matched cutoffs and an output frame every 1 ps.

Record every seed, exact image and software version, input hash, command and
relevant numerical setting. Match physical parameters; disclose differences in
thermostat/barostat and numerical implementations rather than asserting identical
algorithms when they are not available. The production ensemble must be stated
explicitly and kept consistent across engines.

## Gates, in order

- Inventory installed host/container programs, converters, ff14SB/TIP3P source
  files, analysis libraries and renderers before execution. Missing components
  are explicit dependencies, not a reason to fabricate results or substitute an
  engine, force field, or solvent.
- Build and hash one master topology and coordinate set. Record the source force
  field files, peptide composition, waters, box, clearance, charge and ordering.
- Convert using ParmEd and a validated converter where supported. NAMD may read
  the canonical AMBER topology directly. Re-read the actual generated inputs and
  compare atom types, masses, charges, bonded terms, LJ parameters and combining
  rules, exclusions, and AMBER SCEE/SCNB 1–4 scaling. Explicitly reject unsupported
  conversion features; successful execution is not equivalence evidence.
- Compare decomposed single-point potential energies on identical coordinates
  before dynamics. Separate finite precision, PME/PPPM tolerances, LJ cutoff and
  dispersion-correction differences from force-field conversion defects.
- Exercise the actual deployed platform under an ordinary authorized key,
  including input upload, durable operations, status, complete artifact download
  and customer bucket output. Keep failed operations and repair/retest defects.
- Validate all requested stage durations, step/frame counts and finite outputs.
  Compare ensemble statistics/conformational distributions, not frame-by-frame
  trajectories or a claim of convergence from 1 ns.

## Required deliverables

- Complete native inputs and runnable scripts for all four engines; the master
  topology/coordinates and converter provenance.
- Raw topology, trajectory, log, restart and analysis files, with checksums.
- Reproduction README and per-engine command/software/configuration manifest.
- Table: particle count, total charge, initial potential energy, mean production
  temperature/pressure/density and simulation performance. Report native ns/day
  separately from queue, startup, artifact I/O and end-to-end time.
- One short MP4 or GIF per trajectory and a synchronized, labeled 2×2 comparison
  video with identical representation, camera, frame interval and playback speed.
- Peptide backbone phi/psi time-series and distribution plots for all engines.
- Visualization must unwrap PBC jumps, center the peptide, apply a consistent
  alignment/reference, show peptide sticks/licorice and water as thin lines or
  transparent points. Preserve raw trajectories separately.

## Execution boundaries

Use the current Scientific AI Apps and pinned engine images. Prefer existing
available/preemptible single-GPU capacity; do not evict customer jobs or raise
quotas. A GPU snapshot of a running MD state is not a generic replica-independent
startup cache. Do not alter the force field, timestep, output interval or accuracy
just to produce an attractive benchmark. Missing dependencies or genuine external
blockers are reported by exact name. No new licence is accepted on the user's
behalf; the already-confirmed academic AMBER scope remains unchanged.

## Work ownership

Root: master preparation, topology conversion/equivalence, GROMACS/LAMMPS
integration, platform release and customer-path acceptance.
NAMD worker: final runtime fixes and NAMD native AMBER interpretation/execution.
AMBER worker: final runtime/tools qualification, then exact canonical AMBER case.
Snapshot/analysis worker: finish bounded snapshot evidence, then common analysis
and visualization. All final artifacts are bound to exact input/release hashes.

Primary references inspected before conversion:

- https://ambermd.org/AmberTools.php
- https://github.com/ParmEd/ParmEd
- https://github.com/shirtsgroup/InterMol
- https://docs.lammps.org/special_bonds.html
- https://docs.lammps.org/Howto_bioFF.html
- https://www.ks.uiuc.edu/Research/namd/3.0/ug/node13.html

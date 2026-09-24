# Real dense-motion GROMACS and AMBER continuations

Scope: 20 ps of **new native dynamics**, starting at each frozen `delivery-02`
final checkpoint. This does not recover unsaved sub-picosecond frames from the
old trajectories, interpolate coordinates or repeat equilibration/production.
Root owns rendering, labeling and upload. The frozen original delivery is never
modified.

Both use 10,000 steps at 2 fs with coordinates every 10 steps (20 fs): 1,000
nonzero samples over 20 ps, optionally preceded by the native boundary frame.
Canonical atom order, all 6,598 atoms and 6,588 constrained distances are checked.

GROMACS retains native SD/Langevin and C-rescale, LINCS 8/2 + SETTLE, fixed 64³
PME/order 4, 1 nm cutoffs and `-notunepme`. Only run length, output frequency and
explicit continuation bookkeeping change. `grompp -t source-final.cpt` supplies
full-precision state; typed `restart_checkpoint` also passes native `mdrun -cpi`
with `-noappend`. Native origin is step 500,000 at 1,000 ps, ending step 510,000
at 1,020 ps. No GPU bitwise-reproducibility claim. This follows the
[native checkpoint/changed-MDP continuation procedure](https://manual.gromacs.org/documentation/current/user-guide/managing-simulations.html#changing-mdp-options-for-a-restart).

AMBER retains exact PMEMD26 CUDA SPFP, LFMiddle/Langevin with SCR virial pressure,
64³/order 4 PME, 10 Å cutoff, SHAKE and the qualified 2 Å skin. `irest=1, ntx=5`
reads the final native NetCDF restart with positions, current velocities, cell
and time. The source's actual restart time is approximately 1,200 ps; a new
local NSTEP counter runs 1..10,000 to 1,220 ps. The preserved `ig=20260925`
initializes a new random stream; this is **not exact stochastic continuation**.
Native MDIN differs only in `nstlim` and `ntpr/ntwx/ntwv`. Original output
velocities, units and NetCDF scale factor remain preserved.

## Reproduce within the authorized scope

The helper's `prepare` subcommand requires a new output directory and verifies
each source against its frozen native result inventory. `preflight` reads live
normal-key identity/profiles and refuses different deployed runtime digests.
Use the already-qualified customer image
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d`
with `gromacs/qualification/run_image_customer_client.py`; it reads live typed
schemas, uploads immutable bundles, retains the durable operation identity and
downloads every verified artifact. Reuse the same receipt/idempotency key when
resuming observation; do not submit duplicate science. This qualifies that
typed SDK path, not the unrelated LibreChat chat default.

Materialize with the existing `materialize_customer_results.py`, then run:

```bash
python gromacs_amber.py validate --engine gromacs \
  --fixture /new-evidence/gromacs/fixture-01 \
  --workspace /new-evidence/gromacs/materialized-01/dense-gromacs \
  --customer /new-evidence/gromacs/customer-01 \
  --delivery /frozen/delivery-02 --output /new-evidence/gromacs/validation-01.json
```

Use the isolated existing analysis Python environment with NumPy, SciPy,
MDAnalysis and ParmEd. The validator checks all source/output hashes, exact
public/native identity, completed steps, finite periodic coordinates, 20 fs
native timestamps (float32 precision acknowledged), exact XTC integer steps,
constraint geometry and actual restart/native settings. AMBER has no stored
NetCDF step field; native MDOUT steps independently cross-check its cadence.
Its saved velocity stream must also contain 1,000 aligned finite frames.
Every failure remains in a new receipt; no tolerance changes are automatic.

The renderer receives the validation receipt's `renderer` object with native
format/path, exact origin, 2 fs timestep, 10-step cadence and identity atom map.
It must use only the 1,000 nonzero frames, without interpolation or smoothing.
These short continuations support a motion demonstration, not new equilibrium,
thermodynamic or performance conclusions. Native run and artifact timing remain
separate from GPU throughput.

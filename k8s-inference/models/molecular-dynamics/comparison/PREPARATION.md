# Recreating the canonical inputs

The delivered `master/`, `conversion/` and engine input archives are the actual
inputs used for acceptance. Reusing those immutable files is the simplest way
to reproduce the simulations. This page additionally explains how they were
prepared; rebuilding them is a **new input variant until hashes and parameters
have been compared**.

## Master system

`prepare_master.py` runs AmberTools LEaP from the pinned academic AMBER engine
image, not from an assumed host installation. It loads `leaprc.protein.ff14SB`
and `leaprc.water.tip3p`, builds `sequence { ACE ALA NME }`, solvates with TIP3P,
and records all resolved force-field file hashes. A rigid translation centers
the peptide; individual waters are moved only by integer periodic box vectors.
Atom ordering, bond parameters and forces are not changed by centering.

From the delivered case directory, using an already-authorized registry account:

```bash
mkdir prepared-again
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --entrypoint /opt/ambertools/bin/python \
  --mount "type=bind,src=$PWD/preparation,dst=/scripts,readonly" \
  --mount "type=bind,src=$PWD/prepared-again,dst=/output" \
  cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/amber26-engine@sha256:cea619dcb5f8a8577a17edd7ce0fdd70e6718838f9d5af47d7731ca8388fab48 \
  /scripts/prepare_master.py --output /output/master
```

No GPU is required for LEaP preparation. If that licensed/private runtime cannot
be pulled, report the unavailable image or authorization; do not silently use
another force field or engine. The generated inputs, not the runtime executable,
are included in the case bundle.

## Conversion and parameter audit

Preparation used Python 3.12, ParmEd 4.3.1, NumPy 1.26.4, SciPy 1.16.3,
NetworkX 3.5, decorator 5.2.1 and six 1.17.0; the independent parameter audit also
requires OpenMM 8.3.1 (for topology interpretation, not replacement dynamics).
InterMol is pinned to revision
`7125764d42f6e6c589dc2d1df71e3e812b3a7b27`. Its retained patch replaces two
removed Python configuration-parser names in Versioneer only; no conversion
algorithm or force-field parameter is patched.

```bash
python3.12 -m venv converter-env
converter-env/bin/python -m pip install \
  numpy==1.26.4 scipy==1.16.3 ParmEd==4.3.1 networkx==3.5 \
  decorator==5.2.1 six==1.17.0 OpenMM==8.3.1
git clone https://github.com/shirtsgroup/InterMol.git InterMol
git -C InterMol checkout --detach 7125764d42f6e6c589dc2d1df71e3e812b3a7b27
git -C InterMol apply ../preparation/intermol-python312.patch
converter-env/bin/python -m pip install --no-deps ./InterMol
converter-env/bin/python preparation/convert_inputs.py \
  --master master --output converted-again
converter-env/bin/python preparation/audit_topologies.py \
  --master master --converted converted-again --output converted-again-audit.json
```

ParmEd preserves the complete flexible topology, including water H–H bonds;
GROMACS constraints are selected in the simulation inputs. InterMol converts
that flexible GROMACS representation to LAMMPS. Its additional SHAKE adapter is
retained with the fixture source and validated separately: it preserves the
original potentials and uses the exact implied water geometry. NAMD and AMBER
read the canonical Amber topology directly.

The audit checks actual parameter arrays, full torsion-potential curves,
exclusions and scaled 1–4 pairs. A valid format or successful process exit alone
does not establish equivalence. Native single-point energies/forces and the
complete dynamics are separate acceptance gates with their own receipts.

The clean-environment reproduction on 23 September passed this parameter audit.
Coordinates matched the retained master exactly. AMBER topology headers changed
only their timestamp; GROMACS comments changed the date/path/command. InterMol
also reassigned some internal torsion type IDs/order while preserving the full
atom-indexed potential curves. Therefore regenerate-and-hash is not assumed to
produce byte-identical files: use the archived inputs for the exact accepted
run, or treat regenerated files as a newly audited input variant.

## Engine-specific requests

Each `inputs/<engine>/input.tar.gz` holds native configuration/topology files.
Inspect it without altering the archive:

```bash
tar -tzf inputs/gromacs/input.tar.gz
```

Its accompanying `request.json` lists the exact ordered preparation, dynamics
and analysis steps. `run_case.py` invokes that same request with a pinned worker,
records a stable operation ID, and supports native local checkpointing or hosted
submission. The raw `runs/<engine>/result.json` includes the commands actually
executed, exit status, elapsed time and complete native file inventory.

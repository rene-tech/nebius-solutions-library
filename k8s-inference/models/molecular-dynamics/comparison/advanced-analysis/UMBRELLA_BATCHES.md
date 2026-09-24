# Independent native umbrella batches

`umbrella_batches.py` repackages the validated `fixtures-04/window-01..23`
without changing scientific inputs. It does not submit operations, read
credentials, run dynamics, change limits or modify `umbrella_campaign.py`.
Window 00 is excluded because its independent operation is already owned by
the campaign coordinator.

The output is three ordinary GROMACS workflow requests: windows 01–08, 09–16
and 17–23 (8/8/7 independent jobs). This is batch fanout, not MPI, replica
exchange, shared writable directories or a claim that eight GPUs are presently
available. The normal scheduler and existing owner admission rules still apply.

## Exact changes

Every batch bundle contains each selected window's eight original input files
under `window-NN/`. The runtime extracts the common bundle separately into
each independent job workspace. All original file sizes/SHA256 values are
verified against the original preparation inventory and archive, and checked
again after copying. Original requests, preparations and bundles are retained
under `batch-NN/original/window-NN/`, outside the submitted input bundle.

Only these workflow changes are made:

- `grompp -f STAGE.mdp -p system.top -n dihedrals.ndx` gains that job's
  `window-NN/` input prefixes. Only the initial `-c start.gro` is similarly
  prefixed. Subsequent coordinate, checkpoint and TPR dependencies stay in cwd.
- Before each NVT/NPT/production energy extraction, add native
  `eneconv -f {files:STAGE.part*.edr} -o STAGE-canonical.edr`. The energy tool
  then reads that one file. Raw native parts are retained; the explicit pattern
  cannot include the canonical output on a retry. No time shifting, resampling,
  energy scaling, modified selection or new shell API is introduced.

All MDPs, seeds, coordinates, topology, index, protocol, mdrun flags, stage
ordering, duration, output cadence and workflow budgets are unchanged. Each
job now has 15 steps, including three added analysis steps. The builder fails
on unknown stage layouts, differing batch-level settings, incorrect input
inventories or output-directory reuse.

The exact worker's `gmx eneconv -h` confirms multiple inputs and default sorting.
It retains the later file at duplicate timestamps. Its documented limitation is
that accumulated sigma/E² metadata is not updated reliably on merging: compute
statistics from exported per-time energy values, not cumulative energy-summary
statistics. See the [GROMACS 2026.2 eneconv manual](https://manual.gromacs.org/2026.2/onlinehelp/gmx-eneconv.html).

## Reproduction

```sh
python3 k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/test_umbrella_batches.py
python3 k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/umbrella_batches.py \
  --fixtures /home/tux/fs2-alanine-analysis-20260924/umbrella/fixtures-04 \
  --output /home/tux/fs2-alanine-analysis-20260924/umbrella/batches-01
```

Use a new output path on repetition. The tar/gzip is deterministic (sorted
members, fixed headers/owner/mode/mtime); tests verify repeat archive and request
hashes. The source uses the existing runtime's schema normalizer, safe archive
extractor and file inventory rather than a parallel contract implementation.

Actual local result: **18 tests passed**. All 23 original archives and all three
generated archives passed runtime extraction/content checks. All three requests
also normalized successfully inside the unchanged deployed image
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:14ffdae0f0389c7771bae8791c56a5e21736ece630bd11dfb0f3b6a78f8cd643`,
using Docker without network or GPUs and a read-only input mount. The native
help reports `2026.2-dev`. This is local fixture validation, not a simulation
or restart qualification result.

## Generated identities

Root: `/home/tux/fs2-alanine-analysis-20260924/umbrella/batches-01`.
Each `batch-NN` contains `input.tar.gz`, `request.json`, `request.normalized.json`
and `manifest.json`. The root receipt binds science parity and original variants.

| Batch | Archive SHA256 | Request SHA256 |
| --- | --- | --- |
| 01 (01–08) | `208f9167db1c499e98b169cdc920bbe6ab955a2299418960e116372fc5e322ab` | `2c832028e80e1c57c00b752cb980bc88e2d58adb1fae68ce38981fd3eed42354` |
| 02 (09–16) | `f589024e2c34b4f6195b6c4b9a725a209837cb72383162b5f97a8d4dcb055859` | `5f6959df08ea3d775cd8cc83058eb4b6f3545048e4ea2a959fc81fafc832a0b8` |
| 03 (17–23) | `27be0a8b2f0b53d2a5761ab7de9936856ac15ca81d7057a545ac32bdf7aa42e5` | `4bc0975ff2bb5a8f754a582ad92ac358696c8750fb1276829e9193efdf24ef56` |

Root receipt SHA256:
`e3eea56625f32bc31a5971dd9e3c152d2d7cb74cd766ea59f0d808e9460e77ed`.
The separate `exact-worker-schema-validation.json` binds the actual packaged
validator and canonical normalized-request hashes.

## Artifact-size accounting, not a limit change

Because every job receives the common immutable bundle, the initial input
inventory has 64/64/56 files per job, hence 512/512/392 input-file rows across
the respective batches. Expanded input bytes per job are
3,511,762 / 3,511,728 / 3,072,767. Compact input-inventory JSON alone is
8,145 / 8,145 / 7,127 bytes per job. These are measured input lower bounds, not
complete native output or API-manifest estimates. All native outputs, logs,
segments and API metadata will add to them; the actual total is explicitly
unknown before execution. No client transport settings or server limits are
changed or claimed sufficient by this fixture preparation.

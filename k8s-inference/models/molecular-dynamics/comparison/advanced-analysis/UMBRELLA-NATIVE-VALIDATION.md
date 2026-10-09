# Native umbrella-window execution and geometry gate

`validate_umbrella_native.py` validates completed materialized GROMACS windows,
without native simulation, cloud calls, trajectory modification or index-cache
writes. It is deliberately separate from the compiled TPR/bias-energy audit,
WHAM analysis, statistical convergence and customer-release acceptance.

## Invocation

Use the existing pinned analysis environment (NumPy 1.26.4, MDAnalysis 2.10.0,
ParmEd 4.3.1). From the repository root:

```bash
/home/tux/.venvs/fs2-four-engine-20260923/bin/python \
  k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/validate_umbrella_native.py \
  --delivery /home/tux/fs2-alanine-comparison-20260923/delivery-02 \
  --windows /home/tux/fs2-alanine-analysis-20260924/umbrella/native-04/canary/window-00 \
  --output /path/to/new-validation-directory
```

`--windows` accepts multiple explicit workspace paths. Alternatively use
`--manifest windows.json`, containing either a list of paths or
`{"windows": [{"path": "batch-01/window-01"}, ...]}`. Relative paths resolve
against the manifest directory. Duplicate paths or window IDs are rejected;
different repetitions must not silently substitute for different windows.

Each workspace supplies `result.json`, `request.json` and `data/`. Both flat
`data/production-canonical.xtc` and batch-prefixed
`data/window-NN/production-canonical.xtc` are supported. The actual batch layout
has flat outputs and archived inputs under `data/window-NN/`; every window's
input subtree is retained. Inputs are therefore resolved from the exact native
`prepare-production` grompp `-f/-p/-n` arguments, which must match the requested
step. They must belong to this window, be result-inventory-bound, and produce
the same `-o` TPR subsequently selected by production `mdrun -s`. Cross-window
references fail even if the referenced topology has identical bytes.

The first batch validator pass, `validation-batch01-01`, is retained as failed:
the original reader assumed inputs and outputs were colocated. The correction
changes path binding only, with explicit cross-window negative tests; it does
not change native data or scientific tolerances. Retry receipts use new paths.

Output must be a new directory outside all original workspace and frozen
delivery trees. It contains an immutable `receipt.json` and one frame-by-frame
CSV per passing window. A failed window remains a failure with its exact gate
and error in the receipt; other explicit windows can still be checked. Exit
status is nonzero if any window fails. Receipt status `passed` is an execution
and molecule-integrity result, never an equilibrium certificate.

## Checks and tolerances

- Every result-listed native artifact must exist with its exact byte count and
  SHA-256, without duplicate or escaping paths. Consumed input bytes are checked
  again after analysis. The frozen master and reference files are also rehashed.
- `system.top` must match the original frozen GROMACS topology byte for byte,
  independently of the new window's own file manifest. The original reference
  is bound to its frozen native result manifest.
- Request/job identity and completed step inventory must agree. All native
  commands must succeed; production checkpoint steps must increase strictly and
  end at exactly 1,000,000. Native log final step/time and checkpoint-writing
  markers corroborate 2,000 ps and completion, with no LINCS/fatal marker.
  The recorded checkpoint step was produced by the worker's native
  `gmx dump -cp`, and its CPT file is hash-verified. This CPU reader does not
  invent an independent binary-CPT parser or claim to have rerun that command.
- There must be exactly 2,000 nonzero 1 ps native XTC frames at integer native
  steps 500 through 1,000,000, plus an optional initial frame. All 6,598 atom
  coordinates and cells are finite; cells are right-handed, nonsingular and
  reduced. No schedule reconstruction, interpolation or repair is allowed.
- Ordered original `production.part*.xtc` frames must exactly equal the
  canonical concatenation. Only an identical adjacent initial source frame may
  be counted once, with a recorded boundary. A changed restart boundary fails.
- Existing validated periodic geometry helpers make only a working-copy
  peptide whole. Its 21 bonds must remain within the existing 0.6–2.2 Å sanity
  range. Alanine's N–CA–C/CB signed triple product must retain the master's sign
  on every frame. All 6,588 hydrogen/water constrained distances must remain
  within the existing high-precision-XTC tolerance of `1e-4 Å`.
- Pull output must contain exactly the two native dihedrals, with instantaneous
  (not averaged) output, 20,000 nonzero samples at 0.1 ps and optional t=0.
  Missing, duplicate, shifted or reordered samples fail. Native XTC steps and
  times select the matching pull observation; no nearest-time interpolation.
- Phi and psi are computed from canonical atom order using the existing
  dihedral helper. Residuals use wrapped circular differences, not subtraction
  across the ±180° branch. Intentional phi umbrella bias is distinguished from
  coordinate/pull disagreement. Zero direct psi spring does not prevent a
  coupled conformational response; neither is an execution failure.

## Precision-aware coordinate/CV comparison

The native XTC stores precision `1,000,000 / nm`; this is checked on every
frame. Coordinate uncertainty includes half the quantization interval plus
float32 unpacking and periodic-cell image roundoff. The validator derives a
conservative per-frame dihedral error from the perturbation of both plane
normals, then adds native text rounding and a small numerical allowance.
Underresolved geometry requiring a bound larger than 0.05° fails; the bound is
not fitted to the observed coordinate/pull residual.

GROMACS uses four decimal places for pull time and `%g` (six significant digits)
for the native coordinate values; stripped trailing zeros do not mean reduced
precision. For example `170.03` has a rounding allowance of 0.0005°, not 0.005°.
The primary source is
[GROMACS 2026.2 pulling/output.cpp](https://github.com/gromacs/gromacs/blob/v2026.2/src/gromacs/pulling/output.cpp).
This source documents the output convention, not source-to-vendor-binary parity.
Observed coordinate/CV correspondence is tested directly on the exact native
files. Atom mapping, column/sign, timing and representation discrepancies are
reported as possible correspondence causes, not mislabeled as convergence.

## Focused tests

```bash
python -m unittest discover \
  -s k8s-inference/models/molecular-dynamics/comparison/advanced-analysis/tests \
  -p test_validate_umbrella_native.py -v
```

Synthetic tests cover missing/duplicate frames, native steps/times, pull
cadence/columns, coordinate precision, periodic wrap, chirality inversion,
constraint violations, wrong topology despite a rehashed mutant manifest,
false result-only completion, native warning markers, source concatenation,
immutable output paths and fixed native controls. Synthetic fixtures are never
counted as native trajectory evidence.

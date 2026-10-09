# Scientist-visible docking outliers: bounded follow-up

The existing deterministic analysis **does expose** the severe retained pose
when an experimental ligand reference is supplied. It does not currently offer
reference-free receptor-contact diagnostics. No deployed workbench or runtime
was changed during this inspection.

## Verified existing behavior

The workbench `molecule-analysis.py` reports all poses, finite-coordinate and
exact graph/stereochemistry comparability, unfitted reference RMSD, per-pose
confidence, and distinct best/worst RMSD versus highest/lowest confidence ranks.
Its `demos/analysis.cjs` adapter preserves those fields in the typed tool response
and saves deterministic `metrics.json`, `rows.csv` and `report.md` files.

CPU replay of the retained 3O96 / IQO seed19 result confirms:

- All four poses remain visible and chemically comparable.
- Worst RMSD is rank4:705.8664230996361Å.
- Lowest confidence is rank4:-6.129574298858643.
- Best RMSD is rank2:4.960856127966316Å, while highest confidence is rank1.
- Thus the helper does not hide the outlier or confuse confidence rank with
  experimental agreement. `comparable` means a valid metric comparison, not a
  scientifically good pose.

This replay first rejected the raw isolated adapter's internal `poses` envelope,
as expected: the scientist helper accepts the public `ligand_positions` and
`position_confidence` fields. A metadata-only projection then copied the exact
SDF strings and confidences following `Adapter.render_native_response`; no
coordinates or scores were changed. That initial inspection error is not a
customer API failure and was not followed by new model inference.

## Narrow missing capability and recommendation

The helper currently takes a reference ligand and predicted ligand poses, not a
receptor. It therefore cannot compute ligand–receptor contacts, receptor clashes,
or a useful geometric separation diagnostic when no experimental ligand pose
exists. The separate protein–protein structure helper is not this capability.

An optional **same-coordinate-frame receptor input** could add, per pose:

1. Minimum ligand-heavy-atom to receptor-heavy-atom distance, in Å.
2. Count/fraction of ligand heavy atoms with a receptor neighbor inside an
   explicitly reported caller-selected contact cutoff.
3. A descriptive `no_contacts_within_cutoff` flag when that count is zero.

These metrics work without an experimental ligand reference and would expose a
far-separated docking sample directly. They must retain receptor selection,
source hash, atom selection, cutoff and units in provenance. A missing receptor
must mean unavailable metrics, not fabricated zeros. Return the full original
pose set/order and confidence scores; do not filter, rerank, change model
sampling budgets or add a universal confidence threshold. Contact proximity is
not affinity, correct chemistry, lack of clashes or evidence of binding.

This is a recommendation only. No v36 candidate changes or live writes were made.

## Retained evidence

Protected output directory:
`diffdock-analysis-inspection-20260919-oKpxT4/`.

- Analysis helper SHA256:
  `6d0a0ec90400ae66a87da5133c42eae8fcbe7fe10a983b5401109e294316056b`.
- Typed workspace adapter SHA256:
  `9727ac9dbc9a663d3aeb435735edbdffdcfd5c5363a0013588620cebec8d767c`.
- Exact field-only projection SHA256:
  `b55a4e5f6e6721870546d617ff4ea36d442a5df087e7d1f605d598254ee11bf9`.
- Generated metrics SHA256:
  `9c1d2e9783a436dcd375fd83a845e6cd326eab7ef49715f7beaf4d1bc67cdb0d`.
- Generated report SHA256:
  `6a022fa1e894fa368ad965b2eea986f4c80c1e56256d4ecef152c55298acefd2`.
- Generated CSV SHA256:
  `407b001cc6ca6d63575be0011ccbb1b8884729e61c28f66b76a8117a34c04c54`.

The reference's known missing-3D-header warning remains visible. This offline
helper check is not a new live LibreChat or public-route acceptance cohort.

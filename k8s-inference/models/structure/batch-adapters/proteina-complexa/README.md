# Proteina-Complexa scientific-batch adapter

Candidate integration for commit
`54058860d43444c7289873f77d3e50b5b02348cd` of
`NVIDIA-BioNeMo/Proteina-Complexa`. The upstream repository has no release tag;
version 1.1.0 is only the package version observed at that commit.

The public envelope is the shared `scientific-run-request/v1` schema. This
directory owns identity evidence and examples only. `adapter.py` is a small
compatibility import; the implementation lives with the canonical controller
contracts in `components/control-plane/src/fs2_serve/scientific_batch/adapters`.
It accepts logical artifact IDs and the controller localizes them under its
private mount root. User paths, commands, images, environments, unknown fields,
and duplicate JSON fields are rejected.

The catalog profile projects through `scientific_plan_from_catalog_profile`
into four dependent `ScientificStagePlan` objects:

1. `generate` (GPU)
2. `filter` (CPU), after `generate`
3. `evaluate` (GPU), after `filter`
4. `analyze` (CPU), after `evaluate`

Every stage has exec-form `complexa <stage> <config> ...` argv and consumes the
previous stage's logical artifact. The controller materializes each immutable
tar handoff into an operation-isolated campaign workspace; no stage reads another job's
mutable directory. Configs come from the image's exact `/opt/fs2/source/configs`
root. Model identity is limited to the three exact,
ungated Hugging Face variants in the source-qualification record, mapped to the
canonical `complexa-*` cache IDs under `/opt/fs2/artifacts`. Protein-target
self-refolding uses AlphaFold2; ligand-target and AME self-refolding use the
image's `/opt/venv/bin/rf3` plus the exact Foundry checkpoint filename. Optional
ESM2, ESMFold, and MPNN metrics are explicitly disabled until their immutable
artifacts pass the target cache/readiness gate. No NGC-only gate or nonexistent
release is represented.

The collector consumes upstream `RAW_*binder*_results_*_combined.csv` (falling
back to `binder_results_*.csv`) and each referenced PDB. It removes upstream
filesystem-path columns before committing the canonical CSV. The validator
verifies content length and SHA-256, bounded unique designs, finite pLDDT/PAE/
RMSD metrics, and matching non-degenerate two-chain structures. Passing it does
not qualify the model.

Run the focused suite from `components/control-plane`:

```bash
PYTHONPATH=src:../../catalog/runtime uv run pytest -q \
  tests/test_scientific_primary_adapters.py
```

The runtime contract is bound to the locally smoke-tested immutable image
digest, but the candidate still has no complete artifact-readiness manifest,
H100 semantic receipt, exposed route, or deployment readiness. In particular,
the AlphaFold2 cache still needs a declared, verified archive expansion.

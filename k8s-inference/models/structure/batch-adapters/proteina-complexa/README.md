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
back to `binder_results_*.csv`) and both PDBs referenced by each result row:

- `structure.N` retains the original generated coordinates from `pdb_path`
  (`protein-complex-structure/v1`) for backwards-compatible downloads.
- `self-refolded.N` contains the selected self-refolded coordinates from
  `self_complex_pdb_path` (`proteina-complexa-self-refolded-structure/v1`).
  This is the prediction associated with the `self_` evaluation metrics.
- `design-provenance` joins `id_gen`, the sequence hash, sanitized CSV row hash,
  generated/refolded artifact names and hashes, binder chain, metric prefix and
  exact upstream source revision. Protein-target uses AlphaFold2; ligand-target
  and AME use RF3. Neither is described as an Amber-relaxed prediction.

The sanitized CSV retains scientific columns, removes upstream filesystem paths,
and adds `generated_structure_artifact` and `self_refolded_structure_artifact`.
Consumers must use these links, not row position or filename suffix: real
upstream output has `id_gen=0` associated with a filename containing `id_1`.
The validator verifies pointer hashes, bounded unique designs, finite scalar
metrics, complete one-to-one role linkage, matching binder/target sequences and
non-degenerate coordinates. Missing or mismatched refolds fail explicitly.
Raw geometry is not silently represented as the refolded prediction; model
confidence/self-consistency is not experimental binding efficacy.

Requested diffusion steps and sample count are passed to every pipeline stage.
Upstream evaluation flattens its own config into the results table; using the
default evaluation config previously mislabeled 100-step generation as 400
steps. Existing historical CSVs are not rewritten. BestOfN has two replicas per
input sample, so up to `2 * num_samples` designs remain expected. The terminal
artifact count now accommodates two structures per design, at most one nonempty
CSV per design, and one provenance file; the aggregate 2 GiB output and existing
handoff byte limits are unchanged.

In-flight operations admitted before this change retain their exact old
`maximum_designs + 1` artifact limit. The new companion recognizes that frozen
contract and finishes with the original raw PDB/CSV outputs, explicitly marked
`legacy-inflight-generated-only`, `design_provenance_verified=false` and
`scored_refolded_coordinates_available=false`, with a warning about the missing
refolded coordinates. It does not expand an admitted limit or treat the old
output as role-qualified. Every newly compiled request uses the new contract;
unknown frozen output bounds fail explicitly.

Offline checks do not qualify a live deployment. The September 18 collector
regression includes an actual retained evaluate-stage handoff (archive SHA-256
`1aceb5ccf8414a41b687714680fec90c932f7b5e2e05c30b919d09b5cb08977e`), two generated
and two CSV-linked AF2-refolded structures. Independent CA RMSDs reproduced the
two upstream values within 0.000001 Å. Refolded binders had no adjacent-CA
outliers under the qualification bounds, unlike the raw generated coordinates.
This is artifact/linkage evidence, not a claim of experimentally validated
binders, full ligand/AME coverage, or current-release readiness.

Run the focused suite from `components/control-plane`:

```bash
PYTHONPATH=src:../../catalog/runtime uv run pytest -q \
  tests/test_scientific_primary_adapters.py tests/test_proteina_design_provenance.py
```

The retained-handoff test is optional in ordinary CI. Set
`FS2_PROTEINA_RETAINED_HANDOFF` to the private, hash-verified archive to include
it; no customer data, credentials or raw archive are committed here.

The runtime contract is bound to the locally smoke-tested immutable image
digest, but the candidate still has no complete artifact-readiness manifest,
exposed route, or deployment readiness.

## AlphaFold2 parameter localization

`AF2_DIR` names a directory, never `alphafold_params_2022-12-06.tar`. ColabDesign
resolves `AF2_DIR/params/params_<model>.npz` first and `AF2_DIR/params_<model>.npz`
second, and upstream `download_startup.sh` expands the archive flat into that
directory, so the canonical localized tree is the flat sixteen-entry parameter
set: 5,587,956,571 bytes, tree inventory SHA-256
`cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4`. The archive
that produced it is separate provenance: 5,587,968,000 bytes, SHA-256
`36d4b0220f3c735f3296d301152b738c9776d16981d054845a68a1370b26cfe3`. The archive
is 11,429 bytes larger than the tree it carries, because tar headers and block
padding are not runtime content.

Both stages that mount AlphaFold2, `generate` for the protein-target variant and
`evaluate`, carry a `RuntimeTreeBinding`, and the preflight in
`adapters.localization` fails closed on an archive-only mount, a partial tree, a
wrong tree, or an identity mismatch before any argv runs. The declaration lives
in `catalog/runtime/contracts/scientific-artifact-localization.json`.

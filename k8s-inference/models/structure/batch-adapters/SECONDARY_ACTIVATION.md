# Secondary and academic scientific fleet activation inputs

This document is the integration handoff for ESMFold2, ESMFold2-Fast,
Protenix v2, OpenFold3 and AlphaFold 3. It was produced on current `main`
without merging any predecessor branch history, without editing a shared
aggregate JSON contract, and without touching the shared H100 cluster or B300.

## What is delivered

| Piece | Location | State |
| --- | --- | --- |
| Secondary image publisher tree (ESMFold2, ESMFold2-Fast, Protenix v2, OpenFold3) | `models/cancer-immunotherapy/images/structure-secondary/` | Reconstructed as plain files from the reviewed tree; parser sources byte-identical to published source `e6d20c7c`; three opaque node IDs redacted from the README; 39-test `check.sh` gate passes against exact upstream tags |
| AlphaFold 3 controller adapter | `components/control-plane/src/fs2_serve/scientific_batch/adapters/alphafold3.py` | Reconciled to the current controller gate: `data-pipeline` / `inference`, 16 CPU / 64 GiB reference class, whole-root reference mount, private parameter file mount, `processed-input` handoff, deterministic data and result collectors |
| AlphaFold 3 model-owned contract, fixtures, README | `models/structure/batch-adapters/alphafold3/` | Identity contract, two positive and one negative request fixtures |
| AlphaFold 3 terminal reference receipt and validator | `reference-data/evidence/af3-terminal-receipt-20260903.json`, `reference-data/scripts/validate_published_revision.py`, tests | Ported from the accepted `ecdbd477` delta; receipt digest `b049e698…f6a6` verified by the producer's own validator |
| Parameter schemas | `catalog/runtime/schema/{esmfold2,esmfold2-fast,protenix-v2,openfold3,alphafold3}-parameters.schema.json` | Mirror each adapter's `Parameters.parse`; registered by `$id` in the profile catalog |
| Activation fragments | `models/structure/batch-adapters/<model>/activation/` | `workload-profile.json`, `execution-map-fragment.json`, `integration-recipe.json`, rendered by `render_activation_fragments.py`, drift-checked by `--check` |
| Profile schema extension | `catalog/runtime/schema/scientific-workload-profile.schema.json` | Optional `runtime_artifacts` (the sibling general-CPU lane's exact hunk) and optional per-stage `placement` / `resources` that the controller already reads |

Every fragment profile compiles both of its positive fixtures through the real
`compile_adapter_run` dispatcher and rejects its negative fixture. The
AlphaFold 3 fragment pair additionally passes end to end through
`FileScientificManifestRenderer`: the current controller parses the fragment's
execution-map entry, compiles the request, passes `_verify_alphafold3_runtime`,
binds the published reference receipt and the private parameter file, and
renders a CPU pod on the reference hostPath plane and a GPU pod on the private
claim with `subPath alphafold3/af3.bin.zst`.

## What the fragments are

A `workload-profile.json` wraps one complete `scientific-workload-profile/v1`
entry in the projection envelope the onboarding compiler emits
(`merge_target` names the aggregate). The onboarding declaration path was not
used because `expand_declaration.py` can only emit GPU-then-CPU linear chains,
cannot set per-stage parallelism, checkpoint or preemption modes, and cannot
carry the AlphaFold 3 placement class or envelope; a declaration for these five
models would therefore describe a different workload than the adapter runs.

An `execution-map-fragment.json` wraps one `scientific-execution-map/v3` model
entry. For AlphaFold 3 every identity is real. For the four secondary models
the stage entries (image digest, collector and validator ids, resources,
placement labels) are exact, while each `runtime_artifacts[].localization_receipt_digest`
is `null` and listed under `activation_gates`, because no localization
generation exists for those artifacts yet.

An `integration-recipe.json` lists the shared edits the integration owner
performs serially and the recipe digest computed on this branch.

## Serialized integration order

1. Merge this branch. Nothing here is deployable on its own.
2. For AlphaFold 3, add `"alphafold3"` to the module allow-list tuple in
   `fs2_serve/scientific_batch/adapters/__init__.py`. This is the one shared
   code edit the model needs; it moves the `boltzgen` and `proteina-complexa`
   recipe digests, so it belongs with step 4.
3. Add each model's `recipe_paths` to `_RECIPE_MODEL_PATHS` in
   `adapters/common.py` and to `MODEL_IDS` in
   `components/control-plane/scripts/refresh_scientific_recipes.py`.
4. Append each `workload-profile.json#profile` to
   `catalog/runtime/contracts/scientific-workload-profiles.json`, run
   `refresh_scientific_recipes.py`, and extend the pinned expectations in
   `tests/test_scientific_workload_contracts.py` and
   `components/control-plane/tests/test_scientific_canary.py`.
5. Append `execution-map-fragment.json#model` to
   `catalog/runtime/contracts/scientific-execution-map.json` only once every
   `localization_receipt_digest` is a real receipt digest and, for the secondary
   models, once a delivery mechanism exists for public localization generations
   at the adapters' declared mount paths.
6. Rename the AlphaFold 3 bound workload stage in
   `scheduling/cpu-class-contract.json` from `raw-input` to `data-pipeline`
   (scheduling owner).

## Exact blockers, by kind

### Runtime and capacity

- No general CPU pool is deployed (`deployment.cpu_pools` is empty), so the
  secondary CPU stages, which default to the `general-cpu` class, have no live
  lane until `fs2-general-cpu-batch-pool-terraform` lands.
- The AlphaFold 3 data pipeline needs a 16 CPU / 64 GiB reference-data node
  class. Both live reference nodes are 8 vCPU / 32 GiB behind a 6 CPU / 24Gi
  Kueue quota; `runnable_on_declared_pool` stays false until the larger class
  is provisioned.
- Execution map v3 mounts a public localization generation only whole at
  `/reference-data`; the adapters expect `/models/...`, `/databases/...` and
  `/models/protenix-v2`. Delivery of a generation at those paths has no
  mechanism yet (the general CPU lane's execution-map work is closing it).
- Writable compiler caches for Protenix and OpenFold3 (`/cache/...`) cannot be
  expressed as read-only reference or private mounts; caches remain
  unprovisioned auxiliaries and no fast-start level above L1 is claimed.
- None of the four secondary images has exact-artifact semantic H100 evidence;
  they passed build-only starts with empty `/models` and `/databases`.

### Data and identity

- The four secondary models' artifacts exist only as `model-artifacts`
  catalog manifests; no scientific-localization generation, marker or node
  admission receipt exists for them.
- Protenix v2: the adapter contract pins composite identity `5e1c3b54…74d48`
  with localization manifest `a093d28e…cc6b7`, while
  `model-artifacts/manifest-protenix-v2.json` records `8e14bb80…12eca` for the
  same five files (2,514,895,264 bytes versus the contract's 2,514,897,184).
  One identity must be settled by the localization producer; the fragment
  binds the catalog identity and names the gap.
- Protenix v2: the checkpoint was recovered from the mirror
  `TMF001/protenix-v2-weights@653edab2` and has not been byte-compared with the
  publisher CDN object (region-specific 403).
- OpenFold3: the scientific model id `openfold3` collides with the existing
  HTTP catalog record `catalog/runtime/models/openfold3.json` in the onboarding
  compiler's collision check. The accepted adapter, contract, handoff and
  academic readiness projection all use `openfold3`, so the id was kept and
  the collision is recorded for the integration owner.
- The academic parameter object's ownership must be re-verified live as
  gid 65532 / mode 0440 with `supplementalGroups` consumption before an
  AlphaFold 3 GPU run. A 2026-09-03 `fsGroup` rewrite was reported and a
  repair was recorded by sibling tasks; this task did not create a pod to
  re-verify it.

### Licence

- AlphaFold 3 formal institutional licence acceptance is
  `FormalAcceptancePending`. The authorized proof-of-concept path does not
  depend on it and it is not synthesized here. The parameter object stays
  tenant-private, never embedded and never world readable.
- All secondary artifact licences are verified in the catalog manifests (MIT,
  MIT with third-party notices, Apache-2.0, CC0-1.0); no gate remains there.

## Gates run on this branch

See the task's Test Evidence for exact commands and counts. In summary:
`structure-secondary/check.sh` (39 tests, exact upstream tags), the r4
cross-run and external adapter verifier, `reference-data/scripts/check.sh`
(70 tests, Terraform tests), the control-plane suites including
`test_scientific_alphafold3_adapter.py` and
`test_scientific_secondary_activation.py` with ruff and mypy, the catalog
`run_checks.sh`, the academic-assets gate, and the root suite with the
public-export scan.

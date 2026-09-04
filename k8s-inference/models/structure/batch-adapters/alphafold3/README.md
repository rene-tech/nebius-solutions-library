# AlphaFold 3 scientific batch adapter

Model-owned activation input for AlphaFold 3 v3.0.4 (`google-deepmind/alphafold3`
at `85c4d20505fd5cef05eac22b534d4e793971ae69`) on the current two-lane
scientific controller. The implementation lives in
`components/control-plane/src/fs2_serve/scientific_batch/adapters/alphafold3.py`;
`adapter.py` here is a compatibility import, `contract.json` is the identity
contract, `fixtures/` are the public request fixtures, and `activation/` holds
the fragments the serialized integration step merges.

## Stages

1. `data-pipeline` runs on the `reference-data` CPU class (16 CPU, 64 GiB,
   32 GiB ephemeral) in `fs2-academic-poc` through `academic-scientific-cpu`
   and `reference-data-cpu`. It mounts the complete published reference root
   read-only at `/reference-data` with no `subPath`, carries supplemental group
   1000, and passes the producer-generated terminal receipt path plus
   `--threads 16 --cpu-request 16`. After a terminal runtime PASS the adapter
   collector verifies every indexed handoff file by path, size and SHA-256,
   rejects traversal and symlinks, and publishes one deterministic tar named
   `processed-input`.
2. `inference` consumes that handoff on one H100 through `academic-scientific`.
   It mounts only the private `af3.bin.zst` object from
   `academic-assets-runtime-rwx` at `/models/af3.bin.zst` (claim subPath
   `alphafold3/af3.bin.zst`, supplemental group 65532, no `fsGroup`) and never
   sees the reference databases. The result collector requires a terminal PASS
   receipt that names the exact parameter digest, validates the upstream output
   layout (top-level model, summary and full confidences, `ranking_scores.csv`,
   and every `seed-<s>_sample-<i>` directory), bounds `ptm`, `iptm`,
   `ranking_score` and `fraction_disordered`, and rejects structures without
   real atom records.

These are exactly the shapes `execution.py::_verify_alphafold3_runtime`
enforces, which is why the accepted predecessor's `raw-input` stage name became
`data-pipeline` here.

## Identities

| Identity | Value | Source of truth |
| --- | --- | --- |
| Runtime image | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/alphafold3@sha256:0cde199e…9e03e` (tag `3.0.4-85c4d205-r6`) | `models/cancer-immunotherapy/images/alphafold3/contracts/af3-image-lock.json`; registry digest re-read on 2026-09-04 |
| Parameters | `af3.bin.zst`, SHA-256 `74d02586…f33ff`, 1,020,545,840 bytes | `academic-assets/contracts/academic-assets.json` |
| Reference tree | `d27b8956…dfaea`, manifest `aa585259…9748`, inventory `38af3baa…1579`, receipt `b049e698…f6a6`, 195,867 files | `reference-data/evidence/af3-terminal-receipt-20260903.json` |

The parameter object is tenant-private: it is never embedded in an image,
never world readable, and never enters a general shared cache. Callers supply
no licence receipt; the deployment's `Granted` / `Authorized` state is projected
as `access.profile=academic` + `access.state=verified`, and removing it fails
compilation closed. OpenFold3 is an independent, non-equivalent alternative and
never satisfies an `alphafold3` request.

## Activation fragments

`activation/workload-profile.json` is a complete candidate profile (unrouted,
`candidate-unqualified`, MCP discoverable but not invocable) that compiles both
positive fixtures through `compile_adapter_run`. `activation/execution-map-fragment.json`
is the matching execution-map v3 entry with every identity real; the test suite
feeds both through `FileScientificManifestRenderer` and proves the CPU pod
lands on the reference hostPath plane and the GPU pod on the private claim.
`activation/integration-recipe.json` lists the shared edits the integration
owner makes serially: the dispatcher allow-list line, the recipe paths, the
aggregate merges, the test pins, and the `raw-input` to `data-pipeline` rename
in `scheduling/cpu-class-contract.json`. Regenerate with
`models/structure/batch-adapters/render_activation_fragments.py`.

## Still blocking a live run

* The live reference nodes are 8 vCPU / 32 GiB behind a 6 CPU / 24Gi quota;
  the 16 CPU / 64 GiB data-pipeline class must exist first.
* The academic claim's ownership must be re-verified as gid 65532 / mode 0440
  before a GPU run (a 2026-09-03 `fsGroup` rewrite was reported and repaired by
  sibling tasks; this task did not re-verify it live).
* Formal institutional licence acceptance is `FormalAcceptancePending`; the
  authorized proof-of-concept path does not depend on it.
* No raw-input run has completed through the public path, so the route stays
  closed and no readiness is claimed.

Run the focused suite from `components/control-plane`:

```bash
PYTHONPATH=src:../../catalog/runtime uv run pytest -q tests/test_scientific_alphafold3_adapter.py
```

# Secondary structure successor adapters

This slice registers four non-AlphaFold3 scientific adapters against the existing
controller contract. Each adapter consumes one artifact-service-verified logical
entry from the outer input manifest, carries explicit CPU/GPU resource and placement
envelopes through scheduler freeze, and returns the production companion collection
type. The companion installs the four collector sets once in its process-global
allow-list. Renderer, Kueue, Terraform, runtime localization, and route activation
remain owned elsewhere.

| Model | CPU stage | GPU stage | Immutable published digest |
| --- | --- | --- | --- |
| `esmfold2` | `prepare-input` | `fold` | `sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3` |
| `esmfold2-fast` | `prepare-input` | `fold` | `sha256:6eaf386a9bb4453d5048e16c28b8ca4236ae0f222185e33d5a7a49a1e1c8fa35` |
| `protenix-v2` | `prepare-data` | `sample-structure` | `sha256:ac8f7c2c35d2bc911281f9d4a8aa9779e2cb955cdb1c2c2d37eb31d89669980e` |
| `openfold3-openbind` | `data-pipeline` | `inference` | `sha256:6b15da4b2258c0c385adc1dbc7799493f3768cb4881f7990cb957f2c3b6759e4` |

The generated argv is shell-free and is parser-tested directly from image
source Git object `a25251748608f3f437277e3c1c3c91896d5dc482`. ESMFold2-Fast rejects every MSA
mode before GPU admission. Protenix prepares an immutable compressed handoff before
sampling. OpenFold3 is an independent, non-equivalent backend and never satisfies
an `alphafold3` request.

All four localization-compatible successors are immutably published and passed
offline image and protocol checks. Publication remains deliberately separate from
route activation: the new digests have no H100 semantic receipt yet. Existing
activation projections retain their predecessor identities until fresh live runs
qualify each successor; qualification is never inherited. The r7 OpenFold successor
runs as UID/GID `10001:10001` and validates the public `openfold3-openbind` identity,
but its predecessor's H100 receipt is not evidence for r7. The canonical
machine-readable publication handoff and current image tuples remain in the
historically named `secondary-r4-image-handoff.json`.

Protenix keeps three identities separate: the acquired source payload, the
localization manifest/recipe, and the content digest of the tree mounted by the model
Pod. None is accepted as a substitute for another in an activation receipt.

Run the focused contracts from `k8s-inference/components/control-plane`; the
test materializes the exact successor wrapper source from the pinned Git object:

```bash
PYTHONPATH=src:../../catalog/runtime \
  uv run --frozen pytest -q tests/test_scientific_secondary_adapters.py
```

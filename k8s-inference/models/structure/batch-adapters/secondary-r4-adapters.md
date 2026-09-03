# Secondary structure r4 adapters

This slice registers four non-AlphaFold3 scientific adapters against the existing
controller contract. It owns only the model adapters, fixtures, identity contracts,
and the minimum explicit dispatch branches. Scheduler, renderer, Kueue, Terraform,
runtime localization, and route activation remain owned elsewhere.

| Model | CPU stage | GPU stage | Immutable r4 digest |
| --- | --- | --- | --- |
| `esmfold2` | `prepare-input` | `fold` | `sha256:e8fb269ff17e752ed8dd8f6c4689eaa55c0efc7adaffc156ccd9357bd075463d` |
| `esmfold2-fast` | `prepare-input` | `fold` | `sha256:ba55b9bb418d9714b21634c9fd6281f678529042bc3d0b8f06f184fa314a2577` |
| `protenix-v2` | `prepare-data` | `sample-structure` | `sha256:27d816dc518b5dda205f9916205fbc4e2053a8109d9380b85628d9f0d968a644` |
| `openfold3` | `data-pipeline` | `inference` | `sha256:d1d249fcd8aca464ff0ee0b6e78e0f9c1fe243e0ebd18acc3c4223070fcf203b` |

The generated argv is shell-free and is parser-tested against image source commit
`e6d20c7cb3abf5e172852f17a20c7e100daa1245`. ESMFold2-Fast rejects every MSA
mode before GPU admission. Protenix prepares an immutable compressed handoff before
sampling. OpenFold3 is an independent, non-equivalent backend and never satisfies
an `alphafold3` request.

All four image bindings remain `build-only-not-semantic-qualified`. Their public
profiles and routes must stay absent or fail closed until exact artifact localization,
declared runtime-cache delivery, and semantic inference on H100 have all produced
promotion evidence. The canonical machine-readable activation gate and image tuples
are in `secondary-r4-image-handoff.json`.

Run the focused contracts from `k8s-inference/components/control-plane` with the
exact wrapper source available:

```bash
FS2_SECONDARY_WRAPPER_ROOT=/path/to/structure-secondary \
  PYTHONPATH=src:../../catalog/runtime \
  uv run --frozen pytest -q tests/test_scientific_secondary_adapters.py
```

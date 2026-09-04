# Secondary structure r4 adapters

This slice registers four non-AlphaFold3 scientific adapters against the existing
controller contract. Each adapter consumes one artifact-service-verified logical
entry from the outer input manifest, carries explicit CPU/GPU resource and placement
envelopes through scheduler freeze, and returns the production companion collection
type. The companion installs the four collector sets once in its process-global
allow-list. Renderer, Kueue, Terraform, runtime localization, and route activation
remain owned elsewhere.

| Model | CPU stage | GPU stage | Immutable r4 digest |
| --- | --- | --- | --- |
| `esmfold2` | `prepare-input` | `fold` | `sha256:e8fb269ff17e752ed8dd8f6c4689eaa55c0efc7adaffc156ccd9357bd075463d` |
| `esmfold2-fast` | `prepare-input` | `fold` | `sha256:ba55b9bb418d9714b21634c9fd6281f678529042bc3d0b8f06f184fa314a2577` |
| `protenix-v2` | `prepare-data` | `sample-structure` | `sha256:27d816dc518b5dda205f9916205fbc4e2053a8109d9380b85628d9f0d968a644` |
| `openfold3-openbind` | `data-pipeline` | `inference` | `sha256:d1d249fcd8aca464ff0ee0b6e78e0f9c1fe243e0ebd18acc3c4223070fcf203b` |

The generated argv is shell-free and is parser-tested directly from Git object
`e6d20c7cb3abf5e172852f17a20c7e100daa1245`. ESMFold2-Fast rejects every MSA
mode before GPU admission. Protenix prepares an immutable compressed handoff before
sampling. OpenFold3 is an independent, non-equivalent backend and never satisfies
an `alphafold3` request.

All four r4 image bindings remain historical
`build-only-not-semantic-qualified` evidence. They predate the repaired atomic
publication protocol: ESM prepared JSON, shared confidence JSON, and zstd
handoffs were written directly to their final names. None of the r4 digests may
be activated. New immutable successor images must be built from this source,
then semantically qualified. Their candidate
profiles are deliberately `route_exposed: false` and omit runtime image and execution
identity digests until exact artifact localization, declared runtime-cache delivery,
and semantic inference on H100 have all produced promotion evidence. In particular,
the OpenFold r4 image is additionally not runnable as runtime UID 10001 and
uses the legacy localization model id `openfold3`; its successor validates the
public `openfold3-openbind` identity. The canonical machine-readable
activation gate and historical image tuples are in `secondary-r4-image-handoff.json`.

Protenix keeps three identities separate: the acquired source payload, the
localization manifest/recipe, and the content digest of the tree mounted by the model
Pod. None is accepted as a substitute for another in an activation receipt.

Run the focused contracts from `k8s-inference/components/control-plane`; the
test materializes the exact r4 wrapper source from the pinned Git object:

```bash
PYTHONPATH=src:../../catalog/runtime \
  uv run --frozen pytest -q tests/test_scientific_secondary_adapters.py
```

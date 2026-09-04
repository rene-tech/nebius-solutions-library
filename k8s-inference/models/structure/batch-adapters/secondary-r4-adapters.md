# Secondary structure successor adapters

This slice registers four non-AlphaFold3 scientific adapters against the existing
controller contract. Each adapter consumes one artifact-service-verified logical
entry from the outer input manifest, carries explicit CPU/GPU resource and placement
envelopes through scheduler freeze, and returns the production companion collection
type. The companion installs the four collector sets once in its process-global
allow-list. Renderer, Kueue, Terraform, runtime localization, and route activation
remain owned elsewhere.

| Model | CPU stage | GPU stage | Immutable r5 digest |
| --- | --- | --- | --- |
| `esmfold2` | `prepare-input` | `fold` | `sha256:870b9f647f41bb02cfcbf08d5eec6cdf6b5171e8771c776248c5865c2f762a4a` |
| `esmfold2-fast` | `prepare-input` | `fold` | `sha256:fc7b8687849511a04b04afd9c477bcc0fb85a2837eac6ac658609e8b7e2702e0` |
| `protenix-v2` | `prepare-data` | `sample-structure` | `sha256:b90a02bdffe3eefa8a251eb1e3666f3748a72e68fdec0b3cd867c2f08b426af8` |
| `openfold3-openbind` | `data-pipeline` | `inference` | `sha256:3686e5303cbe51b18949b5f5815336db8ca31100b72c8d4b676f848fb193b1de` |

The generated argv is shell-free and is parser-tested directly from image
source Git object `a1b5b6b24b2dae54a1f7caeba9981a7aaa60cc8f`. ESMFold2-Fast rejects every MSA
mode before GPU admission. Protenix prepares an immutable compressed handoff before
sampling. OpenFold3 is an independent, non-equivalent backend and never satisfies
an `alphafold3` request.

All four r5 successors passed offline image and protocol checks. The image handoff
retains its historical `build-only-not-semantic-qualified` publication state, while
activation profiles independently pin the published image, artifact, recipe, and
H100 semantic-evidence identities before becoming dispatchable. In particular,
the successor OpenFold image runs as runtime UID/GID `10001:10001` and validates
the public `openfold3-openbind` identity. The canonical machine-readable
activation gate and current image tuples remain in the historically named
`secondary-r4-image-handoff.json`.

Protenix keeps three identities separate: the acquired source payload, the
localization manifest/recipe, and the content digest of the tree mounted by the model
Pod. None is accepted as a substitute for another in an activation receipt.

Run the focused contracts from `k8s-inference/components/control-plane`; the
test materializes the exact successor wrapper source from the pinned Git object:

```bash
PYTHONPATH=src:../../catalog/runtime \
  uv run --frozen pytest -q tests/test_scientific_secondary_adapters.py
```

# Visual-science Apps

This directory contains two bounded, artifact-backed Scientific AI Apps intended for interactive demos and small research inputs.

## Cellpose CPSAM v2

`cellpose-cpsam-v2` accepts one PNG, JPEG, or TIFF image up to 16 MiB and 4 megapixels. It returns the exact input and model digests, a 16-bit labeled PNG mask, a deterministic color overlay, object count, and per-object pixel areas. The runtime serializes GPU inference per replica.

The checkpoint is pinned to Hugging Face revision `7c61431b5fbb078f3296754bd15d9f51b320f837`; `/opt/cellpose-models/cpsam_v2` must hash to `0f1cc3f7ecdd8a037a57c6c48d9d8921391be4cbce3fa9f13c3e3a2e1253c667`. Keep this App research-only: the code and checkpoint card use BSD-3-Clause, but the upstream training-data provenance includes non-commercial material. The platform contract therefore requires `research_only: true` and declares commercial use prohibited.

## scVI/scANVI

`scvi-scanvi` accepts one raw-count `.h5ad` file up to 64 MiB. The interactive lane is bounded to 100,000 cells, 50,000 genes, 200 million matrix entries, and 20 epochs per training stage. It supports unsupervised scVI and semi-supervised scANVI. A result ZIP contains:

- `integrated.h5ad`
- `latent_embeddings.csv`
- `umap_embeddings.csv`
- `preview.png`
- the saved model and `manifest.json`

The result is a research integration artifact. The UMAP is a visual summary, and the latent coordinates are model output; neither is clinical truth or an accuracy comparison without a labeled evaluation.

## Operation

The manifests in `k8s/` retain zero replicas because the platform controller owns scaling. For isolated qualification, scale one replica explicitly, use a direct port-forward, and run `acceptance/visual-science-20260919/qualify.py`. The validator issues two distinct success requests, one replay, and one failure request per model. It writes only synthetic fixtures.

The images run as UID/GID 10001 with a read-only root filesystem. Mutable caches, temporary training files, Matplotlib state, and Numba state are confined to bounded `emptyDir` volumes. H100 is the only qualified GPU target.

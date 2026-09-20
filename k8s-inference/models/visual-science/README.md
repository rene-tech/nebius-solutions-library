# Visual-science Apps

This directory contains bounded, artifact-backed Scientific AI Apps intended for interactive demos and small research inputs.

## SAM 2.1 Hiera Large

`sam2-1-hiera-large` segments prompted objects in PNG/JPEG images, generates automatic image masks, and tracks prompted objects through MP4 video. It accepts at most 64 MiB, 2,073,600 image pixels, 320 video frames, and eight prompted objects. The response is a ZIP with a manifest, 16-bit masks, and deterministic color overlays; video results also contain an overlay MP4.

The checkpoint is pinned to Hugging Face revision `665f8e2ad61cf5f53d65644ff27c8ee525124610` and SHA-256 `2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318`. SAM 2 predicts visual object masks and tracks objects. It does not generate images, identify biological cell types, or turn a mask into scientific ground truth. Code and checkpoint use Apache-2.0.

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

The manifests in `k8s/` retain zero replicas because the platform controller owns scaling. For isolated qualification, scale one replica explicitly, use a direct port-forward, and run the matching acceptance validator. The validators use synthetic fixtures and retain request and artifact digests.

The images run as UID/GID 10001 with a read-only root filesystem. Mutable caches, temporary training files, Matplotlib state, and Numba state are confined to bounded `emptyDir` volumes. H100 is the only qualified GPU target.

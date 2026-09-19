# Visual-science qualification — 2026-09-19

This acceptance set covers the direct H100 worker boundary for Cellpose CPSAM v2 and scVI/scANVI. It does not by itself qualify the public HTTP/MCP route, customer authorization, LibreChat rendering, elasticity, or cold starts; those require the separate public cohort after catalog publication.

`qualify.py` creates a compact evidence record for:

- two distinct synthetic inputs and distinct semantic outputs;
- exact image/checkpoint/input identities;
- a deterministic Cellpose replay and a semantic scVI replay;
- malformed-input or missing-acknowledgement failure behavior;
- mask overlays and scVI/scANVI ZIP/preview validation.

The retained direct reports are `evidence/cellpose-native.json` and `evidence/scvi-native.json`. `evidence/pods.json` binds them to the observed pod, image digest, UID/GID, node, GPU allocation annotation, and zero-restart state. Both workers reported NVIDIA H100 80GB HBM3 with driver 580.159.04.

Full generated fixtures, ZIP outputs, PNG previews, Trivy JSON, and SPDX SBOMs are retained outside Git under `/home/tux/.local/state/scientific-ai/visual-science-20260919/`. `evidence/scan-summary.json` is the compact repository record. Images with unresolved critical or high findings remain temporary/internal and must not be promoted as production-ready until a successor image passes the vulnerability gate.

Run the validator through local forwards:

```bash
python3 qualify.py cellpose http://127.0.0.1:18080 microscopy-0.png microscopy-1.png --output-dir results/cellpose
python3 qualify.py scvi http://127.0.0.1:18081 cells-0.h5ad cells-1.h5ad --output-dir results/scvi
```

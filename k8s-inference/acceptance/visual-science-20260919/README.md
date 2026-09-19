# Visual-science qualification — 2026-09-19

This acceptance set covers both the direct H100 worker boundary and the separate public file-to-result boundary for Cellpose CPSAM v2 and scVI/scANVI. LibreChat remains a user-shaped acceptance step because MCP qualification alone does not prove tool use from the chat client. Elasticity and cold starts remain separate gates.

`qualify.py` creates a compact evidence record for:

- two distinct synthetic inputs and distinct semantic outputs;
- exact image/checkpoint/input identities;
- a deterministic Cellpose replay and a semantic scVI replay;
- malformed-input or missing-acknowledgement failure behavior;
- mask overlays and scVI/scANVI ZIP/preview validation.

The retained direct reports are `evidence/cellpose-native.json` and `evidence/scvi-native.json`. `evidence/pods.json` binds them to the observed pod, image digest, UID/GID, node, GPU allocation annotation, and zero-restart state. Both workers reported NVIDIA H100 80GB HBM3 with driver 580.159.04.

Full generated fixtures, ZIP outputs, PNG previews, Trivy JSON, SPDX SBOMs and public receipts are retained outside Git under `/home/tux/.local/state/scientific-ai/visual-science-20260919/`. `evidence/scan-summary.json` is the compact repository record. The final scans contain no critical findings; the retained summary records the reviewed high-finding disposition without treating package-inventory false positives as silent passes.

Run the validator through local forwards:

```bash
python3 qualify.py cellpose http://127.0.0.1:18080 microscopy-0.png microscopy-1.png --output-dir results/cellpose
python3 qualify.py scvi http://127.0.0.1:18081 cells-0.h5ad cells-1.h5ad --output-dir results/scvi
```

After the exact combined control-plane image is deployed, run the public cohort with an ordinary user PAT stored in a mode-0600 file. The runner uploads each file through the public artifact API, invokes the discovered typed MCP tool, verifies in-flight and terminal idempotency replay, downloads and hashes the externalized output, checks the result semantics, and rejects two invalid inputs before admission.

```bash
components/control-plane/.venv/bin/python \
  acceptance/visual-science-20260919/run_public.py \
  --origin https://89.169.99.188 \
  --verify-tls \
  --token-file /absolute/private/user.pat \
  --fixtures acceptance/visual-science-20260919/fixtures \
  --output /absolute/private/visual-science-public-qualification \
  --control-plane-image sha256:<exact-digest> \
  --source-commit <exact-commit>
```

The same four synthetic inputs are available to the Rene LibreChat endpoint at `/workspace/demo-assets/visual-science-20260919/`. `UPLOAD-INDEX.json` and `SHA256SUMS` bind the mounted copies to the acceptance fixtures.

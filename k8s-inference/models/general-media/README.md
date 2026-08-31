# FS2 general/media model runtimes

This task-owned directory contains the retained runtime adapters and Kubernetes
manifests for `evo2-40b`, `glm-5-2-fp8`, `nv-reason-cxr-3b`,
`nv-segment-ct`, and `sdxl`.

The common media image is based on the exact CUDA 13 B300-qualified vLLM image
digest already mirrored by FS2. SDXL loads the public exact Diffusers revision;
NV-Segment-CT loads NVIDIA's exact public MONAI repository revision. Both model
processes expose separate health, readiness, metrics, and bounded inference
endpoints and load their model once per retained Pod.

SDXL accepts `POST /generate`. The default or `response_format: "image/png"`
returns the PNG body used by the byte-level semantic fixture. Public control-plane
and MCP callers set `response_format: "b64_json"` to receive a JSON envelope with
the base64 PNG, immutable model identity, effective generation parameters, PNG
size/hash, backend identity, and the echoed `X-FS2-Operation-ID` correlation ID.
NV-Segment-CT accepts a base64 gzip NIfTI volume at `POST /segment` and returns a
JSON envelope containing the output NIfTI plus non-clinical identity metadata.

All medical-model outputs are research-only and non-clinical. Live deployment
evidence and immutable image bindings are recorded under `evidence/`.

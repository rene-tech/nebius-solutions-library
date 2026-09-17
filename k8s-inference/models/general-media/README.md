# FS2 general/media model runtimes

This task-owned directory contains the retained runtime adapters and Kubernetes
manifests for `cosmos3-nano`, `evo2-40b`, `glm-5-2-fp8`,
`nv-reason-cxr-3b`, `nv-segment-ct`, and `sdxl`.

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

Cosmos3-Nano runs the exact vLLM-Omni image and Hugging Face revision recorded
in the runtime catalog. The upstream server remains available cluster-internal
on port 8000. A companion adapter on port 8080 exposes `POST /generate`, health,
readiness, and metrics. The adapter has strict mode-specific requests for text
to image/video, image to video, video to video, and controlled transfer video.
A reference is either a finalized
tenant artifact (materialized by the control plane) or an immutable HTTPS URL;
customer-local paths are never interpreted on the server. The legacy text media
calls retain their bounded inline base64 response, while all new MP4 workflows
return raw video to the control plane for asynchronous object-backed artifact
publication. Transfer controls are limited to the pinned runtime's `edge`,
`blur`, `depth`, `seg`, and `wsm` inputs. Forward dynamics and inverse dynamics
remain dormant adapter code and are not in the public schema: on 2026-09-15 the
exact pinned H100 runtime crashed its diffusion worker with SIGBUS after decode
(and the CUDA/CRIU-restored forward path returned `cudaErrorNotSupported`).
Policy/OpenPI and LeRobot dataset-to-dataset conversion are likewise not
advertised by this App until their own pinned fixtures pass on the deployed GPU
path.
The exact 2026-09-15 media and action-mode results are recorded in
[evidence/cosmos3-nano-media-qualification-20260915.md](evidence/cosmos3-nano-media-qualification-20260915.md).

The exact 68-file Cosmos artifact and Qwen3-8B's exact 15-file artifact are
localized once into immutable content addresses. Concurrent replicas share an
atomic receipt and skip both download and full-payload hashing on a warm cache.
See [SHARED_CACHE_FAST_START.md](SHARED_CACHE_FAST_START.md) for the writer,
receipt, recovery, and qualification boundaries. Pool placement is supplied by
the model profile or overridden through `models.pool_overrides`; the Cosmos
manifest itself has no GPU-family node selector.

All medical-model outputs are research-only and non-clinical. Live deployment
evidence and immutable image bindings are recorded under `evidence/`.

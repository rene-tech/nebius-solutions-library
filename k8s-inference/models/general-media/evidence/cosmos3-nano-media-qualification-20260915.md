# Cosmos3-Nano media qualification — 2026-09-15

This record separates exact-runtime qualification from public release
acceptance. It must not be interpreted as evidence that the corresponding
control-plane/MCP build was deployed to the shared customer endpoint.

## Runtime and placement

- Project: `project-e00rene`
- Region: `eu-north1`
- Managed Kubernetes cluster: `mk8scluster-e00j5z9te7x5dd9g6a`
- kubectl context: `k8s-inference-h100`
- Isolated preview: Deployment and ConfigMap
  `fs2-models/cosmos3-nano-media-preview-r20260915`
- Scheduled node: `computeinstance-e00r9tdjfszjs3angk`
- Node group: `mk8snodegroup-e00n67290jzpdmffbv`
- Capacity: preemptible, one `H100` 80 GB GPU, CUDA driver `580.159.04`
- Runtime image:
  `docker.io/vllm/vllm-omni@sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587`
- Model:
  `nvidia/Cosmos3-Nano@7a312c868bcce8e40b3eb40861300a9d0ba3fde1`
- vLLM-Omni source/recipe revision:
  `eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c`

## Real-GPU semantic results

These were deliberately small, low-step semantic qualification requests; the
durations are warm request latency and are not throughput or quality
benchmarks. Every successful media response had the expected PNG/MP4 magic,
content type, byte length, and a reported digest (inline response metadata or
artifact-response header) equal to the digest of the received bytes.

| Public mode | Qualification input | Result | Bytes | Warm adapter time |
|---|---|---:|---:|---:|
| text-to-image | text, 256x256, one step | valid PNG | 196,993 | 0.175 s |
| text-to-video | text, five 256x256 frames, one step | valid MP4 | 69,585 | 0.462 s |
| image-to-video | pinned official Cosmos image, five frames, one step | valid MP4 | 87,418 | 0.706 s |
| video-to-video | final immutable HTTPS URL for the pinned official MP4, first-frame conditioning | valid MP4 | 50,560 | 2.906 s |
| transfer-video | edge control derived from reference, resolution bucket 256 | valid MP4 | 71,628 | 2.605 s |
| text-to-video with sound | five frames plus generated sound | valid MP4 | 44,708 | 0.644 s |

`ffprobe` confirmed the sound result contained H.264 video (0.5 s) and AAC
audio (0.521333 s). The first transfer attempt exposed a pinned-runtime
contract requirement: without `extra_params.resolution`, vLLM selected 640 and
returned `Unknown Cosmos3 action resolution=640; expected one of
['256','480','704','720']`. The adapter and public schema now require the exact
resolution bucket `256`, `480`, `704`, or `720`; the resolution-256 rerun above
passed.

## Fast-start observation

The CUDA/CRIU preview restored successfully before media qualification:

- CRIU restore: 28.2165 s
- CUDA restore of the main PID: 4.6919 s
- CUDA unlock: approximately 0.03 s
- Recorded mechanism: `cuda-criu-restored`

An ordinary non-snapshot load used to isolate action failures reported 12.23 s
for weights, 15.96 s for model-runner load, and 36.91 s for
`AsyncOmniEngine` initialization, followed by approximately 8.65 s of dummy
warm-up.

## Deliberately unpublished action modes

The pinned runtime contains forward- and inverse-dynamics code, but neither
mode passed the exact H100 runtime gate:

- Forward dynamics on the CUDA/CRIU-restored runtime returned HTTP 500 with
  CUDA `operation not supported` (`cudaErrorNotSupported`).
- Forward dynamics after an ordinary load completed diffusion/decode, then the
  DiffusionWorker exited 135 (`SIGBUS`) and the adapter observed the upstream
  as unavailable.
- Inverse dynamics used the official AV fixture (61 frames, 832x480, action
  dimension 9, chunk 60). It likewise completed diffusion/decode and then the
  worker exited 135 (`SIGBUS`).

Consequently forward dynamics, inverse dynamics, policy, and OpenPI are not in
the public input mode enum or typed MCP tool set. The dormant adapter paths are
not a supported customer interface and require a separately qualified runtime
before publication.

## Automated evidence

From `k8s-inference/components/control-plane`:

```text
.venv/bin/ruff check \
  src/fs2_serve/model_input_contracts.py src/fs2_serve/mcp_server.py \
  tests/test_model_input_contracts.py tests/test_mcp_model_tools_http.py \
  tests/test_artifact_inputs.py ../../models/general-media/tests/test_cosmos3_media_adapter.py
All checks passed!

.venv/bin/python -m pytest -q \
  tests/test_model_input_contracts.py tests/test_mcp_model_tools_http.py \
  tests/test_artifact_inputs.py tests/test_artifact_outputs.py \
  tests/test_packaging.py ../../models/general-media/tests/test_cosmos3_media_adapter.py
69 passed, 1 warning in 41.27s
```

The warning was pytest cleanup of a pre-existing protected PostgreSQL test
socket under `/tmp`; it did not fail a test. Server-side Kubernetes dry-run
parsed all six resources and accepted every object. The preview Deployment and
ConfigMap were then deleted with `--wait=true`; a follow-up exact-name and label
query returned no preview objects or Pods.

## Remaining release gate

The integration owner must deploy the shared control-plane build and exercise
`tools/list`, artifact upload/finalize, typed V2V submission, operation polling,
artifact-result retrieval, authorized download, and byte-level MP4 validation
through the real public MCP URL. Until that passes, the implementation is ready
for integration review but is not a customer-readiness claim.

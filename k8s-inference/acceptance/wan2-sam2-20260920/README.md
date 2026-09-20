# Wan2.2 and SAM 2 qualification

`qualify.py` exercises the exact direct runtime surfaces with deterministic synthetic media. It validates SAM prompted-image, automatic-image and video tracking ZIPs, exact replay for prompted image, and two distinct generated MP4s for each Wan NIM variant. Retain normalized, credential-free receipts here and keep raw artifacts and protected deployment records outside Git.

The Wan checks are marketing-media checks. They do not establish scientific accuracy. SAM output masks are model predictions and are not biological or clinical ground truth.

## Retained SAM 2 H100 result

The exact SAM image, checkpoint and all three modes passed on one Nebius
Serverless AI H100 80 GB instance on 2026-09-20. The model loaded in 5.1
seconds, prompted image segmentation scored `0.984981` with a 3,034-pixel
mask, automatic segmentation returned five objects, and prompted video
tracking followed one object through all 12 test frames. The normalized receipt
is `evidence/sam2-serverless.json` with SHA-256
`c9ffaeb75e7b34700b5d9ddc9a2761e1ece57007b8ce902a2dc9e5f0d6083f5c`.

This receipt covers direct H100 model execution. Kubernetes startup, public
routing, HTTP/MCP, LibreChat, cold start and elasticity still need separate live
acceptance. The runtime omits SAM's optional CUDA connected-components
extension, so the optional hole-filling and small disconnected-area cleanup
postprocessing is unavailable; core image and video inference passed.

A second disposable H100 job started the final image's real Uvicorn service and
called the readiness and segmentation endpoints over loopback HTTP. Readiness
took 8.017 seconds. Prompted image, automatic image and 12-frame prompted video
completed in 0.652, 0.685 and 1.923 seconds; all returned valid ZIP artifacts,
and prompted-image replay matched byte for byte. The normalized receipt is
`evidence/sam2-serverless-http.json` with SHA-256
`33328e2f0021111f79afbcabb18e11f76ad9622410051aa6219e223764e7015f`.
This closes container HTTP startup and direct endpoint semantics. The public
gateway/MCP path, LibreChat, controller-managed cold start and elasticity remain
separate gates.

The same immutable image then passed its Kubernetes lane in `fs2-models`. It
pulled in 89.74 seconds, ran as UID/GID 10001 on one H100, became Ready with
zero restarts, and served the two deterministic fixtures plus automatic image
segmentation through its ClusterIP Service. Prompted image took 0.724 seconds,
automatic image took 0.949 seconds, and the 24-frame video result took 4.260
seconds. The
normalized receipt is `evidence/sam2-kubernetes.json` with SHA-256
`3a7f21eac3339d6585f0bd96c4f36d4ab751ce3492da0ea989d7325386c7bce7`.

## Public route and LibreChat acceptance

SAM 2.1 is deployed at one ready H100 replica from immutable image index
`sha256:8a1b659d53b9ed47edb381e3086ac3768b13248ef6a3ee733e89c733597bd24c`.
Control-plane Helm revision 192 uses image index
`sha256:ac0563cfc216f688ba6841ab7fb3f52e045da2c0c053398d8def358bde73b18f`.
The authenticated public MCP tool `segment_track_media_native` completed a
fresh prompted-image request and returned a ZIP whose digest, size, manifest,
mask and overlay were independently validated. The normalized receipt is
`evidence/sam2-public-mcp.json` with SHA-256
`a4d409f8b6018dce72a7cd906e50b421da38a187206aab96f7b91bd849c6d016`.

The current customer LibreChat endpoint reported its Scientific models MCP
server connected, invoked the same typed tool from an actual chat, and returned
a succeeded operation with the exact model revision and artifact identity. A
separate client downloaded and validated that artifact. The normalized receipt
is `evidence/sam2-librechat-browser.json` with SHA-256
`d40784bdff19d53d382bc04e56dbab0bae3c688ff80bfcfdb95d9e0486990d0a`.

This acceptance establishes direct H100 inference, container HTTP, Kubernetes,
public MCP and LibreChat behavior for image prompts. It does not establish
semantic or clinical correctness, nor does it qualify public video uploads,
scale-to-zero or concurrent-user behavior.

## Wan2.2 activation state

The exact NVIDIA NIM is pinned as `nvcr.io/nim/wan-ai/wan2.2:1.0.0` with index
digest `sha256:05c1d390af4eec607b654172fa889ae8cef2b2c238e84516514e61e5ba52e63b`.
Text-to-video and image-to-video manifests use separate 200 GiB caches, a
16 GiB shared-memory volume and one H200 141 GB GPU each. The BF16 profiles
need about 121 GB of device memory and therefore do not fit the H100 80 GB
lane. The adapter image is pinned as
`sha256:c2cd8a47598c24285f9c4362f013d80cf7807d72babe39a958e29cf828ae66a0`.

The original NVIDIA API key was valid. The failed authentication attempts came
from a trailing newline in the Kubernetes Secret, which produced an invalid
authorization header. Recreating the Secret from the same protected key with
the newline removed fixed the issue; no replacement key was requested or
needed. The NIM listens on port 9000 so that its internal Triton process can
retain port 8001, while the platform adapter serves the bounded API on port
8000.

Both exact NIM variants passed two distinct 50-step requests through their
Kubernetes Services on 2026-09-20. Text-to-video returned 832x480, 61-frame,
16 fps VP9 MP4s in 198.968 and 197.152 seconds. Image-to-video returned
832x480, 65-frame, 16 fps VP9 MP4s in 227.744 and 224.834 seconds. The
normalized receipts are `evidence/wan2-t2v-kubernetes.json` with SHA-256
`55d62d2901624fb2c77c30453747260688daebdd592d4a7c34775575edf0270d`
and `evidence/wan2-i2v-kubernetes.json` with SHA-256
`6daa1656492144d8aa8ea31d3d9fcdd2efe5e3885ea84aff0f8aadd35554597d`.
The four retained MP4s were also decoded with FFprobe and visually reviewed.
They demonstrate colorful molecular, protein and microscopy motion. Generated
lettering is not reliable, so marketing titles and captions must be rendered
by the deterministic editing pipeline rather than requested inside the scene.

This evidence closes direct Kubernetes execution and bounded video semantics.
Public discovery, HTTP/MCP, LibreChat, controller-managed cold start,
elasticity and snapshots remain separate release gates until their receipts
are recorded below.

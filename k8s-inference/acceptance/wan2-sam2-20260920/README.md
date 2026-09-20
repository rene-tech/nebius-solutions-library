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
16 GiB shared-memory volume and one H100 each. The adapter image is pinned as
`sha256:af80ffbcce79ef468b88183388c68ef33c4ecbd6b123ee7f158c8ada2df68870`.
Wan remains deliberately unpublished until a fresh NGC API key is installed in
the protected handoff path and both variants pass real H100 video generation.
An NGC key exposed in chat must not be used as a Kubernetes or runtime secret.

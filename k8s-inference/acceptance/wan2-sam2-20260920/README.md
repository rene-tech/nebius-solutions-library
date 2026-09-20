# Wan2.2 and SAM 2 qualification

`qualify.py` exercises the exact direct runtime surfaces with deterministic synthetic media. It validates SAM prompted-image, automatic-image and video tracking ZIPs, exact replay for prompted image, and two distinct generated MP4s for each Wan NIM variant. Keep evidence outside Git and record its SHA-256 in the deployment-runtime declaration after a real H100 run.

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
seconds. The Deployment was returned to zero replicas after qualification. The
normalized receipt is `evidence/sam2-kubernetes.json` with SHA-256
`3a7f21eac3339d6585f0bd96c4f36d4ab751ce3492da0ea989d7325386c7bce7`.
Public gateway/MCP, LibreChat, controller-managed cold start and elasticity
remain separate gates.

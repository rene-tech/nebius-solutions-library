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

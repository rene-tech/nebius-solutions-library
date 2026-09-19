"""Scientific AI integration of NVIDIA PAIDF video augmentation."""

MODEL_ID = "physical-ai-video-augmentation"
PAIDF_REVISION = "bc5719362492a1e3b40bd7d33b43c46dd89efad5"
COSMOS_REVISION = "7a312c868bcce8e40b3eb40861300a9d0ba3fde1"
SERVING_REVISION = "eb11446b7f2e30ca582f8aff3afe12e9a2e66f6c"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/video-augmentation-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/video-augmentation-result/v1"
MAX_ITEMS = 64
MAX_VIDEO_BYTES = 128 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3

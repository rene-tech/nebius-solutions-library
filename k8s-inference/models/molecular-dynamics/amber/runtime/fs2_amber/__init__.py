"""Private, licensed AMBER26 native workflow runtime."""

MODEL_ID = "amber"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/amber-workflow-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/amber-workflow-result/v1"
# Actual private OCI identity binds PMEMD and the locked AmberTools layer.
ENGINE_ID = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/amber26-engine@sha256:cea619dcb5f8a8577a17edd7ce0fdd70e6718838f9d5af47d7731ca8388fab48"
PMEMD_SOURCE_SHA256 = "0478ccce892f3525e995e9c85458d552c6060b73dd28acd03c366e61ecf23a14"

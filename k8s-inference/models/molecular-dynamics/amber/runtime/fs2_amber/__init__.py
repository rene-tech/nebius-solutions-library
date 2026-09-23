"""Private, licensed AMBER26 native workflow runtime."""

MODEL_ID = "amber"
PARAMETER_SCHEMA = "fs2-serve.nebius.ai/amber-workflow-request/v1"
RESULT_SCHEMA = "fs2-serve.nebius.ai/amber-workflow-result/v1"
# Bootstrap PMEMD OCI identity. Replace with the combined PMEMD/AmberTools
# bundle identity before qualifying/publishing the final worker image.
ENGINE_ID = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/amber-pmemd26@sha256:744b8d42a846f93118d9288efed67546236cfafd2f7d2377e5c99bc7b9a6dcca"
PMEMD_SOURCE_SHA256 = "0478ccce892f3525e995e9c85458d552c6060b73dd28acd03c366e61ecf23a14"


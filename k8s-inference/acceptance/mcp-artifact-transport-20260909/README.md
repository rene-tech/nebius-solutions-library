# MCP artifact transport acceptance — 2026-09-09

The H100 platform now keeps large model inputs and outputs out of language-model
tool arguments and results. This closes the OpenFold2/DiffDock parallel-call
failure in which the client attempted to serialize the bundled 1UBQ PDB into a
DiffDock tool call and exhausted its output-token budget before the request
reached the server.

## Released implementation

- Source commit: `aedb4042b16669b4a67d6bd7aed8179afb5f4ede`
- Control-plane image:
  `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:60f38e8f5b42eb7a901443392de92d89d28844c4e5c530ea55d4941c60599773`
- Deployment: project `project-e00rene`, region `eu-north1`, cluster
  `mk8scluster-e00j5z9te7x5dd9g6a`
- Deployment method: the image digest was changed only in the private
  `terraform.tfvars` and applied through `inference-stack`. The converged
  post-apply Terraform plan reported no changes in configuration,
  infrastructure, foundation, or workloads.

The typed schemas mark transportable file fields. They accept either inline
content, a tenant-owned immutable artifact reference, or a small server fixture
reference where one is published. Artifact ownership, digest, size, media type,
compression, and size ceiling are checked before bytes are materialized. The
worker receives the original runtime payload only immediately before invocation.

The generic artifact tools are `begin_model_artifact_upload`,
`put_model_artifact_bytes`, `finalize_model_artifact_upload`,
`get_model_artifact`, `download_model_artifact`, and
`read_model_artifact_bytes`. Existing scientific aliases remain compatible.
Responses of 64 KiB or more, plus binary responses, are stored in the same
tenant-scoped artifact service and represented by a compact immutable pointer.
If that post-processing service is temporarily unavailable, an already
successful inference remains available inline and is not rerun or charged twice.

## Live regression

One MCP session submitted OpenFold2 and DiffDock concurrently. DiffDock used
`{"fixture_id":"pdb/1ubq"}` instead of placing the 78,570-character PDB in the
tool call. Its complete discovery example was 140 bytes.

| Model | Operation | Attempt | Status | Admission cold start | Result transport |
| --- | --- | ---: | --- | ---: | --- |
| OpenFold2 | `14506e27-ee81-4f71-b8fc-9ce64dba43ba` | 1 | succeeded | 0.571 s | 16,537-byte inline JSON |
| DiffDock | `d7588aae-fe3b-45ca-b38c-6d693ce981ee` | 1 | succeeded | 0.475 s | 317-byte artifact pointer |

Parallel admission completed in 1.765 seconds and both results were retrieved in
11.848 seconds. The DiffDock pointer addressed 80,920 bytes. A subsequent
`download_model_artifact` call retrieved those bytes through a short-lived
authorized handle; SHA-256, byte count, and JSON decoding all verified. The
gateway, model-controller, and admin-console deployments were ready after the
rollout, with no restarts on the released gateway pods.

## Verification

- Full control-plane suite before the lifecycle correction: 1,950 passed,
  98 skipped.
- Focused artifact, PostgreSQL artifact-contract, input-materialization, and MCP
  suite after the correction: 40 passed, 4 integration tests skipped when their
  optional object-store fixture was absent.
- Ruff and strict mypy passed for the changed implementation.
- Terraform validation and Helm lint passed with the chart's required deployment
  values.

Credentials, signed handles, raw model outputs, Terraform state, build
provenance, and plan files remain in owner-only local state and are not committed.

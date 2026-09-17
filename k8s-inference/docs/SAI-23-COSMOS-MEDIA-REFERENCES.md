# SAI-23 Cosmos media-reference boundary

Status: source candidate only. This document does not authorize integration,
deployment, or a production-readiness claim.

Cosmos media-bearing inputs accept finalized, caller-owned platform artifact
metadata only. The public control plane does not accept an HTTP(S) URL, a
runtime-local path, or a storage location in `input_reference`, the deprecated
`vision_path` alias, or `controls[].reference`.

The boundary has two source-level layers:

1. Every `ModelInputContract` for `cosmos3-nano` rewrites those fields to the
   existing artifact descriptor and dispatch-time materialization contract.
   This also covers specialized MCP contracts assembled outside
   `contract_for()`.
2. `AdmissionService` independently parses each Cosmos/native request and
   requires an exact `ArtifactRef` for every present media-reference field
   before writing the durable operation. The error never reflects the rejected
   value.

The worker remains responsible for tenant ownership, stored-content identity,
byte-count, media-type, and size verification before it materializes the bytes
for the selected runtime. Existing text-to-image and text-to-video requests
retain their payload shape and do not trigger artifact resolution.

The authored regression suite proves that `https://10.5.0.1/` is rejected at
all three affected field paths before a durable operation exists, while the
existing text workflow and a structurally valid artifact descriptor remain
admissible. Per the remediation coordinator boundary, those tests were authored
but must not be executed in this task.

This source candidate must be integrated only with the SAI-03 model-pod egress
policy. Artifact-only admission prevents new caller-selected locations from
reaching the runtime, while the egress policy provides defense in depth for
runtime or adapter failures. This task does not claim that dependency is
integrated or deployed.

Rollback is source-only: revert the candidate commit before integration. Do not
roll back by re-enabling URL inputs; if the broader Cosmos media feature cannot
consume artifact-materialized inputs, keep those modes unpublished until their
adapter is corrected and independently reviewed.

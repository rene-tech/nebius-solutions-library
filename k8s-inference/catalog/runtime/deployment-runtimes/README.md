# Deployment runtime records

These files describe tested replacement runtimes without rewriting the historical
NIM catalog, compatibility results or old artifact-acquisition records.

Each JSON record names an existing exact-model variant, the complete model
record, and its independent runtime qualification. Terraform selects a record
only when `deployment.models.image_overrides` names its exact immutable image
digest. A mirror of that digest in another regional registry selects the same
runtime; the actual deployment uses the regional image reference.
It publishes that selected record to the control plane and uses the same model,
artifact and runtime identity for placement, rendering and discovery.

GPU compatibility still comes from the exact runtime's hardware observations.
Passing on H100 does not imply passing on Blackwell. Public HTTP/MCP acceptance
is recorded after deployment and is separate from direct runtime qualification.

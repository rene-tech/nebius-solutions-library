# Recorded customer qualification

`capability_gate.py` evaluates exact-release acceptance receipts. The control
plane loads its published verdict map, validates the receipt structure and
capability states, and expires old evidence. **It does not compare the receipt's
release identity with the currently deployed release.**

Admin Apps therefore labels this projection **Last qualification** and **Recorded
verdict**, with the tested source revision and evidence-evaluation timestamp.
These are recorded acceptance results, not a fresh live-health probe or proof
that the current release is customer-ready. The API fields and evidence-expiry
behavior retain their existing contract; a positive `ready` field must be
interpreted with the recorded release identity, not as a live deployment check.

## Deployment requirement

On any release-identity change, the release owner must **clear or replace the
published verdict map as part of the coordinated deployment**. This includes
source/runtime images, configuration, model revision, client build, tenant
policy, public endpoint, or public tool-catalog identity changes. Do not leave
an unexpired positive verdict from the previous release published as though it
qualified the replacement.

Until exact replacement-release acceptance is complete, remove the affected
App entry (or publish an empty verdict index), retaining the original receipt
in the historical evidence store. Publish replacement entries only for the
exact release tested under [CUSTOMER_RELEASE_POLICY.md](../../CUSTOMER_RELEASE_POLICY.md).
Verify the Apps list/detail show the intended recorded revision, evaluation
time and verdict after deployment. Clearing the map is an operator deployment
obligation; no new automatic rollout or live-identity enforcement is claimed.

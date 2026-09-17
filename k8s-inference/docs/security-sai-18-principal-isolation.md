# SAI-18 principal isolation source handoff

This additive source successor remediates SAI-18 from the 2026-09-16
Scientific AI Platform security review. It was prepared on
`agent/fs2-sai-remediation-sai-18-r20260916` after fast-forwarding to exact
`origin/main` commit `0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697` (tree
`3b9a0627a7ebe0fb6beafe09d1a18b7c2d19083d`).

Independent exact review rejected predecessor
`336bfce007ba6853de407ba8b93473de4ba3c486` (tree
`4aa0b968d1148f2bc5b06f2360197e920d4cebf1`) as SOURCE/INTEGRATION/LIVE
NO-GO. That commit remains preserved. This direct successor closes the four
recorded gaps without rewriting the rejected evidence.

## Security contract

- Scientific status, cancellation, events, and result reads continue to call
  the shared exact-token-and-principal `require_operation_access` gate.
- Artifact metadata and bytes now resolve the artifact record to its immutable
  owning operation before HTTP or MCP returns metadata, a download handle, a
  parsed manifest, or inline content. Same-tenant peers receive the same
  not-found result as callers using an unknown identifier.
- Every PAT-authenticated `/internal/scientific-artifacts/*` read and write
  resolves its artifact or caller-supplied operation ID through that same
  owner gate before issuing a handle, returning state, or mutating artifact
  state. The router is not mounted without its principal-aware access service.
- Scientific submission authorizes the manifest artifact and every referenced
  entry against the submitting principal before reading their bytes. Generic
  inference materialization resolves the artifact's operation owner and
  compares it with the immutable claimed operation owner before iterating the
  content stream. An active `tenant.admin` PAT remains the only explicit
  same-tenant override.
- Interactive operators may issue ordinary workload keys for named models.
  A role-indexed safe scope allowlist makes all unclassified or privileged
  scopes—including `artifacts.write`, `tenant.admin`, `tokens.manage`, and
  `audit.read`—and wildcard model grants admin-only for issue, policy update,
  and rotation. Future scopes fail closed until explicitly classified.

Customer inference, scientific operations, uploads, result publication,
artifacts, and request-debug facilities are not removed or disabled. The
change adds authorization checks before existing read and credential-issuance
paths; it adds no schema or data migration.

## Authored regression coverage

- `test_scientific_lifecycle_service_denies_a_same_tenant_peer` covers service
  status, cancel, events, and result methods with two keys in one tenant.
- `test_same_tenant_peer_cannot_cancel_or_read_another_principals_artifact`
  covers the required HTTP `DELETE`, artifact metadata/download/content, and
  MCP artifact aliases.
- `test_operator_cannot_issue_or_assume_admin_key_policy` covers operator
  refusal for each privileged scope (including `artifacts.write`) and wildcard
  grants, denial of elevated key rotation/policy takeover, and the positive
  admin/bounded-operator paths.
- `test_same_tenant_peer_cannot_use_internal_read_or_write_routes` covers the
  internal handle, event, result, and mutation bypass and exact unknown/denied
  response equality.
- `test_same_tenant_peer_manifest_is_rejected_before_bytes_are_read` and
  `test_same_tenant_peer_artifact_is_rejected_before_its_bytes_are_consumed`
  cover scientific admission and generic runtime materialization respectively.
- `test_same_tenant_peer_cannot_cancel_or_read_another_principals_artifact`
  now compares the complete HTTP error documents for unknown and unauthorized
  artifacts, rather than asserting only status codes.

The coordinator restricted this remediation wave to static source authoring.
No tests, linters, formatters, builds, package managers, databases, containers,
clusters, registries, credentials, or deployed services were run or inspected.
An independent reviewer should execute at least:

```text
pytest -q components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_artifact_inputs.py \
  components/control-plane/tests/test_scientific_artifact_routes.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_execution_handoff.py \
  components/control-plane/tests/test_scientific_batch_production.py
ruff check components/control-plane/src/fs2_serve \
  components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_artifact_inputs.py \
  components/control-plane/tests/test_scientific_artifact_routes.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_execution_handoff.py \
  components/control-plane/tests/test_scientific_batch_production.py
ruff format --check components/control-plane/src/fs2_serve \
  components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_artifact_inputs.py \
  components/control-plane/tests/test_scientific_artifact_routes.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_execution_handoff.py \
  components/control-plane/tests/test_scientific_batch_production.py
```

## Required future live gate

No live or credential inspection was authorized for this source task. Before
any later rollout, the deployment owner must inventory active, unexpired PATs
created through operator sessions and identify keys carrying any scope outside
the operator safe allowlist or a wildcard model grant. Each such key must be
denied or allowed to expire through the approved non-destructive credential
lifecycle, with owner notification and rollback evidence; no credential row is
to be deleted. The inventory result and prior/new deployment digests remain
mandatory independent live evidence.

This is a source candidate only: it is not integrated, deployed, live-verified,
or finally accepted. Rollback after a future integration is a normal revert of
the candidate commit; there are no persistence changes to reverse.

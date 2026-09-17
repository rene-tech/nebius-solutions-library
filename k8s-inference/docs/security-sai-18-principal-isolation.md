# SAI-18 principal isolation source handoff

This source candidate remediates SAI-18 from the 2026-09-16 Scientific AI
Platform security review. It was prepared on
`agent/fs2-sai-remediation-sai-18-r20260916` after fast-forwarding to exact
`origin/main` commit `0e6fdf6d9f61e5737dc6ac5cec4c0111207dd697` (tree
`3b9a0627a7ebe0fb6beafe09d1a18b7c2d19083d`).

## Security contract

- Scientific status, cancellation, events, and result reads continue to call
  the shared exact-token-and-principal `require_operation_access` gate.
- Artifact metadata and bytes now resolve the artifact record to its immutable
  owning operation before HTTP or MCP returns metadata, a download handle, a
  parsed manifest, or inline content. Same-tenant peers receive the same
  not-found result as callers using an unknown identifier.
- Interactive operators may issue ordinary workload keys for named models.
  The `tenant.admin`, `tokens.manage`, and `audit.read` scopes and wildcard
  model grants require an admin role for issue, policy update, and rotation.
  Admin issuance and bounded operator issuance remain supported.

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
  refusal for each privileged scope and wildcard grants, denial of elevated
  key rotation/policy takeover, and the positive admin/bounded-operator paths.

The coordinator restricted this remediation wave to static source authoring.
No tests, linters, formatters, builds, package managers, databases, containers,
clusters, registries, credentials, or deployed services were run or inspected.
An independent reviewer should execute at least:

```text
pytest -q components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_production.py
ruff check components/control-plane/src/fs2_serve \
  components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_production.py
ruff format --check components/control-plane/src/fs2_serve \
  components/control-plane/tests/test_admin_access_api.py \
  components/control-plane/tests/test_scientific_artifact_public_bytes.py \
  components/control-plane/tests/test_scientific_batch_production.py
```

This is a source candidate only: it is not integrated, deployed, live-verified,
or finally accepted. Rollback after a future integration is a normal revert of
the candidate commit; there are no persistence changes to reverse.

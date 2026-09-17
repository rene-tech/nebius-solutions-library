# Final local verification — Cosmos and Stockholm

Date: 2026-09-17. Branch: `agent/fs2-cosmos-stockholm-remediation-r20260917`.
Base: `bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. All three implementation
workers froze their changes before the final focused checks below. The final
source is identified by this document's enclosing Git commit/history; no cloud
image is represented by that source identity.

**Historical local-test receipt, recorded before access was restored.**
Deployment subsequently resumed with the user-selected `sandbox2` profile;
these local counts are not customer qualification. See the current
[implementation and deployment handoff](cosmos-stockholm-remediation-20260917.md).

## Test results

| Check | Result | Scope and limits |
| --- | --- | --- |
| Full CP sweep, `pytest -q -m 'not postgres'` | 2233 passed, 16 skipped, 106 deselected; one packaging assertion failed | The assertion still expected 31 migrations after migration 32 was added. It was fixed, not suppressed. This sweep overlapped final integration edits and is not presented as a frozen all-green run. |
| Final affected CP/API/MCP/Helm/acceptance suite | **350 passed**, 122.04 s | Includes clean-wheel packaging, real local HTTP/MCP server, current router wiring, request semantics, chart renders, actual `promtool` evaluation, capability gates and both customer acceptance verifier tests. Synthetic inputs do not establish real GPU/customer acceptance. |
| Final PostgreSQL suite | **31 passed**, 20.21 s | Task-owned local PostgreSQL16: child delegation/current user policy, uploads/concurrency=1, retention references/maintenance role, usage projections/export, immutable terminal outcomes. No production DB used. |
| Admin-console typecheck and full suite | **225 passed / 35 files**, 9.11 s; typecheck passed | Includes new usage names and historical qualification labels. Not a production browser check. |
| LeRobot final runtime suite | **19 passed**, 16.25 s | Real pinned LeRobot reader/writer and dataset validation; GPU generation is not exercised. |
| Scientific LeRobot adapter suite | **3 passed** | Compiler/collector integration contract. |
| Local final CPU image | Build and offline encode/normalize/full-frame decode passed | Non-root UID10001, network disabled, read-only root, 16 RGB frames. No Cosmos GPU call. |
| Lint/types | Changed CP Python files Ruff passed; root API/metrics/router mypy passed; worker-owned accounting/runtime checks passed | Includes strict runtime typing. |
| Helm | Chart lint passed; final affected suite passed migration/dependency annotations | No cluster apply or Terraform plan/apply performed. |

Counts overlap where a focused suite repeats the full sweep; do not sum them
into a claim about independent scenarios. The 16 full-sweep skips and unselected
PostgreSQL tests are not passes. Existing Starlette and websockets deprecation
warnings remain; no failure was hidden by disabling those warnings.

After access was restored, the Helm, restored-media preview, Stockholm verifier
and live-preparation regression suite passed **191 tests in 40.35 seconds**.
An earlier invocation named a nonexistent test file and ran no tests; it was
corrected before this result. Pytest again warned about unrelated old PostgreSQL
socket cleanup permissions; those resources were not changed. This additional
local run does not replace the separately recorded public acceptance checks.

## Reproduce the final focused suites

From `k8s-inference/components/control-plane`, with its own frozen development
environment (`uv sync --frozen --group dev`):

```bash
.venv/bin/pytest -q \
  tests/test_packaging.py tests/test_api_mcp.py \
  tests/test_mcp_generic_controls.py tests/test_mcp_model_tools_http.py \
  tests/test_readiness_timeouts.py tests/test_request_telemetry.py \
  tests/test_telemetry_dynamic_models.py tests/test_helm_chart.py \
  tests/test_customer_readiness.py \
  ../../tests/test_customer_capability_gate.py \
  ../../acceptance/cosmos3-customer-20260915/test_acceptance.py \
  ../../acceptance/stockholm-customer-20260917/test_verify_receipt.py

# This DSN is an isolated task-owned local fixture, never production.
# These tests reset the named database; do not substitute a customer database.
FS2_TEST_DATABASE_URL=postgres://postgres@127.0.0.1:33474/semantic_outcomes \
  .venv/bin/pytest -q \
  tests/test_scientific_child_delegation.py tests/test_users_apps_postgres.py \
  tests/test_postgres_retention_references.py tests/test_operation_metrics.py
```

From `components/admin-console`: `npm run typecheck` and `npm run test:run`.
Task-owned temporary test roots were used on the final runs. Earlier runs emitted
cleanup warnings about other users' old PostgreSQL sockets; those directories
were not deleted or permission-changed.

The [local LeRobot image receipt](../models/general-media/lerobot-augmentation/activation/local-build-20260917.json)
records config ID `sha256:e8af3b9c347104418d726042a18af3e25e972b57338ea11d1fb3853cadbbae4d`,
the source file hashes, offline steps and retained upstream fixture-encoder
warning. This ID is **not** a registry manifest digest and must not be used as
a model promotion receipt. The registry was unchanged at this local-test stage;
subsequent publication is recorded in the separate registry receipt.

## Task Deck / remaining work

Both parents and their ten children remain open, linked under the NIM Fast Start
Platform epic. Root-owned cards and worker-owned cards record the same branch,
local evidence and missing live proof. Scope:

- Cosmos parent: `fs2-cosmos3-customer-workflows-remediation-r20260915`.
  Children: complete-media-api, mcp-artifact-contract, lerobot-augmentation-app,
  mcp-outcome-observability, customer-acceptance (same prefix/date).
- Stockholm parent: `fs2-stockholm-customer-readiness-remediation-r20260915`.
  Children: mcp-envelope-hardening, usage-accounting-reconciliation,
  operation-outcome-observability, operational-cleanup,
  customer-shaped-release-gate (same prefix/date).

At the time of this local-test receipt, cloud deployment was blocked by `JwtKeyNotExists` for the configured authorized
`project-e00rene` credential. One typed, idempotent Task Deck input request was
sent to Rene's requested Slack DM on 2026-09-17; no progress-message spam or
completion notification was sent.

Do not treat credentials as the only unfinished deliverable: after access is
restored, the Stockholm gate still needs actual deployment/client/policy
collectors, installed LibreChat orchestration and representative approved
per-model fixtures/validators. The implemented offline verifier cannot supply
or authenticate these by itself. Cosmos still needs registry/catalog/execution
promotion and real public GPU MP4/dataset cohorts, including timing, scale-from-zero,
cancellation and observed admin/usage behavior. These omissions are explicit in
each acceptance README and are not waived by the local test counts.

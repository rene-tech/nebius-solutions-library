# Stockholm MCP envelope remediation — local handoff

Status: implemented and tested locally; **not deployed or customer-ready**.
Integration branch: `agent/fs2-cosmos-stockholm-remediation-r20260917`, based on
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. The release owner must record the final
integrated commit and image after the sibling Cosmos/telemetry changes land.

Generic `invoke_model` normalizes immediate `payload.idempotency_key` and
`payload.wait_seconds` before the MCP SDK supplies optional defaults. Missing
outer controls are lifted; identical duplicates are accepted; conflicting
duplicates (including explicit zero/null) return `-32602` with structured
`gateway_control_validation` issues and `durable_admission: false`. Both
locations are validated without boolean/string coercion. The common serving
admission boundary additionally rejects any remaining immediate payload control
before serialization, persistence, or dispatch, for all serving protocols.
Nested model objects remain intact. Flat named tools and their existing wrapped
compatibility checks are unchanged.

The LibreChat agent instructions, bundled scientific-gateway skill, and skill
adaptation handoff now explicitly keep generic controls beside the payload and
prefer the discovered named tool. These are source instructions, not evidence
of an installed client image or an actual customer tool trace.

## Local evidence

Use the integration control-plane directory and its own environment:

```sh
uv sync --frozen --group dev
.venv/bin/python -m pytest tests/test_mcp_generic_controls.py tests/test_mcp_input_contracts.py tests/test_mcp_model_tools_http.py tests/test_scientific_mcp_aliases.py -q
.venv/bin/ruff check src/fs2_serve/mcp_server.py tests/test_mcp_generic_controls.py
.venv/bin/mypy --follow-imports=silent src/fs2_serve/mcp_server.py
```

- Initial dedicated suite: 69 passed in 28.77 seconds.
- Expanded run, after adding positive HTTP coverage of a nonzero nested wait:
  97 passed, 1 failed in 45.09 seconds. All 70 dedicated Stockholm tests passed.
  The failing sibling Cosmos specialized-tool test was collected during its
  integration: the process had loaded the MCP server before those handlers were
  ported. Preserve this negative result and rerun on the final integrated source.
- Ruff and targeted mypy passed for the Stockholm source and tests.
- After the Cosmos handler integration, the same expanded command passed:
  **98 passed** in 47.33 seconds, retaining all earlier negative tests. This
  supersedes the transient handler-integration failure at the MCP suite level;
  the root still owns the final frozen-release suite and deployment gate.
- Coverage includes strict control validation, duplicate conflicts before any
  admission call, named/generic/legacy replay, stored payload inspection,
  nonzero wait execution, and dispatch across all 15 non-Cosmos base catalog
  routes with both published serving protocols (`native`, `openai-chat`).
- The route matrix uses synthetic bindings, a local memory store, and a recording
  stub runtime. It proves envelope handling only; it does not qualify model
  input semantics, live model authorization, GPUs, or biological results.
- Initial collection attempts found an in-progress telemetry import and missing
  `websockets` in the older canonical environment. The integration environment
  resolved those; the canonical checkout and environment were not changed.
- Existing Starlette deprecation warning remains. Initial pytest cleanup also
  warned about unrelated old PostgreSQL socket directories; subsequent tests
  use a separate task-owned temporary root. No old files were removed by this task.

## Remaining release gate

Cluster access was reported revoked before this work. No cloud writes, key
creation, deployment, live cleanup, or runtime invocation was performed here.
The manager has requested refreshed access. After integration, the release owner
must deploy the exact final image, preserve its predecessor digest, check sibling
speech/storage/workshop features, and run the Stockholm concurrency-5 disposable
customer identity through the actual LibreChat plus installed BioNeMo skills.
Both named and generic OpenFold2/Boltz2 calls must reach validated terminal
results, with upstream debug capture proving model-only payloads, in two
consecutive unchanged-release cohorts. Those receipts remain absent.

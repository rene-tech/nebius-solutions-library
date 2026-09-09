# Typed model MCP acceptance

This change replaces opaque named-model `payload`/`request` schemas with concrete
runtime-backed inputs across the catalog. It also documents all shared lifecycle
tools and adds authorized `get_model_schema` discovery. It does not make portable
runtimes implement the full NVIDIA NIM feature set or redirect NVIDIA REST scripts.

See [client guide](../../docs/mcp-model-tools.md) and the
[pinned BioNeMo compatibility mapping](BIONEMO-COMPATIBILITY.md). Existing wrappers
and generic compatibility tools remain available. Actual model inputs are never
silently dropped to make an incompatible scientific request appear successful.

## Reproduce

From `k8s-inference/components/control-plane`:

```sh
.venv/bin/pytest -q tests/test_mcp_input_contracts.py \
  tests/test_model_input_contracts.py tests/test_mcp_model_tools_http.py \
  tests/test_mcp_contract_packaging.py
.venv/bin/pytest -q ../../acceptance/mcp-model-contracts-20260909/test_compatibility.py \
  ../../acceptance/mcp-model-contracts-20260909/test_typed_verify.py
.venv/bin/python ../../acceptance/mcp-model-contracts-20260909/verify.py
```

The last command prints an offline plan only. After verifying the exact deployed
release, an operator can run the bounded live checks:

```sh
.venv/bin/python ../../acceptance/mcp-model-contracts-20260909/verify.py \
  --execute --key-file /private/internal-test-key.json \
  --access-bundle /private/final-stack-output.json \
  --output /private/typed-mcp-r01 --release FULL_DEPLOYED_COMMIT
```

This uses an existing ordinary inference key and existing admin access; it never
creates keys, changes grants, scales capacity or replays customer data. It checks
every currently authorized serving/scientific named tool against discovery and
its published schema, descriptions and available examples. Asset-bearing examples
are contract examples, not pre-created caller uploads. No scientific batch jobs
are submitted by this verifier.

Four original synthetic serving requests run sequentially: PhenoAge, a short
Qwen response, and the existing 20-residue Boltz2/OpenFold2 contract fixtures.
Two incompatible typed requests must return field-level errors before admission,
with exact model/customer attribution in request-debug capture. Results are
retrieved through the durable operation workflow; HTTP acceptance alone is not
counted as inference success. Default operation timeout is 900 seconds, capture
timeout 60 seconds. Failures are preserved; the helper never silently retries or
cancels admitted work.

Raw captures and private operation receipts stay outside Git. Sanitized live
evidence is added here only after the deployed checks finish. The release argument
is the release owner's attestation, not image inspection performed by this client.

## Extending the catalog

Serving adapters register their concrete source-backed schema in
`src/fs2_serve/model_input_contracts.py`; package referenced JSON resources under
`model_input_schemas`. Include meaningful purpose, fields/units, supported limits,
result handling, examples and source provenance. Declare a different runtime
variant separately when its input contract differs. Rebuilding the same adapter
does not require an image-digest-specific schema change.

Scientific model contracts reuse the existing canonical profile/envelope validators
and profile examples. Independent Apps inherit their source model's contract but
retain separate routes and settings. Add schema/fixture tests with a new adapter;
an unknown contract is reported explicitly, not published as another opaque named
tool. Wheel tests ensure schemas do not disappear during packaging.

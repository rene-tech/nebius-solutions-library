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

## Live acceptance: 2026-09-09

The first typed-MCP run passed from **09:14:47.964609 to 09:16:17.867696 UTC**
on source `5de025fe58cc42812629c87694a1ff326e0ef00f`, control-plane image
`sha256:780f92b984b419aed112ced48d3df55eae1ad006091346c13f4953db23390870`.
The release owner verified API/controller rollout before GO and subsequently
confirmed all three Terraform stages had zero managed changes. The unchanged
logging UI rendered the Boltz2 Request log after rollout without UI alerts;
the earlier [logging body/download acceptance](../request-debug-20260909/README.md)
remains separately dated evidence, including its preserved initial failures.

Actual MCP `tools/list`, serving/scientific discovery and `get_model_schema`
matched for **27 authorized models and 27 named model-protocol tools**: 14 native,
2 OpenAI chat and 11 scientific. All had meaningful descriptions and concrete
flat input schemas, with no root `payload`/`request` placeholder. All 25 supplied
examples validated. AltumAge and nv-segment-ct did not supply examples because
their asset/geometry inputs require additional material; no example was invented.

Four original requests succeeded on attempt 1. These are observed operation
completion times, not throughput or cold-start benchmark claims:

| Model | Durable operation ID | Accepted to terminal |
| --- | --- | ---: |
| PhenoAge | `d6178188-c55a-4102-a397-bf067b12f59d` | 12.995279 s |
| Qwen3-8B | `4f455f9b-2ac2-4cf2-82ff-9aff02d2d8a2` | 0.945345 s |
| Boltz2 | `2df975d5-6f39-41dc-8752-132b5192e589` | 25.268213 s |
| OpenFold2 | `1283c5b1-ac39-4aac-8e0b-5808aad14ead` | 2.032633 s |

Validation checked the canonical PhenoAge value, meaningful Qwen assistant text,
and the original Boltz2 mmCIF/OpenFold2 PDB sequence and confidence contracts.
Boltz2 without MSA and OpenFold2 with unsupported `alignments` both returned
structured `-32602` errors (HTTP 400), with **no operation admitted**. Their debug
exchange IDs are `fbfdca30-2b35-42c5-b7e7-744886974942` and
`b0039db0-e852-4e40-873a-82554cf77ddc`. All six original request/error exchanges
retained exact observed bodies, model/owner/token/correlation and removed
authorization secrets. Intentional invalid-input responses are not successful
predictions or unexpected test failures.

[Credential-free live summary](live-summary.json) records all IDs, fixture/result
hashes, schema hashes and capture assertions. Private summary SHA-256:
`5e83fe74fecd9f79dbb6ea1b604a7480628ead6130db4782e1e6fe0ce1862f26`;
private inventory SHA-256:
`e6b20f6a10efbde856918fd22efae1ec68626bb54f809ba87adc8eb028ad18f7`.
The executed helper SHA-256 was
`35a9c27ec1d3acfde1a752767fff483b6a6769d1a89d1c27d2f118f951bd9a34`.
The unchanged verifier plus reused logging-helper tests passed **46 tests in
0.93 s** afterward; Ruff and formatting checks passed. Pytest emitted cleanup
warnings for unrelated old local PostgreSQL socket directories; none were removed.

The existing ordinary key was used without mutation. Both MCP and admin sessions
closed normally, and the exact verifier process ended. No scientific submission,
client replay, cancellation, key, settings or capacity change occurred. Existing
CPU/GPU services were reused; this is not new GPU, snapshot, scale-out, or full
NVIDIA NIM REST qualification. Raw synthetic results and debug documents remain
in the private acceptance directory, not in Git or stdout.

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

# BioNeMo toolkit compatibility: portable Boltz2 and OpenFold2

Source review, **not a new live qualification**. Repository baseline:
`88520758f90a7e171abd86a4a94787a6739d6ba7`. NVIDIA toolkit pin:
`0e67a612e4045f007e38fa77adc8f3ebfc5616b6` (public HEAD resolved 2026-09-09).
This lane changes no runtime, recipe, deployment, key, or capacity.

## Outcome

Use per-model MCP schemas for the **selected runtime**, not the NVIDIA model
name alone. Toolkit guidance can inform a platform-aware agent; existing REST
scripts are not automatically MCP clients. Preserve ordinary platform API-key
access, app routing, durable operation IDs, polling, and result retrieval.
Do not advertise general NIM feature parity for either portable runtime.

### Actual hosted inputs

| Contract | Portable Boltz2 | Portable OpenFold2 |
|---|---|---|
| Selected runtime | `boltz2-hf-portable` | `openfold2-upstream-portable` |
| Source | [server.py](../../models/bionemo/boltz2/server.py) | [server.py](../../models/structure/openfold2-upstream/server.py) |
| Required input | 1–8 `polymers`, each with alphanumeric `id` (1–8), `molecule_type="protein"`, canonical sequence (6–2048), and `msa.msa_search.a3m.alignment` | Exactly `input_id`, `sequence`, `selected_models=[1]`, `relax_prediction=false` |
| Sequence handling | Whitespace removed, uppercased after raw length validation | Uppercase canonical residues only, 1–1024; no normalization |
| Alignment | Explicit A3M string, length 3–33,554,432; `format="a3m"`; `rank>=0` accepted but not consumed | Upstream query-only MSA features; no external alignment/template input |
| Sampling | Recycling 1–10 (default3), steps 1–500 (default200), samples 1–8 (default1), mmCIF only | Exact single `model_3_ptm` checkpoint; no relaxation or checkpoint selection |
| Output | mmCIF structures, confidence scores and pTM scores | One PDB plus pLDDT, PAE, pTM, runtime provenance and inference duration |
| Invalid request | FastAPI/Pydantic 422 before inference; all nested unknown fields forbidden | 400 with specific parser error; 64KiB HTTP body maximum |

OpenFold `input_id` uses `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Boltz's parser
does not validate A3M biological correspondence; passing JSON Schema proves
shape, not a valid scientific alignment. Report `rank` as compatibility metadata,
not a tuning control. H100 image pins are
`sha256:93d5fae96d87dff930206cf331f170b73f2369067f0b32169e0008f0db320a90`
and `sha256:9fc70e781b18f4f547da237e2eb81387df3c38d092bb44c46145db6347e7e164`
respectively; see the [Boltz2](../../catalog/runtime/deployment-runtimes/boltz2-portable-h100.json)
and [OpenFold2](../../catalog/runtime/deployment-runtimes/openfold2-portable-h100.json)
selected-runtime declarations. Archived NIM metadata is not their input authority.

### Toolkit differences and permissible adaptations

The pinned Boltz2 reference describes optional MSA, additional molecule types,
ligands/affinity, constraints, templates and wider sampling ranges. These are
not implemented by our portable adapter. Even empty unsupported keys currently
fail validation. Preserve caller-supplied A3M exactly; explain the required nested
container. A separate, explicit query-only-MSA mode could construct an alignment
from the query, but must not imply homolog search or silently replace a supplied
alignment. Do not drop ligands, constraints, templates or affinity requests.
[Pinned Boltz2 API reference](https://github.com/NVIDIA-BioNeMo/bionemo-agent-toolkit/blob/0e67a612e4045f007e38fa77adc8f3ebfc5616b6/nim-skills/boltz2-nim/references/api.md).

The OpenFold2 hosted sequence-only example already includes the four compatible
fields. Its local example omits the required relaxation field. Other examples
request external alignments or templates. An adapter may require explicit
single-checkpoint/no-relaxation choices, but cannot discard these scientific
features or narrow an explicitly requested ensemble. Implement and qualify the
features in a separate runtime before claiming support.
[Pinned OpenFold2 examples](https://github.com/NVIDIA-BioNeMo/bionemo-agent-toolkit/blob/0e67a612e4045f007e38fa77adc8f3ebfc5616b6/nim-skills/openfold2-nim/references/examples.md),
[API reference](https://github.com/NVIDIA-BioNeMo/bionemo-agent-toolkit/blob/0e67a612e4045f007e38fa77adc8f3ebfc5616b6/nim-skills/openfold2-nim/references/api.md).

The Complexa Boltz2 refolding script has a URL override, but omits MSA and sends
`step_scale`/`write_full_pae`, both unsupported here. Its subsequent scoring
depends on outputs this runtime does not expose. Substituting a URL or returning
placeholder metrics cannot make that workflow equivalent. Its local mode also
omits authentication and its HTTP helper expects immediate model JSON, not a
durably accepted operation.
[Pinned refolding script](https://github.com/NVIDIA-BioNeMo/bionemo-agent-toolkit/blob/0e67a612e4045f007e38fa77adc8f3ebfc5616b6/workflows/generative-protein-binder-design/complexa-binder-design/scripts/boltz2_refold.py),
[endpoint configuration](https://github.com/NVIDIA-BioNeMo/bionemo-agent-toolkit/blob/0e67a612e4045f007e38fa77adc8f3ebfc5616b6/workflows/generative-protein-binder-design/complexa-binder-design/scripts/boltz2_endpoint.py).

### Recommended platform-aware client boundary

1. Discover authorized Apps and read each selected model's typed input contract,
   examples, runtime origin and unsupported-feature notes. Keep clones' public
   app identity separate from canonical model/source identity.
2. Submit the unchanged supported payload to the named typed MCP tool, or generic
   `invoke_model(model_id, protocol="native", payload, idempotency_key)`. For
   scientific-batch models use their existing manifest/artifact workflow rather
   than pretend they expose the same synchronous REST contract.
3. Preserve the operation ID; poll existing status/result tools and retrieve the
   real model result. Stable idempotency prevents a client's transport retry from
   duplicating work. Do not interpret an acceptance envelope as structure data.
4. Return actionable field errors before admission where the schema is known.
   Retain raw upstream 400/422 details in the operator debug view, without exposing
   authentication secrets. A JSON-schema rejection is distinct from GPU execution
   failure, and no valid structure/confidence is synthesized on either path.

These are integration recommendations, not a shipped replacement for the NVIDIA
scripts or a claim that the complete toolkit workflow has run. Core MCP/schema
implementation and live normal-key validation belong to the parent task.

## Offline fixtures and evidence

`compatibility_fixtures.py` loads the existing repository's original two 20-residue
synthetic probe payloads; it never sends HTTP or runs a model. `test_compatibility.py`
compiles only the current runtime's pure parser declarations (no torch/import
startup), proves those original payloads pass, and proves unsupported NIM-shaped
fields fail explicitly. There is no second production validator in this lane.

From `k8s-inference`:

```sh
components/control-plane/.venv/bin/pytest -q acceptance/mcp-model-contracts-20260909/test_compatibility.py
```

Source SHA-256: Boltz2 server
`5bb34260291dd4ff63b932b53df98801d4c641e86ae092deccc75d64c91e3302`;
OpenFold2 server
`8e11db080b0dcb4d95f7873cb1f7792d6a60cc4663bc7248f2e9ba818c08bcd4`.
Tavily discovery request IDs: `38a0c883-93a6-42f3-9b4e-9993cb26ae22`,
`b6d1e1b0-236e-4839-916b-ecf6e37cd78b`,
`e5c17e59-4726-42a8-b4ee-610cfdc6dad5`. Exact toolkit references were fetched
read-only from the pinned official GitHub source, not executed. No credentials,
customer requests, runtime outputs or third-party source copies are included.

### MCP HTTP integration follow-on

The parent-requested user guide is [Model tools over MCP](../../docs/mcp-model-tools.md).
Local HTTP/ASGI integration checks live in
`components/control-plane/tests/test_mcp_model_tools_http.py`: **6 passed**.
They exercise concrete schema discovery, descriptions for all 19 core tools,
flat/legacy idempotent serving and scientific calls, validation before admission,
debug owner attribution, and shared-model access without cross-customer operation
access. These use existing in-memory fixtures and the actual MCP HTTP SDK, not
external services or a fake claim of model inference. Run from the control-plane
directory with `PYTHONPATH=src:../../catalog/runtime .venv/bin/pytest -q tests/test_mcp_model_tools_http.py`.

Final frozen-source regression lane: **96 passed in 63.67s**. It covers every
MCP test file, `test_model_input_contracts.py`, the clean-wheel install/import
gate, and `test_mcp_contract_packaging.py` (all seven JSON resources retained
byte-for-byte and loaded from the wheel with isolated Python). All core-tool
arguments have descriptions. Sanitized JUnit receipt:
`/tmp/fs2-mcp-integration-final-r03.xml`. Earlier r01/r02 receipts are retained:
r01 exposed obsolete NIM metadata in the local HTTP fixture; r02 ran while a
temporary source-resource reference was unfinished. Neither is reported as a
clean run. The final fixture uses current selected portable runtime metadata,
without loosening the production variant check or inventing live qualification.

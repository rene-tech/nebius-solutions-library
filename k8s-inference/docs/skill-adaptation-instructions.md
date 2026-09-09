# Handoff: adapt the existing agent skills for reliable model use

Use this document as instructions for the agent maintaining the model-consumption skills.
Updated on 2026-09-09 for the deployed typed-MCP release `5de025fe`.
Canonical version: `k8s-inference/docs/skill-adaptation-instructions.md` in
`rene-tech/nebius-solutions-library`. This replaces the earlier generic-MCP
handoff. It does not claim that a LibreChat image has been rebuilt or tested.

The ready-to-copy shared skill, agent instructions, LibreChat configuration and
deployment handover are in `k8s-inference/integrations/librechat/`. Replace the
existing bundled `scientific-gateway` skill rather than installing two competing
gateway manuals. Preserve the model skills' useful domain guidance; update their
transport/schema guidance and examples against live discovery as described here.

## Objective and scope

Adapt the skills I already use so that an agent can select the right hosted model,
construct a valid request, submit it once, follow it through queues/cold starts,
retrieve usable outputs, and explain failures with actionable evidence.

Update the existing skills and their client helpers, examples and tests. Preserve
their useful domain guidance and invocation settings. Do not redeploy models,
change cluster configuration, create customer tenants/keys, or substitute a
different model/provider as part of this adaptation. Report a platform API gap
separately instead of hiding it with a workaround that changes the requested model.

The operator hosts shared Apps; customers consume them with their own API keys.
Customers do not deploy their own copies. Scientific and other models use the
same key-based model grants. Do not invent a separate academic login/token flow.
License/use metadata still matters, but academic eligibility is not something a
client may self-assert to obtain access.

## 1. Establish the actual contract, not a guessed model interface

Inspect the existing skills first. Identify the instructions and helper code that
actually issue calls; fix those, not just a separate document nobody loads.

For the target platform, discover models with the customer's own credentials:

- HTTP: `GET /v1/models` and `GET /v1/scientific-models`.
- MCP: `tools/list`, `list_models`, `list_scientific_models`, then
  `get_model_schema(model_id, protocol)` for the selected App.
- Record the exact public model ID, supported capabilities/protocols, operations,
  model revision, and active runtime variant when exposed. Keep discovery scoped
  to that customer/key; do not reuse another customer's cached model list.
- `/v1/models` includes native HTTP models as well as OpenAI-compatible models.
  Being listed there does **not** mean a model accepts chat messages.
- Every current named model tool has a concrete, flat `inputSchema`, with
  meaningful field descriptions, required fields, bounds and nested definitions.
  `get_model_schema` returns `contracts[]` with `tool_name`, `protocol`,
  `input_schema`, `examples`, `source_refs`, and `model_ref`; serving responses
  also expose `active_runtime` where available. Scientific `parameter_schema`
  may still be a reference: obtain the embedded contract from this MCP API.
- The 2026-09-09 accepted all-model key saw 27 named model/App tools and 19 core
  tools (46 total), not the earlier 45. Treat counts as dated evidence, not a
  requirement that restricted customer keys expose everything. No Kubernetes
  access or source-tree inspection is required for normal schema discovery.

Use the deployed runtime's matching schema and tested examples. If public
discovery lacks them, use the operator-supplied runtime-specific contract and
record its source/revision. Do not derive fields from a model name, a similarly
named model, or NVIDIA documentation for an independently packaged runtime.
If an essential contract cannot be obtained, identify that precise missing piece
instead of repeatedly sending speculative payloads.

Current endpoint handoff, configurable rather than hard-coded into each skill:

```text
HTTP base: https://89.169.99.188/v1
MCP:       https://89.169.99.188/mcp
```

Use a shared client configuration, for example `FS2_BASE_URL`, `FS2_MCP_URL`,
and `FS2_API_KEY` or a configured secret-file reference. These are suggested
client conventions, not new server settings. Never embed actual keys in skills,
examples, command history or source control. Use the customer's inference key,
not an admin bootstrap credential.

## 2. Keep the three invocation paths distinct

| Model contract | HTTPS call | MCP call |
| --- | --- | --- |
| OpenAI-compatible serving | Advertised route such as `/v1/chat/completions`; normal model-specific OpenAI payload | `invoke_model` with the exact advertised protocol, e.g. `openai-chat` |
| Native HTTP serving | `POST /v1/models/{model_id}:invoke` with `{"operation":"<advertised operation>","payload":{...}}` | `invoke_model` with `protocol: "native"` and the **inner model payload**, not the HTTP wrapper |
| Scientific batch profile | `POST /v1/models/{model_id}:submit` with the scientific run document | `submit_scientific_run` with `model_id` and `request` containing that document |

For new MCP integrations prefer the named tool returned by `get_model_schema`.
Pass its model fields directly, plus optional `idempotency_key` and (serving
only) `wait_seconds: 0`. Do not wrap them in the HTTP `operation`/`payload`
envelope, and do not add `model` to a named OpenAI-chat tool: the App selects it.
Legacy named `payload`/`request` wrappers remain accepted and validated for
compatibility; do not describe them as the primary contract. The generic
`invoke_model` remains available with `model_id`, `protocol`, `payload`,
`idempotency_key`, and `wait_seconds`. Generic scientific submission still uses
`submit_scientific_run(model_id, request, idempotency_key)`.

Use a real Streamable HTTP SDK, the exact `/mcp` path (no trailing slash), normal
bearer authentication and TLS verification. Let the SDK negotiate MCP protocol
versions and headers; do not hand-build a JSON-RPC POST with missing transport
headers. Refresh/reconnect tool discovery after this update and after changing
keys. Server-side tool lists are private and have zero cache TTL.
If a native model exposes several operations that the generic tool cannot select,
use its documented operation-specific tool or the HTTP wrapper's explicit
`operation`; do not guess which operation the generic tool selects.

Do not call the internal Pod's `/biology/...` path on the public gateway. That
upstream path and the public `/v1/models/{id}:invoke` path are different layers.

## 3. Correct the known Boltz2 / OpenFold2 traps

These examples match the **current portable adapter source**, not every possible
deployment of these model names. They are minimal synthetic schema examples,
not meaningful scientific predictions or new live acceptance evidence. Recheck
the active runtime before making them the skill's default.

### Boltz2: `boltz2`, native operation `predict`

HTTP body:

```json
{
  "operation": "predict",
  "payload": {
    "polymers": [
      {
        "id": "A",
        "molecule_type": "protein",
        "sequence": "MKTAYIAKQRQISFVK",
        "msa": {
          "msa_search": {
            "a3m": {
              "alignment": ">query\nMKTAYIAKQRQISFVK\n"
            }
          }
        }
      }
    ]
  }
}
```

The adapter requires `polymers[]`, a protein sequence and the nested
`msa.msa_search.a3m.alignment`. It forbids unknown fields. A top-level `sequence`,
a plain `msa` string, a FASTA filename, or an assumed `use_msa_server` switch is
not this contract. The current adapter accepts protein polymers; do not promise
arbitrary ligand/DNA support from the upstream model's general capabilities.
The single-query alignment above is only a smoke fixture: do not silently
replace a customer's requested MSA workflow with it.

### OpenFold2: `openfold2`, native operation `predict-structure`

HTTP body:

```json
{
  "operation": "predict-structure",
  "payload": {
    "input_id": "skill-smoke-openfold2",
    "sequence": "MKTAYIAKQRQISFVK",
    "selected_models": [1],
    "relax_prediction": false
  }
}
```

This adapter requires exactly those four inner fields. It accepts canonical
uppercase amino-acid sequences of 1–1024 residues, requires `[1]`, and requires
`relax_prediction: false`. Despite the inherited upstream endpoint name, the
current runtime uses a single-sequence MSA representation without external
templates. Do not send guessed MSA/template fields or advertise those inputs as
supported. Do not silently substitute OpenFold3, AlphaFold3 or ESMFold.

For named MCP tools, pass only the inner model fields above to
`boltz2_predict_native` or `infer_openfold2_native`, plus submission controls.
For legacy generic MCP, put the inner body into `invoke_model.payload` and
supply `model_id` and `protocol: "native"` separately. Label HTTP examples
explicitly so the model does not send their outer wrapper to a typed MCP tool.

Boltz2 does not support NIM ligand/affinity/template/constraint/full-PAE features
on this portable runtime. Do not promise affinity or full PAE output, or leave
NVIDIA Complexa refolding scripts unmodified: changing their base URL alone
does not adapt inputs, authorization, asynchronous results or required outputs.
Read the pinned `BIONEMO-COMPATIBILITY.md` in the typed-MCP acceptance directory.

An upstream 400/422 is not sufficient proof that the skill was wrong: an adapter
can also map an execution error to those statuses. Inspect the actual error
detail and submitted payload before assigning blame.

## 4. Give each model a small, verified reference

Keep common discovery, authentication, polling and error handling in one shared
client/helper if the existing skill layout supports it. Keep model-specific
contracts in the existing model skill or a lazily loaded reference. Avoid
duplicating the entire platform manual across 20 skills.

For each model, include:

- Exact public ID and runtime/revision to which the reference applies; documented
  aliases and distinctions from similarly named models.
- Supported task, protocol and operation; required inputs, field types, bounds,
  units, sequence/chain conventions, file formats and unsupported inputs.
- One complete minimal valid request where feasible, plus an expected result
  shape and a basic output validator. Clearly distinguish illustrative examples
  from live-tested ones. Use the API's examples first; full AltumAge CpG data
  and NV-Segment imaging inputs require real assets, not invented tiny examples.
- Input artifact preparation/upload steps when needed, and output download and
  parsing steps. Explain what is inline JSON versus an artifact pointer.
- Relevant errors, how to correct the specific input, and when to ask for
  operator help. State measured timing only with its date/runtime/test scope;
  otherwise say that timing is unknown.

Prioritize `proteina-complexa`, `boltzgen`, `mosaic`, `bindcraft`, `rfdiffusion`,
`esmfold2`, `esmfold2-fast`, `protenix-v2`, and `alphafold3`, as well as the
recently failing `boltz2` and `openfold2` skills. Then cover all other models
actually exposed to the customer's key, including Qwen, Cosmos, the other
BioNeMo/structure/media/imaging models, AltumAge and PhenoAge where present.
Do not treat this historical list as proof of current availability or completeness.
GLM is outside the current H100 scope.

Keep `openfold3` and `openfold3-openbind` distinct. Preserve the exact selected
ESMFold variant and the deployed scientific/runtime identity of Mosaic rather
than assuming a publication name guarantees a particular implementation.

## 5. Scientific files and batch requests must be real

Use each profile's advertised operations, service classes and parameter schema.
Typical checked-in examples use `customer-batch`, but only use a class that the
current caller is allowed to select.

Prepare and upload the actual input bytes, finalize the uploads, and construct
the required input manifest from the returned immutable artifact pointers.
Calculate hashes and lengths from those bytes. Follow the platform's canonical
manifest serialization. Use the existing scientific acceptance client as a
reference for materialization; do not copy its provisioning or admin workflows.

Checked-in `public-request.json` files often contain fixture artifact IDs and
manifest hashes. They are templates, **not ready-to-submit customer requests**.
Replace them with this customer's actual finalized artifacts. Do not send local
filesystem paths, invent artifact IDs, or reuse another tenant's upload handles.

For bulk work, use model-native batching only if the schema supports it;
otherwise submit independent jobs with a common workload/batch correlation and
distinct item IDs. Use bounded concurrency, respect throttling, and leave
admission/scaling to the platform. Do not change min/max replicas, node groups,
priorities or quotas from a customer model-use skill.

## 6. Submit once; resume through queueing and cold starts

- Generate an explicit idempotency key per logical request. Preserve the same
  key **and same payload** for transport retries or uncertain submission results.
  The current contract accepts keys of 8–200 characters. Keep a private local
  mapping from workload item to key, payload hash and returned operation ID.
- A deliberate new run or corrected payload uses a new key. Do not retry a
  completed failed operation endlessly with its old key, and do not create a new
  key just because a live operation is queued or loading.
- Parse both immediate model responses and `202 Accepted`. Capture
  `x-fs2-operation-id`, the returned operation ID and `Location` when supplied.
  An accepted submission is not a completed prediction. This distinction is for
  HTTP clients; MCP submission returns an operation/status document, not an
  immediate raw model prediction, even when the worker completes quickly.
- Follow `GET /v1/operations/{id}` / `get_operation`; scientific jobs also expose
  `get_scientific_status` and `list_scientific_events`. Handle the actual serving
  versus batch status document shapes instead of assuming identical nesting.
- Treat queued/loading/running states as ongoing work. Respect `Retry-After`
  where present; otherwise use bounded backoff and jitter. Keep interactive
  status messages useful but infrequent. A client wait deadline should produce
  a resumable operation ID, not a duplicate job or a claim that the model failed.
- On terminal success, retrieve and validate the result. `GET .../result`,
  `get_operation_result` and `get_scientific_result` may have different wrappers;
  normalize them deliberately, including JSON, text, binary and artifact outputs.
- Download required artifacts and verify their declared hashes. Do not call
  `acknowledge_operation` / `:acknowledge` before the user has their outputs:
  acknowledgement purges the ordinary operation payload/result. Do not
  automatically cancel ongoing work because a short client timeout elapsed.

Snapshot restore and warm/cold placement are platform concerns. Do not invent
payload flags such as `gpu_snapshot=true`, keep a GPU hot with unsolicited test
calls, or promise a cold-start time that has not been measured for that runtime.

## 7. Make failures useful instead of turning them into retry loops

| Observation | Required client behavior |
| --- | --- |
| Local schema failure | Explain the exact field/type/format problem before submission. Do not guess missing scientific inputs. |
| MCP `-32602`, `data.type: model_input_validation` | No run was admitted. Read `issues[]` JSON-pointer fields and missing/allowed/expected constraints, refresh the schema and correct inputs. Never blindly retry. |
| HTTP 400/422 or other MCP invalid arguments | Preserve the response and inspect its detail. Correct a demonstrated input error; do not blindly repeat the same payload. |
| HTTP 401/403, unavailable model or policy error | Check endpoint/key selection and current discovery; do not switch tenants, use an admin key, or invent an academic flag. |
| HTTP 404 for an operation/artifact | Check the saved ID, owner context and retention; do not assume another customer's object is accessible. |
| HTTP 409 | Inspect its error code: conflicting idempotency payload, unfinished result, expired/purged result and other conflicts require different actions. |
| HTTP 429, retryable 503 or transport interruption | Reuse the same request identity where appropriate, honor backoff, check an existing operation, and stop at the configured deadline. |
| MCP HTTP 200 | Check JSON-RPC errors, SDK `isError`/`is_error`, structured content and the operation's final status; HTTP 200 alone is not inference success. |
| Terminal failed/preempted/expired run | Preserve the evidence and distinguish completed platform retry attempts from a user-authorized new submission. Never report success from acceptance alone. |

For support, retain endpoint/method, public model ID, runtime revision if known,
UTC timestamps, request/operation IDs, idempotency key, attempt, HTTP/MCP outcome,
elapsed client time, and the submitted request/returned error in an appropriate
private debug record. Exclude authentication secrets and signed download tokens.
Do not discard the upstream response merely because it is not JSON or not 2xx.

Give the user a short summary and correlation IDs; keep complete request/response
data privately available for authorized debugging rather than dumping it into
chat or source control. Do not require customer skills to have admin API access.
Full request/response and upstream-attempt debug capture is deployed and enabled
on the retained H100 platform as of 2026-09-09. Authorized operators find it in
Admin → Apps → Runs / request logs, including errors with no operation ID.
Customer skills need no admin access. Capture redacts credentials and records
partial/unread bodies honestly; earlier uncaptured payloads cannot be recovered.
Other deployments must enable the feature before making the same promise.

## 8. Install into the actual LibreChat agent

Use the deployment handover and templates in `integrations/librechat/`.
Keep the chat LLM/provider credential separate from the model-gateway key.
Use a per-user MCP key for a shared multi-customer LibreChat: one global key
attributes every user to the same platform identity. Do not assume forwarding
LibreChat user IDs changes gateway authorization or billing.

Load the replacement `scientific-gateway` skill and its references via the
deployment's actual skill loader, enable skills and MCP tools on the saved
agent, and install the supplied routing instructions in that agent. Files in
an image alone are not proof the agent can see them. Refresh previously saved
tool/schema selections; keep discovery, status, result and artifact tools
available alongside the named model tools.

The existing workbench aliases are `BIONEMO_MCP_URL`/`BIONEMO_MCP_API_KEY`,
while some skills use `SCIENTIFIC_MODELS_*` or `FS2_*`. Map these deliberately;
they are not automatically interchangeable. With per-user custom variables the
gateway key is not a global shell environment variable. Its artifact/file bridge
must use the same caller identity, not silently fall back to a shared env key.

An uploaded chat attachment is not a finalized gateway artifact. Provide a
file-capable helper that reads the real attachment bytes, hashes/uploads them,
finalizes the manifest and stores downloaded outputs for the current user.
Keep multi-megabyte base64 out of the LLM context; honor the reservation's
`max_content_bytes`, HTTP/MCP envelope limits and signed-handle expiry. A skill
is instructions, not an automatic file bridge, workflow scheduler or viewer.

## 9. Verify behavior and deliver a clear readiness report

First run local schema/helper tests. Cover native-versus-MCP envelope differences,
missing/extra fields, artifact rematerialization, accepted-to-completed polling,
MCP tool errors inside HTTP 200, bounded retries and idempotent resume. Use
synthetic fixtures and mocked failures for error cases; no need to generate
intentional failures against customers' running workloads.

Then, within the explicitly authorized test scope, run the smallest known-good
input against each model using the intended non-admin customer key. Verify an
actual usable result, not only discovery or an accepted job. Test a mixed-model
sequence and a small bounded batch, including resume from a retained operation
ID. Do not scale down shared Apps merely to manufacture a cold start. If live
testing is not authorized or cannot run, mark it pending rather than claiming it
passed. Do not run large design campaigns as skill acceptance tests.

Deliver updated skill files/helpers and a per-model table: exact model/runtime,
contract source, example/validator, local-test result, live operation ID/date,
result validation, and any remaining blocker. Record "not tested" explicitly.
Explain which failures were caused by skills, stale documentation, missing API
schemas, or actual backend behavior. Do not label everything a skill issue.

## Source map for the adaptation agent

Repository: `https://github.com/rene-tech/nebius-solutions-library`, solution
`k8s-inference`. Local checkout, when available:
`/home/tux/nebius-solutions-library-inference/k8s-inference`.
Resolve these paths against that solution directory:

- `components/control-plane/src/fs2_serve/api.py`: public discovery, native
  envelope, operation/result handling.
- `components/control-plane/src/fs2_serve/mcp_server.py`: actual tool arguments,
  native dispatch, asynchronous operation and artifact tools.
- `components/control-plane/src/fs2_serve/model_input_contracts.py` and
  `mcp_input_contracts.py`: selected-runtime schemas and typed tool validation.
- `catalog/runtime/deployment-runtimes/`: selected portable model contracts;
  pair these with the deployed runtime identity, not merely the base catalog.
- `models/bionemo/boltz2/server.py`: Boltz2 request models and supported fields.
- `models/structure/openfold2-upstream/server.py`: OpenFold2 `parse_request`.
- `catalog/runtime/schema/`: scientific run, artifact, manifest and per-model
  parameter schemas.
- `models/**/activation/public-request.json` and adjacent manifests: scientific
  example shapes; fixture references must be materialized for the current owner.
- `acceptance/scientific-fleet/run_acceptance.py`: existing artifact/submission
  reference flow. `models/aging/fixtures.py`: explicit synthetic aging fixtures.
- `docs/SERVING_ACCESS.md`: shared Apps and current key-based model access.
- `docs/SCIENTIFIC_BATCH_API.md`: artifact/run mechanics with the shared key
  policy. `docs/mcp-model-tools.md`: named typed inputs, schema discovery,
  all 19 workflow tools and errors.
- `acceptance/mcp-model-contracts-20260909/RELEASE.md` and `live-summary.json`:
  deployed source, 27 live contract checks, four successful synthetic serving
  calls, two invalid-input checks. Not a new live scientific-fleet campaign.
- `acceptance/mcp-model-contracts-20260909/BIONEMO-COMPATIBILITY.md`: pinned
  upstream toolkit differences and the unsupported Complexa refolding path.
- `docs/request-debug-logging.md`: debug capture contract and its explicit
  deployment/retention limitations; check live status separately.

The public MCP schema API now supplies the deployed contracts. Remove stale
claims that no schema API exists, that named tools have only generic objects,
or that Cosmos/Evo2/imaging field names must be guessed by live GPU probes.
If an essential feature is absent from the published contract, report that
specific gap instead of substituting a different scientific workflow.


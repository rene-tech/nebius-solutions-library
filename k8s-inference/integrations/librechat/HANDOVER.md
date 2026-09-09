# Handover: LibreChat with fs2 MCP and deployment skills

Prepared 2026-09-09 for the retained H100 platform and typed-MCP source
`5de025fe58cc42812629c87694a1ff326e0ef00f`. This is the deployment handover;
it is not evidence that a new LibreChat instance has been built or accepted.

## Service to connect

| Item | Current retained deployment |
| --- | --- |
| MCP URL | `https://89.169.99.188/mcp` (exact path, no trailing slash) |
| Inference HTTPS base | `https://89.169.99.188/v1` |
| Transport | stateless MCP Streamable HTTP |
| Authentication | per-user ordinary inference API key as bearer token |
| Admin console | `https://89.169.99.188/admin/` (operators only) |

TLS verification currently succeeds. An unauthenticated `/mcp` request returns
`401`, which is expected. Never copy an admin bootstrap token or one customer's
key into the LibreChat image, `librechat.yaml`, a skill, source control, logs, or
chat. Obtain each user's model-access key through the platform administrator.

## Recommended LibreChat baseline

The inspected workbench base is LibreChat commit
`3f27726e10bdd35d98f5fbbae7aab35af94c8436`, package `0.8.8-rc2`. Its config
schema is `1.3.15`; this number is not the application version. That pin supports
Streamable HTTP MCP, `customUserVars`, server instructions and filesystem-loaded
deployment skills. Preserve the exact pin for a reproducible first deployment,
or revalidate this handover against a newer LibreChat release before upgrading.

Merge `librechat.example.yaml` into the deployment config. Set only the public
endpoint as an environment variable:

```text
SCIENTIFIC_MODELS_MCP_URL=https://89.169.99.188/mcp
```

The `Authorization` header deliberately uses
`Bearer {{SCIENTIFIC_MODELS_API_KEY}}`: double braces are a current user's
custom variable; `${...}` is a shared deployment environment variable. With
`startup: false`, the server is initialized after that user enters a key in MCP
Settings. `requiresOAuth: false` chooses the platform's PAT/bearer flow rather
than LibreChat OAuth detection. Do not add a second static Authorization header.

A deliberately single-customer/private LibreChat may use one injected global
key, but then every chat user is the same billable platform identity. Sending a
LibreChat email/user header does not alter gateway ownership. A multi-customer
deployment must use per-user keys so model grants, run ownership, usage and
billing remain attributable.

An all-workflow key normally needs these scopes in addition to its App allowlist:

```text
catalog.read inference.invoke mcp.invoke operations.read operations.result
operations.cancel
```

Cancellation may be omitted if the UI does not offer it. Ordinary serving
operations are additionally tied to the exact submitting key, so keep that key
active until its results are collected. There is no separate academic token or
customer-deployed copy of a model; the same allowlist includes or excludes
licensed/academic Apps. Access does not grant the underlying license.

## Install the skill and agent

1. Copy `skills/scientific-gateway/` to
   `$DEPLOYMENT_SKILLS_DIR/scientific-gateway/`. Remove or replace the older
   skill of the same name; two conflicting gateway manuals are worse than none.
2. Keep `interface.skills.use: true`, the Agents `skills` and `tools`
   capabilities, and `skills_enabled: true` on the saved workbench agent.
3. Put `AGENT_INSTRUCTIONS.md` on that saved agent. Enable the
   `bionemo-models` server and all catalog, schema, operation, result and artifact
   tools—not just model invocation tools.
4. Restart LibreChat after changing filesystem deployment skills. Confirm the
   startup log reports that the skill directory was loaded.
5. Each user opens MCP Settings, supplies their personal gateway key, and
   initializes `bionemo-models`. Reconnect after a key or catalog change because
   tool discovery is caller-specific and uncached.

LibreChat formats MCP tool IDs from the raw tool name and server name. On the
inspected pin it produces names such as
`get_model_schema_mcp_bionemo-models`. Do not seed the old
`scientific_models__get_model_schema` prefix. Prefer selecting the MCP server in
Agent Builder or persist the actual IDs returned by that LibreChat version.
Schema/tool lists can vary per user, and independent App clones can be added.

Deployment skills are loaded read-only from one subdirectory per skill. In the
inspected image the relevant pattern is:

```dockerfile
COPY skills /app/skill
ENV DEPLOYMENT_SKILLS_DIR=/app/skill
```

Loading a `SKILL.md` does not install its Python dependencies, rewrite vendor
scripts, enable a Code Interpreter, or bridge files. Verify those facilities
separately if the workbench advertises them.

## MCP behavior the agent and UI must preserve

- Discover with `list_models` / `list_scientific_models`; obtain exact fields,
  examples, source references and selected runtime through `get_model_schema`.
- Prefer each returned named typed tool. Model fields are flat; submission
  controls remain top-level. Generic envelope tools remain for compatible clients.
- The all-model acceptance on 2026-09-09 saw 27 named model/App tools plus 19
  core workflow tools. A user with a restricted allowlist correctly sees fewer.
- Serving and scientific submissions are durable. Save the operation ID, poll
  the proper status tool and fetch the final result/artifacts. A long LibreChat
  tool timeout is not a substitute for this flow.
- `MCP-Protocol-Version` and modern routing headers vary by request; let
  LibreChat's MCP client set them. Do not configure static versions/method/tool
  headers in `librechat.yaml`.
- MCP may return HTTP 200 with a JSON-RPC error or tool `isError`. Inspect
  structured content and the final operation state, not HTTP status alone.
- Invalid typed input returns JSON-RPC `-32602` with
  `data.type: model_input_validation` and precise field issues before admission.
- There is no OpenAI token-streaming guarantee and no guarantee that a listed
  App is already warm. Do not repeatedly submit through a cold start.

## File and artifact bridge requirement

The main MCP is enough for inline model inputs and small artifact transfers, but
a smooth scientific workbench also needs a file-capable helper. LibreChat
attachments exist in LibreChat storage; the remote gateway cannot read their
local path. The helper must:

1. resolve a file that belongs to the current LibreChat user;
2. stream and hash its real bytes outside the LLM context;
3. reserve an upload with model ID, digest, byte count, media type and compression;
4. use the returned upload handle for large data, or base64 MCP upload for small data;
5. finalize it and return only immutable artifact metadata to the agent;
6. build/upload/finalize canonical scientific manifests;
7. download caller-owned results, verify digest/size, and store them in a
   user-isolated workspace with a useful downloadable UI link.

The helper must call upstream with the same per-user gateway key. If it is a
second MCP server, declare its own sensitive `customUserVars` value or implement
an explicit authenticated handoff from LibreChat; do not fall back to a global
`BIONEMO_MCP_API_KEY`. Keep local workspaces user-scoped. Signed object handles
and bearer tokens must never enter model context or ordinary logs.

The old `artifact-mcp.py` in the inspected workbench is **not compatible**: it
still calls `clawbio_upload_create` / `clawbio_model_fetch`, assumes 32-character
hex IDs and an `/upload/v1/` service. The fs2 gateway uses UUID upload/artifact
identities and the begin → put/handle → finalize → read/download tools. Do not
ship the old bridge as though it were operational; replace it or initially
disable file-workflow claims.

Default server limits are 16 MiB for a raw MCP/HTTP request and 16 MiB decoded
inline artifact content. Base64 plus JSON overhead means MCP can upload just
under 12 MiB of binary in that default request. Large uploads use the returned
presigned handle. MCP inline reads return base64; large reads use a download
handle or authorized HTTP stream. The source defaults also include a 600-second
signed-handle lifetime, 24-hour ordinary payload/result TTL, seven-day operation
metadata retention and 90-day scientific-artifact retention. Read the actual
response ceilings/expiry timestamps because deployments can override defaults.

## Current catalog families

Always use the caller's discovery rather than hard-coding this list. The dated
all-model contract check covered:

- scientific batch: AlphaFold3, BindCraft, BoltzGen, ESMFold2,
  ESMFold2-Fast, Mosaic, OpenFold3-OpenBind, Proteina-Complexa, Protenix v2,
  RFdiffusion, and one independent cloned App;
- native: AltumAge, Boltz2, Cosmos3-Nano, DiffDock, Evo2-40B, GenMol, MolMIM,
  MSA Search PDB70, NV-Segment CT, OpenFold2, OpenFold3, PhenoAge,
  ProteinMPNN, and SDXL;
- OpenAI-chat: NV-Reason-CXR-3B and Qwen3-8B.

Every listed App has a typed contract. Twenty-five have a validated embedded
example. AltumAge and NV-Segment CT deliberately have no fake tiny example:
they require complete CpG or imaging inputs. GLM is not part of this retained
H100 deployment.

NVIDIA BioNeMo Agent Toolkit skills remain domain references, not an invocation
standard. Their direct NVIDIA REST URLs, authentication and immediate-response
assumptions are not transparently redirected by installing this MCP. The current
portable Boltz2 and OpenFold2 differences are recorded in the skill reference
and `acceptance/mcp-model-contracts-20260909/BIONEMO-COMPATIBILITY.md`.

## Acceptance before handing it to users

Run these checks with synthetic, non-sensitive inputs and the intended normal
user keys. Do not use the admin token.

- Build and start the pinned LibreChat image; confirm config validation, skill
  load, MCP initialization after user-key entry, and no credential in logs.
- With user A, verify discovery, `get_model_schema`, one typed Qwen call, one
  typed native call, durable polling and final result. Verify the admin Apps →
  Runs view attributes them to A and records real timing.
- Submit one intentionally schema-invalid synthetic request; confirm no
  operation is created and the agent explains the field issue without retrying.
- Complete one small scientific flow: local file → finalized input/manifest →
  typed run → status/events → published result → verified downloaded artifact.
- Disconnect/reconnect LibreChat while a run is queued; confirm it resumes from
  the saved operation ID and does not duplicate the submission.
- With user B, verify a different allowlist, separate run ownership and no
  access to A's operation/artifact. Confirm the file helper uses B's key and
  isolated directory.
- Exercise a large file through the signed-handle path rather than chat/base64.
- Change an App's settings in the admin console, reconnect, and confirm updated
  discovery/schema without rebuilding the skill image.

The gateway itself already passed typed contract parity for 27 named tools,
four new synthetic successful calls (Qwen, PhenoAge, Boltz2, OpenFold2) and two
pre-admission invalid-input checks at source `5de025fe`. That does not replace
the LibreChat-specific multi-user and file-bridge acceptance above.

## Troubleshooting and support data

Give users the operation/run ID and a short error summary. For an unresolved
failure, collect UTC time, endpoint/server name, App/model ID, public user/key
ID (never the secret), LibreChat conversation/request correlation, idempotency
key, operation ID, submitted schema version, terminal state and returned error.

Operators can inspect complete captured requests/responses and upstream attempts
through Admin → Apps → Runs/request logs on the retained platform. The capture
is access-controlled, redacts credentials and may mark unread/disconnected
request bodies partial. It has no automatic retention/deletion job today; do
not promise a duration. Earlier requests from before capture cannot be rebuilt.

## Primary references

- [LibreChat MCP configuration](https://www.librechat.ai/docs/configuration/librechat_yaml/object_structure/mcp_servers)
- [LibreChat MCP features and per-user connections](https://www.librechat.ai/docs/features/mcp)
- [LibreChat deployment skills](https://www.librechat.ai/docs/features/skills)
- [Pinned LibreChat MCP schema](https://github.com/danny-avila/LibreChat/blob/3f27726e10bdd35d98f5fbbae7aab35af94c8436/packages/data-provider/src/mcp.ts)
- [Pinned LibreChat deployment-skill loader](https://github.com/danny-avila/LibreChat/blob/3f27726e10bdd35d98f5fbbae7aab35af94c8436/packages/api/src/skills/deployment.ts#L427)
- [fs2 model-tool client guide](../../docs/mcp-model-tools.md)
- [Scientific batch quick start](../../docs/SCIENTIFIC_BATCH_API.md)
- [Typed-MCP release acceptance](../../acceptance/mcp-model-contracts-20260909/RELEASE.md)

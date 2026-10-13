# Native endpoint gateway integration — 2026-10-13

Source-only handoff for `fs2-s2-native-endpoint-gateway-r20261013`, linked to
`fs2-serverless2-full-migration-r20261013`. No deployment, customer credentials,
old-cluster mutation or Git commit was performed by this worker.

## Included

- `endpoint_gateway.py` adapts the existing request-authenticated
  `ServerlessClient`; it never substitutes a tenant service/admin key.
- `gateway.py` exposes the 13 packaged native contracts alongside GROMACS.
  Existing GROMACS job lifecycle and request-scoped storage-broker paths remain.
  Root's tenant/user storage-mode implementation is preserved.
- `mcp_server.py` registers the 13 exact named, flat-field tools from
  `endpoint_catalog.py`. `tools/list`, catalog and schemas are intersected with
  the caller's model grants and current S2 `GET /v1/models` native sync bindings.
  Tool availability is computed per request, never by removing shared tools.
- Explicit S2 scopes cover catalog, invocation, artifact upload, operation/result
  reads and GROMACS control. Imported `legacy.original_scopes` (including the
  nested metadata representation) preserve the application `mcp.invoke` gate.
  Unscoped non-imported keys retain S2's existing unrestricted-scope semantics.
- Native artifact pointers are resolved only through the authenticated owner's
  `GatewayStore`. Binary/large outputs become immutable artifacts; raw encoded
  media do not enter MCP context. Receipts and results remain owner-isolated.

## HTTP surface

- `GET /v1/models`: legacy catalog envelope `{object: "list", data: [...]}`.
- `GET /v1/scientific-models`: retains the pilot `{models: [...]}` envelope.
- `GET /v1/models/{model_id}/schema`: exact contract, runtime image from live
  S2 metadata, observed regional state and explicit unsupported capabilities.
- `POST /v1/models/{model_id}:invoke`: accepts the legacy native request shape
  `{operation: "<declared operation>", payload: {...typed native fields...}}`.
  Returns `{model_id, operation: {id, status}, result}` and
  `x-fs2-operation-id` with the **actual S2-generated ID**.
- Existing artifact upload/finalize/download routes accept these native Apps.
- `GET /v1/operations/{s2-sync-id}` and `/result` read owner-scoped completed
  receipts. These are publication receipts, not a replacement execution queue.

The native timeout is configurable through `Settings.native_timeout_seconds`,
default/max 900 seconds, matching the migration endpoint compiler. The transport
waits another 15 seconds for the response envelope. No automatic submit retry.

## Explicit compatibility gaps / deployment gates

1. These native tools support **synchronous execution only**. Legacy durable
   asynchronous wait handles and invocation idempotency are not implemented by
   S2 native sync. Idempotency/wait/deadline headers or tool options are rejected
   before admission, never silently discarded. The response envelope above is
   explicit; this is not full legacy asynchronous-response compatibility.
2. A timeout may have unknown admission. It returns `retryable: false`; callers
   must not automatically resubmit. In-flight native cancellation and recovering
   a result after losing the entire S2 HTTP response remain S2 lifecycle gaps.
3. Once S2 returns an operation ID, publication errors preserve it in REST/MCP
   errors (and the HTTP operation header). If receipt storage itself is down,
   the returned ID cannot promise a durable local lookup. Artifact publication
   failures can leave a pending receipt, but never report completed delivery or
   repeat inference.
4. Source schemas and synthetic transport tests are not inference/capacity
   qualification. Each native image still needs authorized internal-tenant
   inference and failure/scale qualification on the isolated new fleet. An
   observed binding, or zero ready replicas, is not a claim of hot readiness.
5. Only the packaged 13 endpoint contracts are exposed. Other catalog Apps need
   their own approved adapter/binding; no alias deduplication or phantom catalog
   entries are introduced. GROMACS remains the separately configured pilot and
   public `gromacs` maps to `Settings.model_id`: that actual ID must match the
   imported grant and deployed S2 model before activation.
6. The existing pilot `/v1/scientific-models` response remains unchanged, not the
   old broad platform's separate scientific discovery format. Full catalog and
   old client compatibility must be qualified by the parent migration task.

## Test evidence

```sh
uv run --project k8s-inference/components/scientific-ai --with pytest \
  pytest -q k8s-inference/components/scientific-ai/tests
```

Result: **336 passed, 1 skipped**. The 18 new endpoint integration cases cover
REST and MCP, all 13 typed tool schemas, key/model/scope filtering, disappearing
bindings, private artifact input, sibling-owner denial, binary output download,
actual S2 IDs, original-key forwarding, 900-second timeout, unsupported execution
options, upstream failures and storage-publication faults. GROMACS/native-MD and
tenant/user-storage regressions remain passing. Ruff passes on all four touched
implementation/test files. The skipped test is pre-existing, not native endpoint
qualification. This handoff requires root deployment/real-runtime acceptance.

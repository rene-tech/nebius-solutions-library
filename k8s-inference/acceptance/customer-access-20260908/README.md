# Shared customer access acceptance

The operator deploys Apps and their runtimes once. Customers share those
runtimes; the API key's `models` grant is the same access mechanism for serving,
scientific and academic model Apps. Tenant, user and key identify customer data
and usage, not a separate customer deployment. This harness does not set user
App grants, academic eligibility, special `use.*` scopes or model policies.

This is a prepared live check, **not a claim that every model has been run**.
The release manager runs it after deploying the shared-access implementation.

## Run

Use the existing control-plane environment, with the normalized private access
bundle. Both commands log in to the admin API and explicitly log out afterward.
Use fresh output directories; existing receipts are never overwritten.

```bash
cd /home/tux/nebius-solutions-library-inference/k8s-inference/components/control-plane

# Set this to the full commit actually deployed, not an image-tag prefix.
customer_release=DEPLOYED_FULL_COMMIT
customer_private=/home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/customer-access-20260908

PYTHONPATH=src:../../catalog/runtime .venv/bin/python \
  ../../acceptance/customer-access-20260908/customer_access.py provision \
  --access-bundle /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json \
  --key-file "$customer_private/kopra-key.json" \
  --output "$customer_private/provision-r01" \
  --release "$customer_release"

PYTHONPATH=src:../../catalog/runtime .venv/bin/python \
  ../../acceptance/customer-access-20260908/customer_access.py accept \
  --access-bundle /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json \
  --key-file "$customer_private/kopra-key.json" \
  --output "$customer_private/accept-r01" \
  --release "$customer_release"
```

The secret is only in the owner-readable `kopra-key.json` (`0600`, outside the
repository). Never print, commit or attach that file. It contains the origin,
owner, user/key IDs and one-time `secret` for the final private customer handoff.
Normal stdout contains only mode, outcome, release and completion time.

### Provisioning

`provision` ensures `tenant_id=kopra`, `principal_id=kopra`, display name `Kopra`.
It reuses the existing owner, changing only the display name if needed. It creates
one retained `kopra-customer-default` key with `models=["*"]`, concurrency one,
no new budget or expiry, and these ordinary scopes:

```text
catalog.read inference.invoke mcp.invoke artifacts.write
operations.read operations.result operations.cancel operations.acknowledge
```

There are no administrator, token-management, academic or non-commercial
access scopes. Any license/usage information remains model documentation.

Re-running provisioning with a fresh receipt directory reuses the same named
key and private file. It does not rotate, broaden or revoke an existing key.
If the named key exists but its one-time secret is missing, mismatched, revoked
or has a different policy, the harness stops for operator recovery without
creating another key. A crash after server issuance but before local persistence
therefore cannot silently cause a duplicate on the next run. Local provisioning
is file-locked; run only one manager across hosts because the API does not make
key names globally unique.

### Acceptance boundaries

`accept` requires the existing retained user/key and never modifies them. It:

1. Compares HTTP `/v1/models` and MCP `list_models`, requires every enabled
   shared App and specifically PhenoAge, BindCraft and AlphaFold3. Cold models
   must remain discoverable. Paused Apps are not required. This cluster check
   assumes enabled Apps are operator-shared, not deliberately private targets.
2. Confirms the inference key cannot access the admin context.
3. Submits exactly one original synthetic clinical PhenoAge CPU fixture using
   the ordinary native route. The qualified fixture hash is unchanged. An exact
   HTTP replay must return the same durable operation ID. Actual HTTP and MCP
   results must match each other and the previously measured formula result.
4. Creates one disposable key for a different customer with only `qwen3-8b`
   allowed. PhenoAge invocation must be denied over HTTP and MCP, without an
   accepted HTTP operation. It then changes **only this disposable key** to
   allow PhenoAge, confirms discovery and proves Kopra's operation and result
   are still inaccessible over both transports. This isolates customer data
   ownership from the model grant itself.
5. Verifies the exact operation's tenant/principal in the App Run and positive
   accepted-window usage for the Kopra user. User totals may include separately
   coordinated requests; they are not asserted to contain only this harness.
6. Revokes the disposable key in `finally` and verifies its next request gets
   HTTP 401. The retained Kopra key is **never revoked**, even on failure.

No scientific job, GPU model request, App configuration, node, quota or cluster
mutation is performed. The negative test has a valid PhenoAge payload: if a
grant regression unexpectedly accepts it, preserve that extra operation and
report failure rather than counting a clean one-operation cohort. Real academic
inference and artifact isolation require the release manager's separate
qualified scientific-fixture check; discovery alone is not a runtime proof.

The existing PhenoAge policy handles any cold activation and normal idle grace.
The default terminal polling bound is 1,020 seconds, configurable with
`--timeout-seconds`; there is no hidden request retry or automatic resubmission.
An accepted request is written to `accepted.json` before replay. A failed run
leaves Kopra's accepted operation alone for diagnosis rather than cancelling
customer work. Inspect `outcome.json` and numbered HTTP/MCP receipts before
deciding whether another fresh cohort is appropriate. Transport failures and
internal MCP errors do not count as successful access denial.

Timings retain the raw OperationView (including unknown/null fields), actual
accepted-to-terminal elapsed time, status transitions and the external client
clock. The external clock includes replay, polling and result checks and must
not be described as model compute time or a new GPU snapshot benchmark.

## Offline checks

```bash
PYTHONPATH=src:../../catalog/runtime .venv/bin/pytest -q \
  ../../acceptance/customer-access-20260908/test_customer_access.py
.venv/bin/ruff check ../../acceptance/customer-access-20260908
```

Initial implementation: 16 tests passed. Checks cover exclusive `0600` secret
persistence, redacted HTTP disclosure receipts, retained-key reuse/no duplicate
on missing secret, unchanged owner metadata, original fixture identity,
disposable-key cleanup after failure, same-model cross-customer result probes
and genuine MCP policy errors versus internal failures. All are mocked/offline;
they create no cloud resources and are not live acceptance evidence.

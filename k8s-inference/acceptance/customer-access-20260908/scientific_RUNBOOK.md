# Ordinary-key academic acceptance

Status: live `scientific-r01` passed on 2026-09-08 at 16:54:28.638378 UTC,
release `21bccafaf889fa09475eaa4de00c4dfa0cf48d05`. Exact scope and receipts follow below.

`scientific_access.py` reuses the existing `scientific-fleet` client and accepted
BindCraft/AlphaFold3 fixtures without parameter or artifact changes. It runs one
logical request per model, sequentially, using Kopra's retained ordinary key.
This is a technical platform smoke test, not new biological optimization or a
clinical validation. No model settings, capacity, keys, or deployment resources
are changed by this helper.

## Offline checks

From `k8s-inference`:

```sh
components/control-plane/.venv/bin/python acceptance/customer-access-20260908/scientific_access.py
cd components/control-plane
.venv/bin/python -m pytest ../../acceptance/customer-access-20260908/scientific_access_test.py -q
.venv/bin/ruff check ../../acceptance/customer-access-20260908
```

Preparation opens only repository fixtures, never the supplied private paths.
It verifies the manifest/input digest chain. The 19 offline tests cover pinned
request identities, the release gate, caller attribution, same-model discovery
before isolation probes, failure preservation without retries, redacted traces,
and session closure on both success and failure.

| Model | Original canonical request SHA-256 | Input bytes |
| --- | --- | --- |
| BindCraft | `f2ed7f1380f0ef8e60432afe13b33d871e7a30d93b81ffc5bcd9a59e1f1d8dda` | Original `bindcraft-native/activation` manifest and qualified PDB fixture |
| AlphaFold3 | `60c8c9a81f1b99c32999335640d1b6df28a2d18dfa6b955db9d1dcb25bf04cdc` | Original `batch-adapters/alphafold3/activation` manifest and `af3-fold-input.json` |

Each input is uploaded afresh under Kopra. Replacing old artifact IDs with the
new owner-scoped upload pointers necessarily changes the submitted manifest's
digest; the source fixture bytes and biological parameters do not change.

## Release-gated execution

Run **only after root supplies an explicit GO and the exact deployed full source
SHA**. Provisioning the customer/key and verifying rollout belong to root's
separate lane. The output directory must be fresh and outside the repository.

```sh
components/control-plane/.venv/bin/python acceptance/customer-access-20260908/scientific_access.py \
  --execute \
  --deployed-source '<DEPLOYED_FULL_SHA>' \
  --run-id customer-access-scientific-r01 \
  --key-file /home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/customer-access-20260908/kopra-key.json \
  --access-bundle /home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/final-stack-output.json \
  --output /home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/customer-access-20260908/scientific-r01
```

The retained key must be active, wildcard-model, and have exactly the ordinary
scopes from `customer_access.py`; no `use.*` or academic entitlement is added.
The script preserves the key. Credentials are held in memory and never placed
in commands, stdout, or receipts. Admin authentication is used only for session
login/logout and existing key/App-run/User-usage reads, not to submit or fetch
the scientific result.

## Assertions and evidence scope

- Both models appear in Kopra's HTTP scientific discovery. Both original HTTP
  submissions complete, with exact idempotency replay returning the same
  operation. Replays are deliberate tests, not retries after a failure.
- Existing model-specific receipt validation, output publication, all attempt
  resource-release checks, and SHA/size-verified download of the output manifest
  plus one bounded actual output remain enabled.
- Each result also matches an ordinary-key `get_scientific_result` MCP read.
  This proves MCP result usability, not submission of these two runs over MCP.
- Admin App-run metadata retains Kopra tenant/principal and the exact retained
  key prefix. The explicit run-time User-usage window contains two scientific
  logical runs; upload/poll/replay exchanges do not inflate this count. Do not
  submit other Kopra scientific runs concurrently with this bounded check.
- The existing default-tenant inference key first discovers the same models,
  then must receive 403 or 404 for each Kopra operation, result, and both
  downloaded artifact contents. A 401, missing model, 429, or server/transport
  failure does not count as successful data isolation. No inference is issued
  by the second key.

Private output includes the immutable plan, original delegated model receipts,
submitted IDs, per-model summaries and verified projections, bounded HTTP/MCP
traces, and final `outcome.json`. These contain existing technical fixtures, not
customer payloads. Only intentionally selected credential-free projections
should later be copied into the repository; raw admin traces stay private.

The default bound is 1,800 seconds per model. A failure is retained and stops the
sequence; no hidden inference retry or automatic cancellation is performed.
If a request has been accepted before failure/timeout, report its retained ID
to root for scoped disposition. Do not invent resource-cleanup success. Every
new authorized attempt needs a fresh output directory and run ID, preserving
earlier negative evidence.

## Executed r01 result

Both original technical fixtures passed on the exact root-verified deployed source
`21bccafaf889fa09475eaa4de00c4dfa0cf48d05`. The helper started at
16:44:47.924581 UTC and exited 0 at 16:54:28.638378 UTC. There were no client
failures, resubmissions after failure, configuration changes, or key changes.

| Model / durable operation | Accepted → terminal (UTC) | Accepted-to-terminal | Delegated client wall time |
| --- | --- | --- | --- |
| BindCraft `a96c8dad-4591-425f-b6b7-c27baa18f458` | 16:44:53.919894 → 16:52:59.230392 | 485.310498 s | 492.438 s |
| AlphaFold3 `1e104004-a899-4cd6-966b-753ef26b8b7a` | 16:53:10.756787 → 16:54:17.574741 | 66.817954 s | 76.123 s |

Client wall time includes input upload, exact replay, polling and output validation;
it is not cold-start or GPU-compute time. Both GPU stages were admitted to existing
`inference-h100-reserved-8x` capacity, using the shared `fs2-academic-poc` execution
namespace. BindCraft's design stage was admitted at 16:44:54 and completed at
16:52:36.358770; this stage interval is not a fine-grained compute measurement.
Both models' CPU and GPU attempts succeeded and reported resource release.

For each model, the original semantic validation passed; the manifest and one
actual bounded output were downloaded with exact SHA/size verification; the MCP
result read matched HTTP; the exact retained key prefix and `kopra` tenant/principal
were verified through admin attribution reads. The independent same-model customer
received 404 on the operation, result, manifest content and selected output content:
eight expected denials total. Both original submissions were HTTP; MCP was used for
result parity, not a second logical submission.

The explicit owner usage window contained exactly two logical requests, both
scientific and succeeded, with failed/cancelled/pending/running all zero. Uploads,
polls and exact replays did not add logical runs. The admin session was logged out,
the retained customer key was not changed, and helper session 37306 exited normally.
A root-requested extra read-only BindCraft progress check is preserved separately;
it already observed terminal success and did not submit or retry any work.

Private evidence root:
`/home/tux/.local/state/k8s-inference-dual-acceptance/h100/releases/customer-access-20260908/scientific-r01`.
No raw private receipt or key is copied into this repository.

| Evidence | SHA-256 |
| --- | --- |
| `outcome.json` | `d824ce6cdbc641eb375e164e5961316d2685c98f8ca9eef69a690e01d3ef5fd8` |
| `bindcraft.json` | `0a95e45736e6180af7c761f9ae5531b9885fa3897b3c8968c73751b404a575d2` |
| `alphafold3.json` | `722aa88862b451d4695e511030ba5cc908fa26d21533763879a890c704692838` |
| Executed `scientific_access.py` | `59b2ca3c62f389bc432a4d63e422f276dc7263ecf0d018622ae3dde07c097789` |
| Final import-formatted `scientific_access.py` | `3f9283e378e67ce24c445495ef7024f700a12b6d903715ad2c82bf6d18b31011` |
| Unchanged delegated `run_scenario_acceptance.py` | `a47132c73d497d739c19da95e9f62f7694de1ade02ade8447a9b3cf1e1d7203a` |

The two import-order lint findings were fixed only after live execution completed;
no behavior was changed and no live run was restarted. With the control-plane
working directory/configuration, final whole-directory Ruff checks pass and all
19 scientific helper tests pass (0.79 s). Root separately recorded 44 combined
acceptance tests. Earlier unrelated failed customer-access attempts remain in
their original evidence directories and are not reclassified by this pass.

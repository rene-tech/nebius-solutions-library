# Scientific app acceptance

One Protenix-v2 app uses the existing accepted fixture, unchanged parameters and
input bytes. HTTP and named MCP replays must resolve to the same operation.
Acceptance also checks semantic results, exact artifact downloads, app-specific
history/usage, preservation of the source settings and clone-only pause.

## r01: real pre-submission failure, preserved

Release: `d21439d025c806f6a3bcb4167c57b95d84ff923f`.
Window: 2026-09-08 12:29:41.088702–12:29:42.523683 UTC.

- Admin login succeeded; app creation returned 201.
- Created app: `95943840-d2b1-4a65-b5cd-aa4717b2cdc3`.
- Public route: `app-95943840d2b14a65b5cdaa4717b2cdc3`.
- Source default app: `3eea3bf2-8846-5e13-bba1-a0765cd12172` (`protenix-v2`).
- Immediate settings read returned 503 `scientific_controller_unavailable`,
  request `6eec41aa-98a2-4256-a29a-e536948a6faf`. Cleanup settings read also
  returned 503. Logout returned 204.
- No input upload, inference operation, GPU workload or temporary key was
  created. The app could not yet be paused; no cleanup success is claimed.
- Original source settings were not written. No request was silently retried.

Cause: an admin-session request on another API replica did not refresh the new
scientific app's in-memory source mapping. The durable policy row was visible,
but startup-policy projection could not resolve its public app route. Public
PAT authentication refreshed this inventory; admin authentication did not.

Correction: refresh the inventory at `AppScientificModels.list_models` before
catalog/policy projection. A deterministic second-replica test fails before the
fix and passes afterward. Focused Apps/scientific-admin tests: 40 passed, two
PostgreSQL tests skipped in that local invocation. Ruff and scientific recipe
identity checks pass. Independent real-PostgreSQL regression is coordinated
with the database test lane; release and rerun remain root-owned.

The parallel historical acceptance check also found upload bookkeeping counted
as model runs: 28 scientific inference operations plus 56 artifact-upload
operations appeared as 84 runs. Apps now exclude protocol
`scientific-artifact-upload-v1` from logical counts, tokens/user/time buckets,
lifetime Last Used, paginated Runs and app Run detail. Filtering happens before
pagination. The optional store query filter defaults to empty, so the original
global operation history remains unchanged; observed HTTP traffic still counts
actual upload exchanges separately. Focused regression verifies two inference
operations remain two across exact replays and pagination, a later upload does
not change Last Used, and all four original operations remain in global history.
Apps/scientific/admin focused suite: 43 passed. Independent real PostgreSQL
confirmation passed within a 27-test combined suite: newer upload leaves
logical count two, tokens 6/14, owners/status/timeline and Last Used unchanged;
one-item pages contain both inference operations and an empty third page,
while unfiltered history includes the newer upload. Strict mypy for all seven
changed source modules, Ruff and scientific recipe identity checks also pass.

Private outcome receipt:
`h100/releases/admin-apps-20260908/scientific-clone-r01/outcome.json` beneath the
local dual-acceptance state root. SHA-256:
`0d631c2c922f606687e551d34062691d503e59a0dcb653058dc713f5b57dcb3c`.

## r02: actual runtime identity failure, preserved

Release: `2d170292037386f339fdc96fcf115a07adcf6932`.
Window: 2026-09-08 13:00:30.274296–13:00:59.480566 UTC.

The retained app's settings, scientific discovery and named MCP discovery now
work. The unchanged tiny fixture was uploaded and operation
`ec70dbac-dce9-414d-a50b-025963a12035` was accepted at
13:00:36.736948 UTC; exact HTTP replay returned the same operation. The existing
academic key already allowed this route, so no temporary key was created.

CPU stage `prepare-data` failed with an application error before any GPU stage
started. At 13:00:50.907461349 UTC, the retained scientific-stage log reports:
`runtime localization marker model_id differs from the image contract`.
The operation failed at 13:00:53.898308 UTC with HTTP status 422; by the final
13:00:59 poll, its failure result was published and its only attempt reported
resources released. Source settings remained unchanged. The client exited 1;
there was no retry, cancellation, new key, model-source change or capacity change.
The harness conservatively left the clone policy unchanged after its terminal
failure exception; it does not claim clone-pause cleanup occurred.

Cause: the app renderer passed its public app identity into the runtime
localization marker, while the qualified immutable Protenix image expects
canonical `protenix-v2`. The correction is confined to `apps_scientific.py`:
all model/init-container marker copies use the canonical image identity, while
signed capabilities, operation/tenant/attempt identity, dispatch and public
accounting stay app-scoped. Source validation, runtime arguments, stage bindings,
weights and scientific recipe hashes are unchanged. Both Job and JobSet layouts
are covered. The regression executes the actual unchanged image validator for
CPU and GPU stages and reproduces the original rejection as a negative control.

Focused Apps, scientific handoff and secondary-adapter verification: **102
passed**. Ruff, strict mypy and scientific recipe `--check` pass. Acceptance
history checks now compare against the retained app's existing runs: a future
rerun must add exactly one logical operation, not erase the failed r02 record;
four offline harness tests pass. Deployment and another live test remain pending
the release owner's signal.

Private evidence beneath `h100/releases/admin-apps-20260908/scientific-clone-r02/`:

- `outcome.json`: SHA-256
  `94c7894c14a527b2e4f0db047dcb01d921b9ecb61d0b58432c07bb93236a89f7`.
- `prepare-data-loki.json`: SHA-256
  `7d6c76aecac725db191e942d29574f83bd7ab03300480b2a4ec3d1e2a6d794f7`.
- `public-http.jsonl` retains acceptance, exact replay and every public status
  transition; `submitted.json` retains the original operation receipt.

## r03: real independent scientific app passed

Release: `a6963e1fbe70c1043d59a285c0ff2df99d0e0305`, control-plane digest
`sha256:921f0bb1a91e62df2b35626865ea8c6b7976412ab030bf62f936e86c4f5dd3bf`.
Window: 2026-09-08 13:18:48.004350–13:20:07.444344 UTC.

The same retained app accepted new operation
`a8b7f5ad-4851-468c-8f98-78f50ed03608` at 13:18:55.024539 UTC and completed
successfully at 13:19:59.859385: **64.835 seconds accepted-to-terminal**.
Both original qualified stages succeeded on their first attempt: CPU
`prepare-data`, then one H100 for `sample-structure` on the existing reserved
pool. Both attempts reported resources released. No source recipe, fixture,
policy or capacity was modified to make the run pass.

HTTP discovery, named MCP discovery, exact HTTP and MCP replay, terminal MCP
status/result, semantic result validation and artifact download hashes all
passed. Replays resolved to the one original operation ID. Apps history/usage
retained the real failed r02 run and added exactly one successful logical run;
upload bookkeeping was not counted. Source app history remained separate and
its desired settings were unchanged. Only the test clone was paused afterward.
The existing academic key already covered its route, so no temporary key was
created or another key changed. Logout returned 204 and the owned client exited 0.

Retained GPU-stage logs independently confirm actual snapshot use:
`scientific_snapshot_request` with `mechanism=cuda-criu-restored` on the exact
`fs2-sample-structure-main-a1-c571d62c4d05` workload. Private `snapshot-loki.json`
SHA-256: `71e54234655da9399d55ab0705a671db3d1b802f83d71d49398d5d3b6cbac42d`.
This is runtime evidence, not inference from an available startup option.

Exact downloaded outputs:

- Manifest: 1,006 bytes, SHA-256
  `85df9a2aa8d7cc4a6586ffb59f35f8d2a665206f1d5c20fa391fd584aee01884`.
- Selected JSON output: 706 bytes, SHA-256
  `d4a05f8d45e5d2aee66fd24f5c3c70bad9c4778444780ba6c999fea0ad5011f5`.

Private r03 `outcome.json` SHA-256:
`beeec6396e9b67d19aaff3c6fda027f729e205d98824d1be26d4aa48104b4877`.
`scientific-receipt.json` SHA-256:
`990018bead0199ca40653e2ca138c6c1fb583f0e731abab2fa91897e9ef5eda9`.
Original r01/r02 failures remain untouched and are not reclassified as passing.

## Original resume procedure (completed by r03)

After the release owner's explicit ready signal, run `scientific_app_clone.py`
with a fresh private output directory and
`--existing-app-id 95943840-d2b1-4a65-b5cd-aa4717b2cdc3`. This reuses the owned
pre-submission clone, rather than creating another app. The original r01
and r02 receipts remain untouched. If the original scientific key cannot address the
new route, the test may issue one clone-scoped expiring key under its existing
owner, then revoke it and confirm HTTP 401. No owner settings or other keys are
changed.

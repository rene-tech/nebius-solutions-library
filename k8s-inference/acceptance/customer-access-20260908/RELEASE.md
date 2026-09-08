# Shared customer access release — 2026-09-08

Deployment, public customer-access acceptance, and the additional real BindCraft
and AlphaFold3 requests all passed. Kopra's ordinary wildcard-model key is
retained and active; there is no separate scientific customer entitlement.

## Deployed release

| Item | Exact identity / result |
| --- | --- |
| Core source | `21bccafaf889fa09475eaa4de00c4dfa0cf48d05`, pushed to `origin/main` |
| Control-plane image | `sha256:3bc7e4c8028c8f97a720d730cfa386b2d787a42f09d21be1072fbef069b75c22` |
| Admin UI image | `sha256:b2ab6738d36b06b753d795da0b8aacf8d9bd6d9de90c42fd5229a957815e80da` |
| Rollout | API, model controller and admin UI each 2/2 Ready, confirmed by release owner |
| Post-apply Terraform | Infrastructure, foundation and workloads: zero managed changes, confirmed by release owner |
| Discovery inventory | 27 discoverable App routes / 26 distinct model types: 16 serving routes and 11 scientific-batch routes, including an independent clone |

The outcome's canonical `available_app_ids` list contains 26 IDs; the combined discovery set contains 27 routes, including scientific clone `app-95943840d2b14a65b5cdaa4717b2cdc3`. These counts do not assert an independently audited enabled-state inventory. GLM remains unsupported on the H100 deployment. A discovery pass is not an inference benchmark of every listed model.

The private zero-change plan JSON SHA-256 values are infrastructure `69497b18e2a958b2c20017b3ae906d622830fee0a34a0ffaecebe7ce685e37ab`, foundation `90914e6ae6bdae855846405bfed1ebf48b2e9b426a702f3a5dfd53568cc6d820`, and workloads `8a696d4992ca196bbf01eb8092712bd594525ee2ceac2490d8e0b2658b79f5df`.

## Access and ownership

The operator deploys and manages shared Apps. Customers do not need a deployment per tenant. An API key's model list grants access to public App route IDs, subject to the existing functional endpoint scopes, key validity and general enabled-user/App restrictions. Serving and scientific models use this same customer access mechanism; ordinary scopes cover catalog discovery, inference/MCP invocation, artifacts and operation/result access as applicable.

Deployment ownership does not replace the caller's identity: operations, idempotency, results and usage remain attributed to the customer tenant, principal and key. Granting another customer the same model does not grant access to an existing customer's operation or result.

`academic_eligible` and license/research-use classifications are informational, not additional customer invocation entitlements. Required platform assets remain an operator-side prerequisite for running a model; their availability does not require an additional academic customer grant. Legacy `use.nonclinical` and `use.noncommercial` scope values remain accepted for token compatibility but are not implicit invocation requirements.

For compatibility only, explicitly private deployments or nonempty principal allowlists retain their existing restrictions. Those identities are tenant-qualified; a matching principal name in another tenant is not sufficient. All 16 live managed serving deployments checked for this release use legacy `Tenant` visibility with empty principal allowlists, so this exception affects none of the current Apps. No new grant field or per-tenant deployment requirement was introduced. Runtime qualification, readiness, disabled-state and revision/public-route fences remain enforced. See [Serving access](../../docs/SERVING_ACCESS.md).

## Verification

| Check | Result |
| --- | --- |
| Full backend suite | 1,840 passed; 4 skipped; 93 deselected |
| Full UI suite | 200 tests across 32 files passed; production build passed |
| Root-focused tests | 75 passed |
| Terraform tests | 17 passed |
| Public/scientific helper offline checks | 44 passed |
| Public customer access `accept-r02` | Passed at `2026-09-08T16:44:25.826547Z` |
| Real scientific BindCraft | Passed; operation `a96c8dad-4591-425f-b6b7-c27baa18f458`, 485.310498 seconds accepted-to-terminal |
| Real scientific AlphaFold3 | Passed; operation `1e104004-a899-4cd6-966b-753ef26b8b7a` |

The successful public run verified HTTP/MCP discovery parity separately for serving and scientific catalogs, then their combined set of 27 App routes. It completed a real synthetic Clinical PhenoAge request, exact replay and matching HTTP/MCP results. The operation `fad5a193-406d-40b9-9502-a67298f40749` succeeded under tenant/principal `kopra`; App Run and user usage attribution identify Kopra. Accepted-to-terminal time was 13.023781 seconds; client time including checks was 20.912014 seconds. These are this CPU acceptance request's timings, not a GPU cold-start benchmark. A restricted disposable key was denied, and subsequent access to the same model still did not expose the original customer's operation/results.

The disposable key was revoked and revocation checked; the admin session logged out. The retained customer key was not revoked. This public run performed no cluster or model-settings writes. Its private `accept-r02/outcome.json` has SHA-256 `6e5c8c4a4d5deafcaedc22836346b932751912dcde4b7a4684f6d08acc29dbc6`. Only credential-free summaries are published here; keys, headers and customer fixtures remain outside the repository. See the [acceptance protocol](README.md) and [scientific runbook](scientific_RUNBOOK.md).

The earlier `accept-r01` failure is preserved: its harness incorrectly compared serving-only discovery with the combined serving/scientific inventory and stopped before inference. It is not relabeled as a passing attempt or a model execution failure. `accept-r02` is the separately recorded corrected run.

BindCraft completed at `2026-09-08T16:52:59.230392Z` using Kopra's same ordinary
key and the unchanged qualified technical fixture. Replay, semantic output and
publication checks, hashed download of the manifest and actual output, MCP
result parity, customer/key attribution, resource release, and another
same-model customer's operation/result/artifact denials all passed. This is a
bounded current-release access/runtime check, not a new scientific validation
or cold-start benchmark.

AlphaFold3 passed the same checks with its unchanged technical fixture. Both
scientific operations completed on existing H100 capacity. Each same-model
comparison-customer operation, result and two artifact reads returned 404;
the customer owning those resources successfully retrieved them. The bounded
Kopra user-usage window contains exactly two scientific requests and two
successes, with no failed, cancelled, running or pending requests. Exact replay
did not create extra logical work. Both requests released their attempt
resources, the admin session logged out, and the retained key was unchanged.

Scientific acceptance completed at `2026-09-08T16:54:28.638378Z`. Its private
`scientific-r01/outcome.json` has SHA-256
`d824ce6cdbc641eb375e164e5961316d2685c98f8ca9eef69a690e01d3ef5fd8`.
No cluster, model-setting, capacity or quota changes were made by these checks.

## Recipe and qualification provenance

This release changes shared scientific execution authorization code. That code participates in the runtime-recipe hash, so the current source hashes differ from the hashes attached to the earlier qualification records. The model image pins and historical receipts must not be presented as fresh qualification of an identical current recipe.

| Model | Historical recorded recipe SHA-256 | Current release source recipe SHA-256 |
| --- | --- | --- |
| BindCraft | `ad5f9d682c0c0c9174f3f2029a5dca208095284a88806a927334d9b8c1125355` | `632d0091b81129e31038733bb6a28123844f4af5f8e39986a1d31fb61c871298` |
| AlphaFold3 | `71a42e0cf48fe3eb755c5065bcdc68bf04c95786731811e1761e1e6dc8d8dcdf` | `a401c2325f731343283a8b5120c986fb92ce827fe9eb6d31930ff3e85264a7f3` |

The retained [scientific workload contract](../../catalog/runtime/contracts/scientific-workload-profiles.json) records BindCraft qualification at `2026-09-06T22:53:07.977893Z` and AlphaFold3 at `2026-09-06T22:44:00.468714Z`. Current hashes above were calculated with the repository's existing `runtime_recipe_sha256` helper, with no differences from the deployed core commit in its relevant source paths. No historical qualification receipt was rewritten for this report. The passing real scientific checks establish their own bounded current-release evidence; they do not retroactively change those historical identities or qualify every catalog model.

Earlier performance and scale-to-zero measurements remain available in the [aging release report](../aging-20260908/RELEASE.md) and their original receipts. This authorization release does not claim new all-model startup benchmarks, GPU snapshot qualifications or a new capacity envelope.

# Stockholm customer receipt verifier

Implemented scope: an executable **offline receipt consistency verifier** and
its tests. This is **not a complete live acceptance runner**, and no Stockholm
customer acceptance receipt has been produced. The live deployment remains
unverified while cluster credentials are unavailable.

`verify_receipt.py` builds per-App capability manifests using the existing
[`customer-readiness/capability_gate.py`](../customer-readiness/capability_gate.py)
identity, schema, freshness, and verdict logic. It follows the Cosmos customer
runner's pattern of requiring separate actual LibreChat evidence tied to the
exact operation IDs. Raw MCP SDK success cannot satisfy that client path.

## Run it

From `k8s-inference` with the control-plane environment installed:

```sh
components/control-plane/.venv/bin/python acceptance/stockholm-customer-20260917/verify_receipt.py \
  --release-identity /secure-evidence/release-identity.json \
  --discovery /secure-evidence/stockholm-discovery.json \
  --team-policy /secure-evidence/stockholm-team-policy.json \
  --client-identity /secure-evidence/librechat-client-identity.json \
  --receipt /secure-evidence/stockholm-cohorts.json \
  --librechat-trace /secure-evidence/librechat-trace.json
```

The paths are placeholders for real sanitized exports, not bundled fixtures.
The program emits a JSON report with required scenarios, separate App verdicts,
explicit Cosmos exclusions, and `verification_scope: offline-receipt-consistency`.
Exit status is 0 for a consistent qualifying receipt, 2 for missing/failed
evidence, or 3 for invalid input metadata. Omit the two receipt arguments to
inspect the generated matrix; every App then remains `not-ready`.

The report's `ready` value is conditional on the supplied receipts being genuine
and complete. This offline tool cannot authenticate manually authored evidence,
discover omitted live failures, verify collector provenance, or prove that a
claimed artifact validator ran. It must not be used as an independent live
customer-readiness attestation. The release owner owns that combined verdict.

## Required inputs

All files are sanitized metadata with strict allowed fields. Keep credentials,
raw prompts, scientific inputs, outputs, signed download URLs, and private logs
outside these files. Hashes reference separately retained authorized evidence.

| Input | Required content |
|---|---|
| Release identity | Existing capability-gate identity: exact 40-character source revision; SHA-256 image map including `control-plane`, `librechat`, and every covered App; configuration, client-build, tenant-policy, and public-tool-catalog hashes; model revision; HTTPS public endpoint. Include every other deployed sibling image in the map too. |
| Discovery | Schema `fs2-serve.nebius.ai/stockholm-discovery/v1`, matching `tool_catalog_sha256`, and `apps`. Each App has the fields in `APP_FIELDS`: exact public App/source-model/revision/runtime identities, serving or scientific kind, operation, discovered named and generic tools, and promised workload states. |
| Team policy | Exact fields in `POLICY_FIELDS`: tenant, sorted model grants/scopes, concurrency exactly 5, request/GPU budgets, and rate limits. The release identity contains the canonical JSON SHA-256 of this object. Administrative scopes are rejected. |
| Client identity | Exact SHA-256 values for `client_build_sha256`, `installed_skills_sha256`, and `agent_configuration_sha256`, obtained from the installed LibreChat deployment/agent. |
| Cohort receipt | Schema `fs2-serve.nebius.ai/stockholm-customer-receipt/v1`; release identity before/after; discovery hash; actual team-equivalent policy and client identity; disposable canary principal, key fingerprint and expiry; two complete ordered cohorts. |
| LibreChat trace | Schema `fs2-serve.nebius.ai/stockholm-librechat-trace/v1`, producer `librechat-agent-trace`, exact installed client/endpoint/caller identity, and the actual operation/request/conversation/message/tool/argument-shape/argument-hash/terminal/loaded-skill records. |

Use `digest()`'s canonical JSON encoding when computing the policy and discovery
hashes: sorted keys, compact separators, UTF-8, no non-finite numbers, prefixed
with `sha256:`. This is distinct from hashing an arbitrary pretty-printed file.
The source `model_revision` in the multi-App release identity should identify
the pinned model inventory; each discovery App also carries its exact revision.

Discovery must account for every team grant. A missing grant blocks the report;
do not shrink the policy to conceal an untested or unavailable App. Explicit
Cosmos source-model rows are listed as externally owned exclusions and generate
no passing coverage. No model name, biological fixture, or runtime call is
invented by the verifier.

## What the verifier checks

For every non-Cosmos serving App, both cohorts need named MCP, generic MCP,
legacy nested-control generic MCP, named LibreChat, generic LibreChat, and
direct public API cases. Scientific Apps require the same cases except the
serving-only legacy-control envelope. Scientific cases additionally require
verified input-upload hashes and observed queueing. Each App uses its discovered
tool/operation identity; the verifier does not substitute model families.

Every case carries unique operation and request UUIDs, accepted/completed
timestamps, replay-to-the-same-operation identity, terminal semantic and artifact
validation, fixture/result hashes, captured upstream field names, actual workload
states, and hashes of runs/logs/metrics/container/usage evidence. Reserved serving
controls in captured upstream fields fail the gate. The collector must preserve
all attempts and unexpected operations rather than presenting a successful
retry as a clean initial call.

The two cohorts must be consecutive, nonoverlapping, fresh, and on identical
before/after release identities. The verifier calculates operation overlap from
timestamps: each cohort must reach exactly five concurrent durable operations
with at least two distinct Apps active at that point, and must never exceed the
team's concurrency limit. Queue time counts as an admitted active operation;
this is not a claim that five GPUs executed simultaneously.

Each cohort must attest responsive platform checks, catalog refresh, usage
reconciliation and retry behavior, with zero unexpected restarts, unexplained
warnings, manual recoveries or resource leaks. Missing or extra scenario rows,
reused operation IDs, failure outcomes, stale/future receipts, identity drift,
missing artifacts/correlation, and incomplete client traces fail closed.

LibreChat records must come from the actual saved agent and installed
`scientific-gateway`/BioNeMo skills. They must match the exact operation and
request IDs, public endpoint, canary fingerprint, discovered named/generic tool,
and flat/outer-envelope shape in the cohort receipt. A raw SDK receipt renamed
as a client receipt is not acceptable evidence; the release owner must retain
the corresponding actual client traces and provenance.

## Still required before live acceptance

The following pieces are **not implemented by this directory**:

1. Trusted exporters for live deployment/image/config/model identity, complete
   customer discovery, team policy, and installed LibreChat/skill fingerprints.
2. A disposable same-policy Stockholm canary lifecycle and verified ordinary
   caller identity, without using the operator's or Rene's credentials.
3. A live orchestration driver for actual LibreChat plus raw SDK/public API
   routes, including five-way scheduling, polling, discovery refresh, bounded
   retries, and failure/cancellation behavior.
4. Approved, bounded, model-specific synthetic inputs and semantic/artifact
   validators for every advertised serving and scientific App. No fake
   all-model fixture inventory is supplied here.
5. Collectors for client traces, upstream shapes, complete attempts, lifecycle
   identity, usage reconciliation, warning/restart/resource counts, and cleanup.
6. Two real unchanged-release cohorts and exact final-image evidence, followed
   by root-owned release evaluation and a human-readable customer handoff.

Existing model/public acceptance helpers can provide pieces of this work, but
must be adapted and exercised through these actual paths before they count.
Until these inputs exist, the overall Stockholm release-gate task remains
partial and the customer-ready claim remains false.

## Local tests and source handoff

```sh
components/control-plane/.venv/bin/python -m pytest \
  acceptance/stockholm-customer-20260917/test_verify_receipt.py \
  tests/test_customer_capability_gate.py -q
```

2026-09-17 result: **41 passed in 0.11s** (31 dedicated tests plus 10 existing
capability-gate tests). Ruff and CLI help passed. Tests use only two synthetic
App metadata fixtures and one explicit Cosmos exclusion; they never invoke a
model or establish coverage of live Apps. An initial test fixture accidentally
shared mutable release identities across cohorts; that fixture was corrected,
and the release-drift rejection remains covered.

Integration branch `agent/fs2-cosmos-stockholm-remediation-r20260917`, base
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. The root agent owns the final commit
and frozen suite. This directory made no cloud writes, created no canary, and
performed no live inference or cleanup. Credentials were already reported
revoked; the manager has requested refreshed access.

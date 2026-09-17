# Stockholm customer receipt verifier

Implemented scope: an offline receipt consistency verifier, read-only live
deployment/team/client collectors, an explicit disposable-canary issuer, and a
bounded resumable OpenFold2/Boltz2 SDK/HTTP driver with representative ESMFold2
batch readback. This is **not a complete customer release gate**. Raw client
passes never establish actual LibreChat or all-App readiness.

Authorized sandbox2 access was restored on 2026-09-17. The first live attempt
completed three OpenFold2 MCP operations (named, generic, legacy nested), each
with in-flight and terminal replay plus semantic PDB validation, against CP
image `sha256:1cf5df3c2a7b11207eed3f939cb3c2b12b2c5920e3b67a52da098104bb7ab8df`.
Its HTTP attempt then stopped before recording an operation: the driver used
the base NIM operation `predict` instead of the live portable discovery's
`predict-structure`. The driver now obtains the operation from live discovery.
The negative receipt is retained. Further
submissions were paused while the release owner replaced this CP's broken
metrics endpoint. The image above is **not final-release acceptance**.

On corrected CP `sha256:f96d890a19fb917322f5986d1ee81b1d5911643e35b62d76a46503c5f35654f8`
(Helm 140), one bounded cohort subsequently passed all eight OpenFold2/Boltz2
named/generic/nested/HTTP cases with semantic validation and replay, plus five
mixed-model operations with server-observed outstanding concurrency exactly five.
ESMFold2 operation `1ad494d6-43b5-4fe3-b5dd-0d82c1040c64` succeeded with queue,
upload and scientific validation evidence. The advertised download tool plus
HTTPS readback verified its 695-byte manifest, 26,292-byte PDB and 597-byte
validation receipt against their published SHA-256 hashes. The first readback
attempt incorrectly used the unadvertised client-only `read_scientific_artifact_bytes`;
that collector error is preserved as a negative attempt, not a backend defect.
Only saved results were read on resume; no replacement inference was submitted.

`cohorts-final-1/partial-receipt.json` reports `partial_scope_passed`, still
`customer_ready: false`. This is one intermediate cohort, not two qualifying
customer cohorts. Both bounded cohorts must be repeated after release-owner GO
for the additive release 141 configuration identity. No cold-start, all-App,
or actual LibreChat qualification is inferred from these results.

The first fresh release-142 attempt passed all 13 protein operations but failed
its workload-overlap gate: three warm OpenFold2 calls could finish before all
five shared-session requests were admitted, producing peak four. No batch was
submitted. This is retained at `cohorts-release142/`, not called a backend error
or passing cohort. The approved bounded retry uses four existing Boltz2 fixtures
followed by one OpenFold2 fixture, preserving two models and the exact server-
timestamp concurrency-five requirement. No threshold or payload validator was
relaxed; two fresh cohorts are still required on the frozen effective release.

The ordered retry passed 13 protein operations and real overlap five. ESMFold2
`5426bdb8-b1a2-48d2-8654-76367c920dab` succeeded after normal CPU autoscaling
(expected `FailedScheduling` followed by `TriggeredScaleUp`, no manual recovery).
Its original polling client stopped on one `http_transport_failed`; the untouched
failure receipt is supplemented by `existing-batch-recovery/`, which verified the
same admitted operation's terminal semantics and three artifact hashes without
new uploads or inference. That recovery is not a clean initial transport run.
The next cohort was paused for the release owner's separate Cosmos CP fix;
this is still intermediate release-142 evidence, not a final qualifying pair.

The ordered serving operations also lack per-operation Pod/node/GPU identities
(all 13 public runtime objects are empty). Correct model revisions, scientific
outputs and independent runtime Pod/image snapshots do not fill that correlation
gap or establish zero GPU usage. The read-only usage export marks online quality
unavailable and excludes shared serving from additive totals. The release owner
has the contrasting earlier attributed and later unattributed receipts for
diagnosis. Batch provenance independently records application-observed GPU
occupancy 65s (28s active, 37s startup), with scheduler-event Pod/node correlation.

Private evidence is under
`/home/tux/secure-handoff/stockholm-live-acceptance-20260917/`, mode 0700; files
are 0600. A new same-policy `stockholm-canary-*` key expires at
2026-09-17 19:43:47 UTC. No real team key or hosted client setting was changed.

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

Discovery must account for every team grant. Live teams grant `*`, which expands
through the complete caller-scoped discovery; it is not a literal App name.
A missing explicit grant blocks the report;
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

## Executable live preparation and bounded probes

`collect_live.py` performs only explicit-context Kubernetes and in-pod read-only
policy queries. It records all fs2-system deployment images, CP Pod image IDs,
readiness/restarts, mounted configuration hashes and the exact common policy of
the 20 active Stockholm teams. The observed policy hash was
`sha256:467d5cefd976e1a6444eb858b9c71d2c25fa84a782871deec7c3c442fca6c1b9`:
wildcard models, concurrency five, ordinary inference/MCP/results/artifact
scopes, and null request/GPU/rate budgets. No admin scope is granted.

`issue_canary.py --execute` issues only a new six-hour same-policy canary after
the supplied immutable CP image is fully running. Its output contains secrets
and must stay outside the repository. It never changes or revokes existing
customer keys. The release owner owns revocation of this one disposable key
after all unfinished work has settled.

`run_live.py` without `--execute` prints the offline plan. Execution requires
that owner-only key, an exact deployed CP image, and explicit context. It
refuses real team keys, mismatched grants, unbounded/expired canaries and
rollout drift. Each cohort has eight protocol cases (two models × named,
generic, legacy nested, HTTP), then five mixed-model submissions. Replay is
checked both while admitted and after terminal success; concurrency is measured
from server accepted/completed times, not from client task counts. `--batch`
also uses the existing scientific-fleet ESMFold2 upload/queue/semantic runner
and obtains the advertised MCP download handles, then verifies the output
manifest and every artifact over HTTPS using a separate unauthenticated HTTP
client. It never forwards the gateway bearer to object storage or persists
signed URLs/headers. Required download-tool discovery is checked before inference.
`--resume` reuses persisted operation/idempotency identities; never discard an
uncertain submission receipt and resubmit with a new identity. `--stop-file`
lets the operator block new admissions while already-known operations settle.
The batch transport journal persists operation/upload/artifact IDs before polling
and records every transport failure with timing. Only the identical GET can be
retried after one second; three failed transports exhaust the entire batch
budget. POST/PUT are never retried by this wrapper. Each cohort and final receipt
retain failure counts and `clean_batch_transport`; recovered reads cannot be
presented as a clean initial run. `observe_batch.py` is a read-only fallback for
an already-known operation, using recovered immutable upload references and the
same scientific/artifact validators, never new submission or upload calls.

`finish_live.py` requires two complete bounded receipts, unchanged CP/config
identity, fixed target model/runtime identities and released batch resources.
It uses the installed SELECT-only `fs2_serve.usage_reconciliation` exporter for
the exact canary/window and checks all canary operations are terminal with zero
admission reservations. It records operation-bound Pods and GPU observers on
their nodes; a readback snapshot is not continuous telemetry coverage. Optional
explicit `--revoke-canary` revokes only the validated disposable token, retains
the durable response, checks other token metadata is unchanged, and verifies
the revoked key is refused. It never deletes operations, artifacts or history.

Example after explicit release-owner GO (replace the image/path placeholders):

```sh
components/control-plane/.venv/bin/python acceptance/stockholm-customer-20260917/run_live.py \
  --execute --key-file /secure-evidence/canary.json \
  --kubeconfig /secure-evidence/authorized.kubeconfig --context explicit-context \
  --expected-cp-image registry/control-plane@sha256:EXACT_DEPLOYED_DIGEST \
  --output /secure-evidence/new-cohorts --cohorts 2 --batch \
  --stop-file /secure-evidence/PAUSE
```

`sibling_checks.py` reads speech discovery, the caller's storage metadata
(Stockholm must remain disabled), and existing MindEval health/catalog routing.
It does not request storage credentials, provision buckets, submit speech, or
run evaluations. These checks are sibling preservation, not modality quality.
The corrected-release readback preserved all five speech catalog entries;
Stockholm own-user storage stayed disabled with no bucket or credentials; public
MindEval catalog returned 200. The optional in-pod MindEval service-health probe
timed out, so its receipt explicitly does not claim internal health success.

`inspect_librechat.py` reads the hosted endpoint identity, authenticates only
for configuration/skill reads, paginates all installed skills and hashes their
bodies. It never calls a model/tool or changes client configuration. The live
Stockholm endpoint is `aiendpoint-e00mhcnw5jpbsg95dk`, project
`project-e00z6b02t8ddk96c49`, image tag `20260909-cc85e14`, with 32 installed
skills including scientific-gateway, scientific-batch, openfold2 and boltz2.
The image tag has not been independently resolved to a runtime digest here.

The exact `cc85e14` configuration renderer uses a global
`SCIENTIFIC_MODELS_API_KEY` whenever that environment variable is supplied;
per-user custom variables exist only in the opposite branch. This endpoint
supplies the global key and its read API advertises no per-user variable.
It therefore has not been bound to the disposable canary. Replacing that
shared key is out of scope. An isolated same-build client would provide only
component integration evidence, not unchanged hosted-client acceptance.

The optional `probe_librechat.py` authenticates the existing seeded account and
would label any run as the existing shared principal, never the canary. Its
bounded attempts stopped before inference: the global key is a MysteryBox
reference (never resolved), then the known saved-protein-agent API lookups did
not return a usable agent. This does not establish that the hosted agent is
absent; its actual route remains unresolved. No model turn or tool call was
submitted. The browser login-page inspection also remained unauthenticated,
and its task-owned browser was closed. `browser_login.js` is now explicitly a
read-only page check; the earlier VM dynamic-import failure is retained in the
attempt history. No alternative client is presented as hosted-client evidence.

## Still required before full customer acceptance

The following pieces remain incomplete or unexecuted:

1. Complete release identity assembly across actual model images, source,
   configuration and client digest, tied to the release owner's build receipts.
2. Actual hosted LibreChat canary binding and conversation/tool traces. Installed
   skill hashes do not prove use; per-model skills must be mapped honestly into
   the verifier's BioNeMo integration evidence, not fabricated group labels.
3. Two complete unchanged-final-release cohorts, including concurrency five,
   representative batch and failure/cancellation behavior. One corrected-release
   bounded cohort passed, but release 141 changes configuration and requires a
   fresh pair. The new bounded
   driver is not a full all-model or LibreChat driver.
4. Approved, bounded, model-specific synthetic inputs and semantic/artifact
   validators for every advertised serving and scientific App. No fake
   all-model fixture inventory is supplied here.
5. Collectors for client traces, upstream shapes, complete attempts, lifecycle
   identity, usage reconciliation, warning/restart/resource counts, and cleanup.
6. Root-owned release evaluation, disposable-key teardown evidence and a
   human-readable customer handoff. The first live discovery advertised 21
   serving and 10 scientific Apps / 60 tools; excluding Cosmos leaves 30 Apps,
   not the two protein models selected for the first bounded run.

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

Initial offline result: **41 passed in 0.11s** (31 dedicated tests plus 10 existing
capability-gate tests). Ruff and CLI help passed. Tests use only two synthetic
App metadata fixtures and one explicit Cosmos exclusion; they never invoke a
model or establish coverage of live Apps. An initial test fixture accidentally
shared mutable release identities across cohorts; that fixture was corrected,
and the release-drift rejection remains covered.

Integration branch `agent/fs2-cosmos-stockholm-remediation-r20260917`, base
`bad3f9cba9cac2762ddbe0b62f8c6ab3a780a6d7`. The root agent owns the final commit
and frozen suite. That initial implementation made no cloud writes or live
calls. After authorized access was restored, preparation tests plus verifier
tests initially passed **54 tests in 0.60s**. After the public download-path fix,
**62 tests passed in 0.60s**, including absent-tool, metadata mismatch, expired
handle, HTTP failure, oversized content and digest mismatch negatives; Ruff
passed. The live activities above
supersede the original credential blocker. Earlier failed/incomplete client
collector receipts are retained, including pagination and redaction corrections;
use `librechat-final-readback.json` for the complete 32-skill inventory. No
historical scientific data or customer key was deleted.

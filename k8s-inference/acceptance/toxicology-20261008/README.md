# ADMET-AI and CToxPred2 — hosted qualification, 2026-10-08

Scope: pretrained, non-clinical molecular screening over the Scientific AI public
REST API and typed MCP, including bounded file/batch input and durable output.
This is not clinical validation, a LibreChat/LLM qualification, a GPU/snapshot
release, or a measurement of unrestricted multi-tenant sustainable capacity.

## Identity and access

- Origin: **https://89.169.99.188**, MCP `/mcp`.
- Operator instruction: `forge.nebius.cloud` is intentionally unavailable for this
  site. Its existing redirect, DNS and routing must remain unchanged. All site
  and backend checks here use the IP; no form intake was reactivated.
- Project `project-e00rene`; cluster `mk8scluster-e00j5z9te7x5dd9g6a`, eu-north1.
- Existing `batch-cpu` pool; 2 CPU / 2 GiB per replica, min 0 / max 2, 300-second
  cooldown. No GPU, node-pool, cloud-quota or customer-key change.
- Existing internal `system/qa` inference principal, concurrency 2. Only this key
  temporarily received both new model grants and artifact-write permission.
  Its exact previous grants, scopes, concurrency and expiry were verified
  restored at closeout; no customer key was used or changed.
- Backend image source `fb9e21e75bfe9d5b166335cf69800a5b9fc8ef2d`, based on the
  previously deployed reliability source `b12fa656dc92d7c851bc4cd69604b71576133e6b`.
- Backend image manifest `sha256:71dd5ea5a352c91aabfedd55c4baa6e83f6f31376d9d86c0b53a37cdc2c33756`.
- ADMET-AI image `sha256:2a6d0ee5e0f48d761d295eb20c91f25bfbb652ce5223bf9ba80425092dfeefdf`.
- CToxPred2 image `sha256:d7579452d45024fa159a8e25929f112fe4cf5569d6bcd17dec956655cba8aa2c`.
- Website image `sha256:0b814885a4f9e6265354ef125cc200958bc60b99e64e69a1b991c0488d95ac07`,
  source `4e5efdb19d1c018bba0501ad76db090a24d589b6`. Both models have reviewed
  **Toxicology & ADMET** metadata, without an NVIDIA publisher badge.

Full regional image references are in the generated model records and retained
private rollout receipts. Model source, license, preprocessing, input contracts,
scientific limitations and reproduction commands are in
[the runtime README](../../models/toxicology/README.md).

## Evidence and repeatable checks

- `*-runtime-r3.json`: exact-image dependency/weight inventory, upstream parity,
  1/32/1,000-molecule CPU timing, seeded neural replay, representative structures.
- `*-public-evaluation.json`: all 376 published CTox evaluation records, their
  labels, checksums and output values. Descriptive RF ROC-AUC is 0.790 hERG,
  0.782 Cav1.2, **0.626 Nav1.5**. The weak Nav1.5 result is material; these are
  upstream evaluation sets, not a new independent clinical validation set.
- `qualify_hosted.py`: public REST + typed MCP; concurrent 250-record CSV and
  1,000-record JSON artifact jobs; exact upstream output comparison, immutable
  result download verification, idempotent replay, single/SDF requests, partial
  invalid rows, wholly invalid inputs and invalid contracts. CTox additionally
  tests the supervised neural/MC-dropout method. The large 1,000-record fixture
  cycles 12 molecules to measure batching, not chemical diversity or accuracy.
- `--wait-for-cold`: requires natural min-zero convergence and absence of Pods;
  never manually scales the models to manufacture cold-start evidence.
- `observe.py`: read-only Pod, controller and replica observations.
- `collect_operator_evidence.py`: App identity, settings, Runs, Usage, request
  records, resource/log metrics and lifecycle correlation. CPU runs must not
  acquire billed GPU time. HTTP polls/uploads/replays are not extra model runs.
- `manage_release.py`: additive registration, conflict-checked rollout, admin
  preview/apply and reversible QA-only grants. Its `prepare` command is read-only.

Private raw evidence is retained at
`/home/tux/secure-handoff/fs2-toxicology-20261008/`; never commit credentials.
The first two hosted cohorts passed 13 execution cases and two invalid-contract
checks each, but inspection showed their 422 responses dropped actionable error
details. The backend r2 patch preserves recognized molecular error codes and
operator-owned guidance through the existing failure path, without forwarding
arbitrary runtime messages. A further 147 focused tests passed; final hosted
cohorts additionally assert the public error code and explanation.
See [hosted-qualification.json](hosted-qualification.json) for immutable final
release evidence and [release-closeout.json](release-closeout.json) for live
publication, healthy replica, unchanged App-spec and restored-key checks.
No broad customer-ready verdict follows from the first cohort alone.

### Final deployed result

Two completed full cohorts on the same backend digest passed: **r3 cold** and
**r5 warm**, each with 13 execution cases and two expected invalid-contract
checks. Both Apps naturally reached zero Pods and activated to two Ready
replicas. A subsequent publication-only check passed one REST and one typed MCP
request per App; these four requests are not counted as another full cohort.

| App | Cold activation, accepted to ready | Cold 1,000-row end-to-end | Warm 1,000-row end-to-end |
| --- | --- | --- | --- |
| ADMET-AI | 8.43–13.22 s | 31.12 s | 19.24 s |
| CToxPred2 | 9.49–12.69 s | 119.42 s | 111.93 s |

Activation includes queuing and scheduling on existing CPU nodes; it is not
pure weight loading or new-node provisioning. End-to-end time includes submit,
idempotent replay, polling and result download. These are individual measured
runs, not latency percentiles. The 1,000-row batching fixture cycles 12 structures.
The 250-record hosted outputs match the retained direct runtime outputs exactly
for ADMET and within approximately 1e-16 for CTox.

The intermediate **r4** cohort aborted in the test client: an invalid-input job
finished before its idempotent replay, correctly returning the same terminal
HTTP 422 operation. The harness incorrectly treated that replay as a transport
failure. It now accepts the terminal error envelope while checking identical
operation identity and expected error details. The failed r4 receipt remains
retained; it is neither excluded as a model success nor called a completed pass.

Live publication changed only qualification metadata: envelope
`fs2-toxicology-b740dccb30340079`, selections/routes
`fs2-toxicology-37e3489429db9acb`, with unchanged renderer bundle
`fs2-toxicology-bc20e3892388faf0`. The qualification receipt SHA-256 is
`311441f5e7cde626d2f28cc0a35e8a95a4022e58b4565a70798ac3ba31a5ed69`.
Runtime images and App execution specs remained unchanged. Final read-only
closeout verified 3/3 API, 2/2 controller and 2/2 website replicas healthy, and
the original QA-key policy restored. The Apps remain deployed with min 0/max 2.

Supported qualification is two concurrent internal operations and up to 1,000
molecules per operation. Independent cross-tenant saturation, LibreChat/LLM
workflows, preemption, node provisioning, GPU execution and snapshots were not
qualified. These CPU-first Apps do not advertise GPU snapshot support or
clinical/regulatory suitability.

Unit-test notes: the existing Starlette HTTP-422 constant deprecation and old
pytest temporary PostgreSQL socket-cleanup warnings predate this feature. They
do not represent unexplained model-runtime warnings or change deployment code.

## Rollout observations and limitations

The website image-only rollout preserved its nginx redirect sidecar and all
environment/network settings: 864 public-IP route checks, no failures. The backend
rollout changed only its image and three additive ConfigMap references, leaving
existing bindings, scientific executors, pools and customer jobs unchanged.
The maintenance CronJob runs unchanged generic database retention code and has no
model catalog mount; it was deliberately not restarted.

During the backend rollout, two of 1,404 route checks rejected the catalog's
non-fresh response. Website logs record an upstream 503; its existing adapter
falls back to a timestamped last-known catalog. The check requires `status=ok`
and therefore rejects `stale` as well as `unavailable`; these receipts alone do
not prove an empty catalog. Both checks recovered. The post-rollout 546 checks
all passed. No DNS, Gateway, HTTPRoute or hostname redirect was modified.

The r2 error-detail rollout recorded four non-fresh checks out of 1,344, followed
by 570 checks with no failures. The final qualification-metadata rollout recorded
one non-fresh check out of 834. All **414** final post-publication checks passed.
The strict fresh-catalog transients remain a recorded rollout limitation; they
are not presented as proof of uninterrupted freshness or an empty customer
catalog. Final verification never requested or changed the Forge hostname.

Retained implementation failures: client-side apply exceeded Kubernetes'
last-applied annotation limit during **server dry-run**, before any mutation;
the helper now uses server-side apply. The first QA grant attempted before all
API replicas learned the new Apps returned 404 and changed no key; it succeeded
after registration. The first observer encountered a newly created resource
with null status; the corrected observer treats this as unknown and retains
that initial receipt. None is hidden as a model pass.

CTox's upstream imputer warnings about 50 training-empty features are explained
and reflected in model metadata; no customer-data fitting or warning suppression
was introduced. Brief CPU scheduling contention was observed and queued work
completed; this pool has not been qualified for unlimited concurrent customers.
Per-operation CPU Pod/node identity may remain unknown behind a multi-replica
Service; App container/resource observations are available, but this is not a
claim of complete per-request CPU occupancy attribution. The retained operation
and lifecycle receipts expose these unknown fields explicitly.

## Rollback

Prior backend image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane@sha256:597732bbdc89a1aa5d9e7a5d04295090064e14d85891823fd94db87d154dc7e4`.

The private directory retains `before-deployments.json`, original ConfigMaps,
exact patches and App creation receipts. Drain these two Apps before reverting
their registrations and both backend/controller references; do not blindly undo
a Deployment revision or overwrite another agent's subsequent rollout. Existing
Apps, customer credentials, website redirect and GPU allocations are not rollback
targets. No tenant, bucket or notebook was created for qualification.

## KERMT

One verified official pretrained public backbone:
[NVIDIA NV-KERMT-70M-v2](https://huggingface.co/nvidia/NV-KERMT-70M-v2).
No verified downloadable task-trained toxicity heads were found. HF and NGC
entries are distributions of the same encoder, not several toxicity predictors.
No head was trained and no encoder was mislabeled as a toxicity prediction App.

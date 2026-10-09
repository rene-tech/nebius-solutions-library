# Verified response attribution candidate

Scope: identify the actual CXR serving Pod for future HTTP responses when more
than one replica is Ready. This is not GPU billing, a clinical qualification,
or a reconstruction of historical missing identities. No production selection
has changed in this lane.

## Retained defect evidence

The two revised CXR cohorts completed 40 operations. Eleven carried a Pod UID;
29 did not. The last attributed request completed at 2026-09-18T20:27:22.490911Z.
The second CXR Pod became Ready at 20:27:26Z. The first unattributed request
completed at 20:27:33.024507Z. Both Pods had valid GPU observer annotations.
The old provider intentionally returns `None` for multiple Ready candidates,
without using the operation ID. A ClusterIP HTTP peer is not the selected Pod.

Protected evidence root:
`/home/tux/secure-handoff/scientific-qualification-20260918/attribution-diagnosis/`.

| Receipt | SHA-256 |
| --- | --- |
| `live-cxr.json` | `6411286614667b90388b6d12a076809709d99bc5a9d8680f0691dce81c0e7c2d` |
| `retained-campaign-identity.json` | `7ed51d0aec71836c3bbcb0a008560d7bfa4723de20d18552570fba7ab063f5b7` |
| `cxr-known-lifecycle.json` | `548d4984869dd0a050414bfb345da9eb67a03c5f0d848e0e1cf3aa5c3ed96562` |
| `cxr-unknown-lifecycle.json` | `b125369c265cb0804d1f8ca69debd19035e43c2da2aaa9bee908458c610f4f1e` |

The retained campaign snapshot is per-case `operation.json`, deduplicated by
operation ID, not an authoritative campaign-final ledger export. It includes
active cohorts and original failures. For example, native-r3 lacks a top-level
Pod identity in 333/403 receipts, speech-r1 in 70/119. Scientific batch parents
must instead be examined through their per-stage lifecycle subjects; a missing
parent runtime field is not proof that those stage measurements are absent.

## Repair boundary

The pinned vLLM 0.28 image supports an importable pure ASGI class through
[`--middleware`](https://docs.vllm.ai/en/v0.28.0/cli/serve/#--middleware).
`models/general-media/fs2_runtime_identity.py` adds three response hints:
downward-API Pod UID, operation UUID, and attempt number. It overwrites any
application-supplied copies, does not buffer the body, and leaves errors,
streaming, disconnects, token budgets and generation parameters unchanged.

`RuntimeClient` verifies the request/attempt echo, then the Kubernetes provider
requires an instrumented Ready Pod with matching model label, model revision,
pinned/observed image digest and downward-API binding. Its named Service must
select that Pod, and a Service-owned EndpointSlice must contain the exact
Pod UID/name/namespace/IP on the expected port, Ready and not terminating.
Node UID and GPU UUIDs come from Kubernetes/kubelet observations, never these
headers. Missing, duplicate, mismatched, stale or unavailable evidence stays
unknown. Uninstrumented existing Apps retain their previous behavior.

Only a read-only EndpointSlice `list` permission is added to the existing model
namespace reader. No proxy, routing choice, new service identity, database
migration, admission limit or inference retry is introduced in production.

`prepare_candidate.py` stages the complete old CXR bundle plus a content-addressed
immutable middleware ConfigMap and the new template digest. The image, weights,
precision, context, GPU memory setting and resources stay unchanged. The owner
proposal preserves availability/placement settings and uses Off/Never without an
old process snapshot. Previous bundles and snapshots must remain available.
The isolated harness uses two task-only Pods/Service on existing H100 nodes,
read-only existing weights and ephemeral compile caches. It cannot join the
production Service. Actual GPU and public end-to-end acceptance remain separate
gates; unit tests do not qualify a live deployment.

## Retained isolated H100 result — 2026-09-18

Six actual responses passed across two simultaneously Ready replicas: JSON,
streaming SSE and a native HTTP error on each. Four real generations used two
distinct SHA/length-pinned public CXR inputs, without changing their structured
output schema or output budget. The JSON/SSE responses completed with `stop` and
validated against the requested schema. This checks protocol and attribution,
not diagnostic accuracy or clinical fitness.

The fresh Kubernetes observations span 21:02:33.659297–21:03:13.622891 UTC.
Both devices were NVIDIA H100 80GB HBM3, driver 580.159.04, on existing
preemptible nodes. Each response resolved to its actual distinct Pod and node,
with the GPU UUID independently supplied by the existing node observer:

| Replica Pod UID | Observer-reported GPU UUID |
| --- | --- |
| `916aad40-b880-4c35-922d-730a8f66fea2` | `GPU-b9790cfd-34f8-8c04-faea-56b5cedc79d7` |
| `7e748d60-ff7b-4436-8ada-829c1b26a958` | `GPU-1462b7c3-181d-f4ac-fa60-d9122abbc5d8` |

Exact unchanged runtime image:
`sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`.
Middleware SHA-256:
`c0cf52479aff5ab5efe334fc154640fd92b39e0aa348ab7705f8966e2dd4ca9c`.
New template `nv-reason-cxr-3b.response-identity-20260918`:
`sha256:e84014a61b5cf3ac5eb3247ffb31b1001cd6284ffe092354f08448e4bb31a595`.
Runtime source is commit `6e3a832ee`; the final direct-probe harness is
`e41fe5a8f`. Production CXR selection was not changed.

Protected receipts under the evidence root above:

| Receipt | SHA-256 |
| --- | --- |
| `h100-response-proof-r2/summary.json` | `d6b235efda90ce5519ee40ecb11e16b010bb959c221d3699fe33ce3c9af0458d` |
| `owner-render-preflight.json` | `8a1ba08de236382a0eb4b8b3622f4c98836d8604434991664ba863c33a12d823` |
| `cleanup-response-proof.json` | `43f80f27af5616ae4f20d1c5f17061786c071a3cdcf45cc2f5f14848f7232ee3` |

The owner-render preflight used the retained mounted contracts, validated actual
gateway bootstrap, and rendered hot/burst Deployments with middleware present
and old snapshot restoration absent. It is not a fresh production promotion.
The six HTTP receipts and all verification reads are retained privately.
Both task Pods, their Service and two task ConfigMaps were normally deleted;
both port-forwards closed. The shared weights PVC was not modified or removed.

Retained qualification corrections: an initial direct probe mistakenly passed a
platform artifact-reference object to raw vLLM and got HTTP 400; the HTTP body
was not retained in that failed harness run. The corrected probe resolves the
manifest-pinned bytes into data URIs and saves raw replies before assertions.
This negative is not a model failure or an acceptance pass. Initial task Pod
model labels were corrected before generation to a task-only ID, so candidate
replicas cannot enter the production metadata candidate set or Service.

Local checks: 205 affected CP regressions plus ten candidate/promotion/Helm
tests passed. Ruff passed. A targeted mypy run retained five pre-existing
usage-parser typing errors; it is not reported as a passing full typecheck.

## Accounting interpretation remains limited

The known sample's lifecycle API reports 0.609478 seconds as
`application_observed` active/scheduler/device time: this is an attributed
invocation interval, not DCGM-measured GPU busy time. A warm shared Pod's complete
resident idle interval outside requests is not allocated to the request. The
unknown sample reports quality `unavailable` with numeric zero fields and
`reconciled=true`; those zeroes do not mean zero work or complete measurement.

Shared online request intervals can overlap and remain excluded from additive
customer GPU totals. Admission reservation/attempt counters are not measured
occupancy. This repair improves per-response identity; complete physical
occupancy/idle accounting still needs non-overlapping Pod/device residency
intervals plus sampled device activity with an explicit unknown phase. Neither
an exact endpoint nor a successful response supplies those measurements.

## Local and isolated checks

Run the dedicated CP response-attribution suite together with existing lifecycle,
runtime/debug, scientific-error, speech and accounting regressions. The candidate
tests exercise immutable template preservation, old snapshot exclusion, isolated
selectors/resources, and rendered read-only EndpointSlice RBAC.

`qualify.py` takes two explicit Pod port-forward origins and two distinct retained
CXR JSON-schema requests. It requires both real candidate Pods Ready, invokes real
JSON/error responses through `RuntimeClient`, checks a streamed response through
the same verifier, retains full HTTP exchanges and fresh Kubernetes observations,
and requires both actual Pod UIDs. Its opaque operation IDs are local probes,
not public durable admissions. Keep its output private.

Promotion: after exact candidate qualification, root merges the new CXR template
into the current complete envelope/bundles and qualification projection using
the existing four-map promotion/bootstrap checks. Root owns CP image/RBAC rollout,
drain/owner-API template selection and public two-replica acceptance. Do not
publish or overwrite old templates from this helper.

`prepare_promotion.py` reuses those existing validators, accepts only the exact
two-replica-qualified template, and requires one freshly mounted envelope and
bundle, the current routes/admin baseline, and all ModelDeployments. It retains
all old templates/snapshots and preserves the other 19 model records, voices,
replica/resource/context limits and operator settings. The admin artifact
identity is unchanged, but its real bootstrap is still validated. A CXR public,
semantic, cold or elastic qualification is not inherited from the old process.

Run with the CP virtualenv and `PYTHONPATH=components/control-plane/src:catalog/runtime`,
using `--live-configmaps`, `--live-routes`, `--live-admin-configuration`,
`--modeldeployments`, `--source-commit` and a new private `--output` directory.
Outputs include immutable four-map documents, a minimal Helm values delta,
owner-API proposals, original rollback proposals and validation. No apply is
performed. Re-capture/rebase after another lane changes any map; root alone
serializes publishing, Helm and drain/template/resume. Public durable
two-replica request attribution remains the post-promotion gate.

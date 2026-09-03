# fs2-serve model catalog

This directory is the versioned source of truth for model identity, runtime,
staging, and startup-policy data consumed by the fs2-serve gateway and model
controllers. One JSON file under `models/` represents one exact model lane or
one explicitly blocked candidate.

The catalog is fail-closed:

- conventional launch is the fallback for every tested lane;
- no local entry is exposed before B300 qualification, and no federated entry
  is exposed before its exact alternative-backend qualification;
- a historical H100/H200/B300 observation is evidence, not platform support;
- the compatibility audit records OpenFold2, OpenFold3, Boltz2, GenMol, and
  MSA-PDB70 as historically B300-compatible, while the exact Evo2-40B,
  RFdiffusion, DiffDock, MolMIM, and ProteinMPNN executions are permanently
  blocked on SM103 unless a separately qualified alternative backend is bound;
- snapshot experiments require an exact single-GPU workload allocation on a
  separately bound B300 node placement, plus driver, CUDA, runtime image,
  model artifact, allocated GPU UUID, full node-inventory digest, and cache
  capability tuple;
- multi-GPU CRIU is always recorded as unproven and disabled;
- an unresolved license, entitlement, immutable identity, artifact acquisition,
  target-node NGC canary, or runtime prerequisite blocks routing.

Cosmos3-Nano is pinned to Hugging Face revision
`7a312c868bcce8e40b3eb40861300a9d0ba3fde1` and to the official vLLM-Omni OCI
digest in its model record. Its 68-file, 34,986,890,561-byte inventory is bound
by a canonical per-file manifest. NVIDIA/vLLM upstream material records H100 and
B300 execution, but those are vendor historical inputs rather than retained
FS2 performance evidence. The base record is therefore discoverable and
qualified for bootstrap while remaining unexposed and MCP-non-invocable; the
live inventory and normal qualification receipts remain the only route
authority.

Qwen3-8B at exact revision
`b968826d9c46dd6066d109eabc6255188de91218` has reviewed Apache-2.0, public,
ungated Hugging Face evidence and is the preferred first conventional B300
qualification candidate. Its exact-revision raw `LICENSE` bytes are bound at
SHA-256 `832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e`.
That review removes license ambiguity only: the base
record remains unqualified and unexposed until live artifact, immutable
runtime, exact B300, gateway semantic, readiness, and serving-binding evidence
passes the normal route intersection.

GPU `count` and `topology` are workload-allocation fields, never an inference
about the physical node. `resources.gpu.placement` independently binds the
cluster-owned node-pool labels, physical GPU count/preset, exact GPU taint
toleration, cache capabilities, and ordered storage qualification. Qwen
requests one GPU but its first target is the preemptible eight-B300 burst pool
(`gpu=true`, `type=preemptible`, `gpu-count=8`, `preset=b300-8x`,
`pool=burst`). Its first storage cohort is a 64-GiB, ext4, RWO,
`WaitForFirstConsumer` provider block PVC using the cluster-owned
`fs2-network-ssd-retain` StorageClass (`compute.csi.nebius.com`,
parameters `type=NETWORK_SSD` and
`csi.storage.k8s.io/fstype=ext4`, expandable, `Retain`). The current
cluster/integration source does not create this StorageClass, so this candidate
remains fail-closed. Qualification requires a signed API-server observation
with the StorageClass UID and resourceVersion plus the exact provisioner,
reclaim policy, binding mode, expansion flag, and parameter map. A caller's
flat semantic summary, the provider default `Delete` class, or any parameter
drift is not evidence. The PVC renderer requires the reopened opaque admission
and cannot render from the StorageClass name alone. After that admitted claim
exists, a second signed receipt must bind its API-observed UID/resourceVersion,
the exact acquisition operation and Job/ServiceAccount, the writer-admission
controller and ValidatingAdmissionPolicy UIDs, and the CAS-held writer Lease.
RWO is not a writer fence: the API policy must reject a second same-node writer,
and the signed handoff must observe zero writer UIDs plus a released Lease before
runtime admission. A zero-GPU acquisition Job is the first consumer and sole
writer; after exact-manifest publication it exits, and the
one-GPU Deployment mounts the same claim read-only. Route qualification also
requires controlled detach/reattach to a distinct replacement node, no
Multi-Attach, two gateway semantic responses, and scale from one to zero while
the claim remains Bound. The provider default `Delete` class is canary-only
unless deletion is independently protected. SFS and node-local NVMe are later,
separate disabled cohorts. Node-local remains explicitly
`gated-unimplemented` until a reviewed local-PV/PVC controller proves
`WaitForFirstConsumer`, exact PV node affinity, preemption and lost-node
fencing, and PVC recreation for each activation generation. The one-B300 pool
is never a Qwen provider-block or node-local cache target.
Model and localizer Pods consume PVCs by default and never pin a Pod with
`nodeName`. The scientific renderer has one deployment-owned exception: the
AlphaFold3 data stage may use the canonical operator reference volume root
`/mnt/fs2-reference-data/data`, but never exposes that root directly. A
promoted aggregate-tree identity supplies the exact safe
`datasets/alphafold3-public-databases-v3.0/v3.0-paper-snapshot-2022-09-28/sha256/<tree_sha256>`
subPath mounted read-only at `/databases`, its independent manifest digest, and
attested node-access selector. The trusted target and renderer both require
`storage.fs2.nebius/reference-data=true` at apply time, conjunct it with Kueue's
normal ResourceFlavor accelerator placement without node or pool IDs, and retain
the dedicated inference toleration plus supplemental group `1000`; AF3 parameter inference uses the
private RWX claim with supplemental group `65532`. The trusted
`scientific-execution-targets/v1`
contract fixes that model, stage, namespace, root and subPath policy. It also
binds the controller caller to ServiceAccount
`fs2-system/fs2-serve-control-plane-runtime`, matching the companion academic
execution RoleBinding. Public requests cannot select a controller identity,
namespace, PVC or `hostPath`.

The target contract is a production compiler input, not a second runtime map.
`fs2-serve-render-scientific-execution-map` combines it with the promoted
canonical profile set and an exact
`scientific-runtime-localizations/v1` deployment-evidence document. The tool
expands packaged adapter stage contracts, verifies the complete small-file or
aggregate-tree closure through `FileScientificManifestRenderer`, and emits both
`execution-map.json` v3 and an immutable content-addressed ConfigMap document.
The workloads Terraform stage installs that exact ConfigMap and passes its name
to Helm. Generation fails when a profile is not runnable, an image is not
digest-pinned, a localization differs, or the AF3 tree content, independent
manifest, pinned subPath, placement receipt, namespace or group is absent.

```bash
uv run --project components/control-plane \
  fs2-serve-render-scientific-execution-map \
  --catalog-root /path/to/promoted/catalog/runtime \
  --targets catalog/runtime/contracts/scientific-execution-targets.json \
  --localizations /path/to/scientific-runtime-localizations.json \
  --output /private/run/execution-map.json \
  --config-map-output /private/run/execution-map-configmap.json
```

Set the absolute generated ConfigMap path as
`deployment.scientific_batch.execution_map_file`. Checked-in profiles remain
candidate/unqualified, so the command intentionally refuses to manufacture a
deployable map until exact image and AF3 database promotion evidence exists.

The same trusted target contract binds writable compiler-cache PVCs only for
AlphaFold3 inference (`/cache/alphafold3` in `fs2-academic-poc`), OpenFold3
inference (`/cache/openfold3` in `fs2-models`), and Protenix sampling
(`/cache/protenix` in `fs2-models`). Each cache is same-namespace,
operator-owned, has no runtime artifact identity, and uses exact image-contract
environment paths. These bindings are auxiliary L1+ compiler/JIT optimizations,
not L2. Their numbered ceiling is `L1`, their current qualified level is `Off`,
and routing remains unqualified until first/warm H100 compile measurements. L2
requires an actual GPU/process snapshot on shared filesystem or object storage;
L3 requires a local-disk snapshot, and L4 requires host-RAM residency.

The acquisition Job runs as deterministic UID/GID/fsGroup `10001`, with
strict supplemental groups, `RuntimeDefault`, and no root fallback. A provider
block acquisition receipt is v4 and is accepted only after the mounted
filesystem reports `ext4` and an exclusive mode-0600 marker is written,
fsynced, read back, removed, and its directory fsynced. The observed marker
ownership is part of the signed subject.

The Job renderer never accepts a caller-selected acquisition image. The v5
plan fixes the helper repository suffix, package and lock digests, entrypoint,
platform, and security identity; an opaque signed image admission adds the
exact OCI digest, registry identity, signature, SLSA provenance, SPDX SBOM,
reviewed Git commit/tree, builder/build type, digest-pinned container
materials, and wheel digest. The Job remains suspended until a
controller patches its API-server-observed Job UID into the Pod template; the
Pod UID is sourced from the downward API. The in-Pod worker result is not
routing evidence. Only a post-termination v4 receipt that joins the helper,
Job UID, Pod UID/owner, ext4 proof, UID-precondition deletions, absent final
state, distinct untouched replacement UIDs, and zero foreign-UID mutations can
be used by the route validator. No such live receipt exists, so routes remain
zero.

Qwen, GLM, and CXR runtime argv consume the exact mounted content path, not a
remote Hugging Face model ID. Their canonical command contains one
`{FS2_MODEL_CONTENT_PATH}` token and `--served-model-name <model.id>`; the
workload adapter replaces only that token and independently refuses to render
unless the first served name remains the stable catalog ID. Thus public Qwen
requests use `qwen3-8b`, never the source repository as a replacement name.
vLLM can carry a source-name compatibility alias after the stable name in a
separately reviewed deployment overlay, but that alias is optional and is not
the public catalog identity. Repository IDs and revisions remain immutable
acquisition provenance and cannot occupy the model-content argument or a
revision flag, preventing a silent redownload. Their Pods set the
Hugging Face, Transformers, and Datasets offline flags and are selected by an
API-observed deny-all-egress NetworkPolicy before process start. Promotion
also requires a signed startup receipt proving DNS, HTTPS, and model-registry
access were blocked through readiness.

Native Deployment and NIMService renderers bootstrap at zero replicas. The
`fs2-model-activation-controller` exclusively owns `/spec/replicas`; workload
annotations bind the replica-ownership contract and require GitOps
`RespectIgnoreDifferences` for that pointer after bootstrap. API replicas may
not patch it, and Helm/GitOps may not force-conflict with the activation field
manager.
`fs2-serve.nebius.ai/node-scaler-owner` is distinct from the replica field
owner; rendered desired state remains zero and activation alone owns the
server-observed managedFields entry for `/spec/replicas`.

Cache ownership follows the binding architecture: NVIDIA NIM records belong
only to the NIM Operator `NIMCache` controller, non-NIM records belong only to
the fs2 localizer, and blocked candidates have no cache controller. The loader
rejects a second writer for the same model-scoped path.

The public base-loader contract is `fs2_serve_catalog.loader.load_catalog`. It
rejects duplicate keys and symlinks, checks the catalog index and every model
invariant, verifies semantic-validator and exact semantic-request/asset
contract digests, and binds every historical
commit/tree/path to the digest-pinned packaged provenance lock. A full Git
object database is additionally checked when present but is not required for a
detached production or `git archive` installation. A base catalog is
deliberately never routing authority by itself.

Gateway consumers use the stable typed entry point
`fs2_serve_catalog.consumer.load_gateway_catalog`. It intersects this one
canonical base schema with a separate `model_id`-keyed serving-binding overlay.
The overlay carries only live Service, policy/MCP binding, and qualification
receipts; it cannot replace a model revision, image, artifact, interface, or
policy operation. Serving-bindings v16 selects one explicit storage mode:
Qwen provider-block uses the signed acquisition receipt as its immutable
content subject plus a distinct signed PVC lifecycle proof; SFS conventional
reuses the signed acquisition receipt as its placement proof,
local NVMe is rejected until a later reviewed binding contract can reopen the
local-PV/PVC lifecycle receipt and activation generation, and NIM uses a signed
NIMCache/PVC receipt. The three modes cannot substitute for or be pooled with
one another. `contracts/scale-contracts.json` separately binds each model,
executable, and resource-placement identity to an exact namespaced Deployment, NIMService, or
attested batch Job selector; fixed zero-to-one bounds; scaler ownership;
readiness/warmup; cooldown/preemption; and UID-fenced cleanup. Ordinary gateway
API replicas have no Kubernetes mutation authority. An enabled local route
must instead bind a distinct least-privilege activation-controller Deployment,
projected ServiceAccount, named leader Lease, exact leader/target Roles, and
durable PostgreSQL intent interface. There is no activation Service or
endpoint. The live binding and signed lifecycle subjects join controller and
submitter Deployment/Pod/ServiceAccount UIDs, two value-suppressed
server-observed pre-created PostgreSQL DSN Secret UID/resourceVersion/key sets,
`fs2_activation_submitter` and `fs2_activation_claim_owner` roles and exact
grant-set digest, packaged canonical DDL digest, leader Lease
UID/resourceVersion/holder/renewal, durable intent/idempotency subject,
previous/current monotonic per-model fencing tokens, DB-clock claim expiry,
and target-state CAS. Signed zero-to-ready and
return-to-zero receipts bind distinct fenced intents, the same immutable target
and runtime, activation-only replica managedFields with GitOps excluded, and
the expected cleanup UID set. The stable
`activation_intent_binding_digest` typed helper covers immutable route inputs
while normalizing derived expiry and receipt digests out of the intent subject,
so receipts reopen independently without a hash cycle. Artifact acquisition
and prerequisite resources are explicit bound contracts. Every audited
114--132-day-old legacy NGC Secret copy is
NO-GO; it may not be read, copied, or used as bootstrap. Phase-7c HMAC material
and the exposed Evo bearer are separate forbidden credentials, not NGC
fallbacks. NGC promotion requires a newly issued platform-owned key securely
pre-created out of band in the exact existing `fs2-models/fs2-ngc-pull` and
`fs2-models/fs2-ngc-runtime` Secrets. A signed, value-suppressed API-server
observation must bind each exact UID, resourceVersion, type, and complete key
set; Helm, Git, logs, and receipts contain names/keys only and never values.
ESO 2.5.0/MysteryBox remains an optional disabled backend because the current
foundation CRD is ineligible and installs no ESO. It cannot be selected until
a separately reviewed eligible provider/build receipt and a future contract
update bind the exact controller image, CRDs, provider build, tests, reviewer,
and validity. It then requires an exact-digest pull/runtime canary on the target
B300 node, NIMCache authentication with that same credential generation, and
two distinct semantic gateway requests. NGC access from the development runner
is WAF-blocked. All NIM base records remain unqualified/unexposed, and no NIM
route can load without this joined evidence chain. Enabled routes require fresh
Ed25519 attestations verified against caller-supplied trusted public keys.
`EvidenceStore` keeps a pinned root directory descriptor and performs
component-by-component no-follow `openat` custody. Subject bytes are parsed
from one bounded regular-file descriptor; post-read leaf and intermediate
inode checks reject symlink and atomic-replacement races. The pinned root,
every opened directory, and every leaf must retain the validator's exact
UID/GID and be non-group/world-writable. Regular leaves have exactly one link;
POSIX directory link counts (inherently at least two for `.`/`..`) are pinned
and rechecked. Replacing the root pathname cannot redirect an in-progress
validation away from its pinned root. Receipts, attestations, manifests, and
raw JSON objects all use the same strict bounded parser before schema logic:
64 MiB bytes, depth 64, 100,000 nodes, 10,000 collection members, 1 MiB strings,
signed-64-bit integers, finite bounded floats, unique keys, and no
`NaN`/`Infinity`. Parse, recursion, numeric, and memory failures normalize to
`CatalogError`; a subject is never reopened to parse it.
Every signed subject is joined to the reopened artifact, localizer or NIMCache
placement, worker/runtime, semantic, cohort, cleanup, and readiness payload in
one non-replayable evidence session. Gateway semantic evidence also binds the
gateway identity, scoped-API-key auth class, route contract, backend Service
UID, private origin, endpoint identity, and trust bundle. The receipt and its
separately signed validator result must also carry the packaged request-contract
and licensed-asset-set digests, exact ordered request IDs/payload hashes,
gateway and backend Service UIDs, operation, gateway-proxy transport, and
readiness identity. A fully re-attested unrelated request or Pod-local shortcut
therefore remains invalid. Internal Service origins and activation
controller/target identity never appear in the public gateway projection. The
catalog-bound `contracts/federated-backends.json` preserves exact SM90
observations without making them routes. MolMIM's exact us-central1 H200
KServe/NIM subject is preferred but gated; Evo2's observed H200 Serverless
subject is blocked by an exposed credential and an unpinned backend image; the
exact DiffDock, RFdiffusion, and ProteinMPNN subjects are historical H100
bridges only. Newer professional-service digests are different identities and
cannot be aliased. A federated route requires the inventory itself to be
updated through review with exact endpoint/trust hashes, a new scoped
value-suppressed Secret receipt, a digest-pinned runtime, a signed
content-addressed artifact-on-backend subject, fresh readiness, and two
distinct semantic responses. Serving-bindings v16 implements that distinct
alternative-backend evidence path and forbids mixing it with local B300
receipts. The current records remain zero-route because none of those reviewed
live receipt chains exists and the separate activation controller is not
deployed by this lane. Each enabled binding exposes a `valid_until` equal to
the earlier of the earliest verified attestation expiry and the live
activation-controller Lease expiry; gateway consumers must call
`ServingBinding.valid_at()` per request or reload/remove the route before that
instant. The attestation verifier requires the locked
`cryptography==41.0.7` runtime package; deployment images must pin and scan the
exact wheel they carry. Build or install it with `uv sync --locked` and
`uv build --project k8s-inference/catalog/runtime`. The wheel includes the typed
API, Kubernetes adapters, the reusable validators, digest-identical packaged
copies of every external BioNeMo validator/fixture, and detached static catalog
data resolved through `installed_catalog_root()`. The exact cross-lane
fixture is
`contracts/gateway-consumer.fixture.json`, and the immutable identity regression
map is `contracts/golden-identities.json`. Only
`GatewayCatalog.routable_model_ids()` is routing-capable.

`contracts/model-variants.json` is a separate, additive source-candidate index.
It does not replace the canonical model record, serving binding, or scale
contract. `Catalog.model_variant()` and `Catalog.variants_for()` expose typed
read-only discovery; `Catalog.routable_variant_ids()` is deliberately empty.
Each independent candidate comes as a `portable` and `blackwell-sm103` pair and
binds an immutable upstream/Hugging Face revision, expected artifact identities,
license state, exact-versus-capability-equivalent relationship, and required
supply/qualification receipt schemas. Expected file hashes are acquisition
inputs, not a completed manifest or staging receipt.

An exact-model variant preserves its base model ID but still records NIM
artifact parity separately. A capability-equivalent variant must use a distinct
customer-facing model ID and receive its own canonical base record before any
promotion. In particular, exact MolMIM remains only the pinned NGC NIM identity.
`NV-GenMol-89M-v2` is the exact upstream identity for the canonical `genmol`
lane, not MolMIM; a secondary capability relation is recorded as explicitly
non-aliasing. The v4 discovery index contains source-only portable/SM103 pairs
for DiffDock, Evo2-40B, GenMol, ProteinMPNN, RFdiffusion, and Segment-CT. It also
reconciles all eleven fallback candidate IDs: unmapped Boltz2/OpenFold2 remain
deferred, OpenFold3 remains access-blocked, MSA-PDB70 remains license-blocked,
and exact MolMIM remains NGC-provenance-blocked. None has an immutable platform image, complete
artifact/license receipt, B300 qualification, independent promotion review, or
route.

Future v5 supply receipts must bind the variant digest, exact repository and
revision URL, canonical full per-file path/size/SHA manifest, revision-bound
license bytes, immutable image and complete build materials. Signature,
provenance, SPDX SBOM and scan are distinct custodied raw content-addressed
objects. The signature object contains a verified Cosign-style DSSE envelope;
SLSA v1, SPDX 2.3 and scan objects are parsed and joined to the exact OCI
repository/digest, source/build and freshness under separate canonical raw-key
role principals. Embedded booleans or hash-shaped summaries are not supply
evidence. Future v5 qualification receipts must
additionally bind the exact B300 worker/GPU/runtime tuple, compute capability
10.3, repeated native dispatch and determinism, network-denied mounted startup,
two gateway semantic responses per successful attempt, exact quality comparator
and canonical vendor-NIM baseline evidence, separate cold n>=3 and warm n>=10
cohorts with every failure retained, preemption recovery, and
zero-to-ready-to-zero lifecycle. The static source index cannot consume those
receipts or label itself qualified: digest-shaped promotion fields are rejected.

`model-variant-promotions/v4` is the distinct live authority. Its typed loader
reads the ordered artifact manifest through one custodied descriptor; Ed25519-signed v5 supply plus exact raw
DSSE/SLSA/SPDX/scan bytes; v1 runtime; v2 semantic; v3 failure-complete cohort;
per-cold-attempt v1 zero/Pod-absence/new-process/cache boundaries; v2 backend
readiness joined through raw Service, EndpointSlice, Pod, Node, PodResources
and gateway probe API observations; v1 Event-backed preemption with old-fence
and distinct replacement Pod/Node/GPU; two v1 lifecycle subjects; v5
qualification; and v4 independent review under one fresh session. Every exact
evidence role has one enabled canonical raw Ed25519 principal and a distinct
policy group. Attempts are globally unique, chronologically ordered,
non-overlapping on one Node/GPU, and cold completes before warm starts;
attestations are issued after observations and expiry is strict. Failure rates
are recomputed, bounded to `[0,1]`, and limited by the promotion threshold. A
route exists only after those subjects intersect the exact canonical model, an
enabled, ready, fresh normal serving binding with the same runtime image and
API-observed Service/EndpointSlice/Pod/Node/GPU chain, and the immutable scale
contract. OCI repository substitution, license/revision drift, raw-object or
path/hash tampering, cross-paired or unrelated preemption, synthetic readiness,
non-cold boundaries, unopened lifecycle subjects, shared-role keys,
untrusted/stale/replayed evidence, SM100/kernel relabeling, n<3 cold or n<10
warm evidence, Pod-local semantics, and candidate/profile/base/binding bypass all
fail closed. No live overlay or enabled variant is committed here.
The cross-lane consumer handoff is the packaged
`contracts/model-variant-consumer.fixture.json`; it names the public typed
loader and every required live receipt schema without embedding any evidence.

Run the focused offline checks with:

```bash
uvx --from ruff==0.12.11 ruff check \
  k8s-inference/catalog/runtime/fs2_serve_catalog \
  k8s-inference/catalog/runtime/tests \
  k8s-inference/catalog/runtime/validators
bash k8s-inference/catalog/runtime/run_checks.sh
```

Live staging remains a separate overlay, but its artifacts and receipts are
reopened and verified by the gateway consumer before a route can exist. See
[`MODEL-ONBOARDING.md`](../../../docs/fs2-serve/MODEL-ONBOARDING.md) for the
exact record, staging, Kubernetes, qualification, cleanup, and promotion flow.

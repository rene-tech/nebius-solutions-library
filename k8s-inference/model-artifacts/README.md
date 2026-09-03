# Public cancer-model artifact cache

This directory is the checksum-first acquisition and readiness boundary for
public cancer-immunotherapy model bytes. The committed catalog currently pins
86 acquisition entries in 23 independently named artifacts. Two pairs reuse
the same source archive for deliberately different runtime trees, so this is
84 unique source objects (83,419,711,292 bytes / 77.69 GiB), not 89,057,956,007
bytes of distinct storage. No model byte, credential, signed URL, or private
license acceptance is stored in Git.

## Contract and safety properties

- `artifact-catalog.json` records the upstream URL, exact revision, filename,
  byte size, SHA-256, license instrument, consumers, and offline check for every
  object. `manifest-*.json` uses the shared `artifact-manifest/v1` contract.
- `generate_catalog.py --check` proves generated manifests have not drifted
  from the reviewed source locks. Each content digest is SHA-256 over the
  canonical sorted file inventory, and each manifest has its own canonical
  SHA-256.
- `public_artifacts.py stage` accepts credential-free HTTPS only, resumes into
  a deterministic `.part` path, verifies size and SHA-256 before a
  same-filesystem atomic rename, rejects symlinks and undeclared files, and
  makes the published tree read-only.
- A per-manifest `flock` prevents concurrent writers. Retries reuse verified
  files, while checksum failure leaves no published destination or receipt.
- A non-secret immutable receipt binds the catalog, manifest, content, source,
  license, storage project/region/cluster/filesystem and size, dedicated
  namespace/queue/CPU pool, exact read-only `/models` and `/databases` consumer
  bindings, both source commits, and cluster-side offline checks. `readiness`
  fails closed on a missing or invalid receipt, projects the immutable runtime,
  private-delivery, and reference-data constraints for each consumer, and never
  treats a fallback as its requested successor.
- Kubernetes ingestion Jobs are CPU-only, non-root, capability-free,
  read-only-root-filesystem workloads using a digest-pinned Python image and no
  service-account token. They are suspended for Kueue admission, opt into only
  the reference plane's public-source egress policy, tolerate only its dedicated
  taint, and mount its existing node path. The renderer requires the isolated
  `fs2-reference-data` namespace and storage-attached regular CPU pool, and
  rejects the shared system pool or filesystems smaller than 2 TiB.

`model-artifacts/terraform` is intentionally resource-free. It validates the
non-secret handoff from the integrated reference-data Terraform plan and emits
renderer inputs; it cannot create a PVC, namespace, filesystem, or Job. The
reference-data plan must own the 2 TiB regional filesystem, dedicated
namespace, public staging policy, Kueue LocalQueue, and dedicated regular CPU
preprocessing pool. Never create or resize those resources from this directory.

## Coverage and truthful blockers

The available set includes all three Proteina-Complexa variants and their
AlphaFold2, RoseTTAFold3, ESM2, ProteinMPNN, and LigandMPNN dependencies; every
BoltzGen checkpoint including `boltzgen1_structuretrained_small.ckpt`; the
runtime-selected Boltz2 and mosaic components; the complete upstream
RFdiffusion and ProteinMPNN sets; ESMFold2 and ESMFold2-Fast trunks plus the
ESMC-6B config, tokenizer, index, and all six shards; the ESMFold2 CCD as one
standalone artifact consumed by both full and Fast runtimes; the single
OpenFold3 OpenBind-0 checkpoint plus the separate upstream OpenFold3
`components.bcif`; the exact public BoltzGen `mols.zip`; and the independently
verified Protenix v2 third-party mirror. No Protenix v1 substitute is published
or exposed. Both ESMFold2 runtimes bind
the shared ESMC snapshot at `/models/esmc-6b` and the separately receipted CCD
artifact at `/databases/esmfold2/ccd.pkl`; their own model weights remain distinct at
`/models/esmfold2` and `/models/esmfold2-fast`. ESMC and CCD stay external to
the runtime image, immutable, read-only, and independently receipt-gated.

Archive identity and runtime-tree identity remain separate for primary-model
consumers. `alphafold2-params` stages the exact 5,587,968,000-byte Google
archive, then the primary artifact localizer must produce the 16-entry flat
Proteina tree at `/opt/fs2/artifacts/alphafold2-params` with inventory SHA-256
`cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4`.
`alphafold2-params-bindcraft` intentionally reuses those source bytes but is a
different 17-entry runtime tree at `/models/alphafold2`, including BindCraft's
generated admission manifest, with inventory SHA-256
`9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f`.
Neither tree can be represented by the tar digest.

BindCraft's ColabDesign MPNN trees are also distinct artifacts. Both come from
`sokrypton/ColabDesign@e31a56fe1d9b4de25c8697f3a28b75892941cc72`
(archive SHA-256
`26c948e5e577c65d5b3e908cc11eece435eb0f05729b1e227926d671c463d37f`),
but `colabdesign-mpnn-weights-vanilla` and
`colabdesign-mpnn-weights-soluble` select different source subtrees, have
different inventory digests, and mount at their exact installed-package paths.
Readiness for all four primary localized artifacts and the BoltzGen molecule
tree requires the canonical scientific-localization receipt in addition to the
source-cache receipt; an archive-only cache entry cannot make a runtime ready.

Licensed PyRosetta bytes remain entirely in the academic-assets plane. The
runtime-integration projection exposes only the non-secret installed-tree
identity required by BindCraft: artifact
`bindcraft-pyrosetta-installed-tree`, tree-manifest SHA-256
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`,
3,287,122,494 installed bytes, and the read-only `PYTHONPATH`
`/opt/fs2/academic/pyrosetta-bindcraft/site-packages`. The separate wheel
artifact identity remains provenance only and neither the wheel nor installed
files are copied into this public cache. Operational academic-PoC authorization
and the offline PyRosetta import/pose-score are recorded; formal institutional
acceptance remains a non-PoC advisory. The final immutable runtime localization
binding remains deliberately absent until a corrected producer receipt is
independently accepted.

The current ESMFold2 and ESMFold2-Fast image contract is a binary-compatible
Hopper candidate, not a qualified accelerator contract. Exact ESM
source revision `827ec128e4cdaf80f7d6f95fb367a08980b34918` freezes flash-attn
2.7.4.post1 from the `py312-pt211-cu13-sm80-90` release. Manager-provided binary
inspection found only `sm80` and `sm90` cubins and no PTX in that wheel. The
catalog therefore records Hopper/`sm90` as a binary-compatible candidate while
qualification remains pending an exact-image H100 semantic test. Blackwell
fails closed until either a separately qualified SDPA fallback image or a
target-aware Blackwell image exists. The mere presence of an upstream SDPA code
path is not runtime qualification.

OpenFold3's pinned source revision
`c4771653c5d0a3ebb0b3af71b05efd64bc44ee86` downloads the CCD object from
`https://openfold3-data.s3.us-west-2.amazonaws.com/components.bcif`. The
catalog binds the exact 63,393,643-byte object (S3 ETag
`b251a30629b9c30d077a5b91aeefecb2-4`, SHA-256
`473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c`) at
`/databases/openfold3/components.bcif` and its production checkpoint at
`/models/openfold3/of3-ob-2025-06-30-174k.pt`. Offline verification requires
the exact single-file artifact, a BinaryCIF envelope, the `components` block,
the `_chem_comp`, `_chem_comp_atom`, and `_chem_comp_bond` categories, and the
recorded Biotite 0.3.0 encoder metadata.

AlphaFold3's public database bundle remains owned by the reference-data plane,
not duplicated by this model-artifact ingester. Its cross-plane layout is
provenance-bound to `alphafold3-public-databases-v3.0` and the exact upstream
source revision, mounted read-only at `/databases`, and passed to the runtime as
`--db_dir=/databases`. The separately licensed private parameter object remains
outside this general cache and every image, delivered only by the tenant-private
academic-assets plane. Its canonical runtime mount is `/models`, its exact file
is `/models/af3.bin.zst`, and the runtime argument is `--model_dir=/models`.
The cross-plane identity is Google generation `1780568696389861`, exactly
1,020,545,840 bytes with SHA-256
`74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff`.
This metadata does not make the private artifact public or ready.
The accepted r6 runtime image is independently H100-qualified at digest
`sha256:0cde199e8473a2d069c896c4f8d67a58b31e00bfb87c3660aed154693699e03e`;
that image contains no licensed parameter bytes, and its qualification does not
turn this public cache into an AlphaFold3 parameter store.

Protenix v2 retains ByteDance's checkpoint URL as its canonical source. The
actual acquisition URL is `TMF001/protenix-v2-weights` at immutable commit
`653edab28103133512575365130916e3fd23ecc3`. The downloaded object matched its
declared 1,859,785,497-byte size, SHA-256 and MD5. A `--network none` safe
CPU/mmap `weights_only` load in pinned image digest
`sha256:ad2a55f1740f49296ec730e9ff4f1d06ad391a87354f03b2921f960fe0f6d240`
observed the exact root `dict` key `model`, an `OrderedDict` containing 4,174
float32 tensors, and 464,442,431 elements. The complete key/shape inventory
also matched the architecture built from exact source revision
`2475421477ab414b571149ad4a875c390ff8a35d`. It is therefore available only in
the explicit state `mirror-verified-not-publisher-byte-compared`. It is not a
publisher-byte match or an H100 semantic qualification; that older image digest
is inspection-only and remains explicitly disallowed for deployment.

Because the runtime artifact combines that mirrored checkpoint with four
publisher-hosted common-data files, its `artifact-manifest/v1` source identifies
the repository-owned composite declaration and a revision string containing the
exact code, mirror, and common-data revisions. The unavailable publisher URI is
retained only as canonical provenance; every downloaded object remains recorded
separately in the catalog and must reconcile exactly with the manifest inventory.

The source cache tree retains the checkpoint at relative path
`checkpoint/protenix-v2.pt` and the four exact official common-data files under
`common/`. `public_artifacts.py materialize` transforms only a verified source
receipt into one immutable runtime root at `/models/protenix-v2`. That root has
exactly seven files: checkpoint, four common files, `manifest.json`, and the
single `.fs2-manifest-sha256` ready marker. The canonical source-inventory
digest is `8e14bb809d37db806159b7d277577abc692aec81d8899fbc84915d23ebe12eca`,
the manifest digest is
`a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7`,
and the complete localized-tree digest is
`5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48`.
Readiness requires both the source-cache receipt and this composite
localization receipt; legacy per-checkpoint markers and a split
`/databases/protenix` handoff are rejected. The exact existing local checkpoint
and common-data bytes passed a real offline materialization/hash smoke; its
temporary hardlink tree was removed afterward. The non-secret receipt is
`evidence/protenix-v2-localization-smoke-20260902.json`.

After the cache receipt exists, create or re-verify that single runtime tree
with the artifact-owned localizer (the destination must be the exact mount root,
not a checkpoint subdirectory):

```bash
python3 model-artifacts/public_artifacts.py materialize \
  --catalog model-artifacts/artifact-catalog.json \
  --artifact protenix-v2 \
  --cache-root /reference-data/model-artifacts/public/v1 \
  --destination /models/protenix-v2
```

The catalog also defines the two-stage offline semantic smoke: CPU preparation
in namespace/queue/pool `fs2-reference-data`, followed by H100 SM90 prediction
on context `k8s-inference-h100`. It remains
`required-not-yet-qualified` until the corrected `-h100-r2` image digest, public
cache receipt, composite localization receipt, observed H100 identity, offline-egress
enforcement, parseable mmCIF, finite confidence JSON, and semantic-validator
pass are all recorded. The checked-in minimal input is
`smoke/protenix-v2-minimal.json`.

Reproduce the checkpoint inspection against an exact source checkout with:

```bash
python3 model-artifacts/inspect_protenix_v2.py \
  --checkpoint /secure/local/path/protenix-v2.pt \
  --source-tree /secure/local/path/Protenix-2475421477ab414b571149ad4a875c390ff8a35d
```

Two entries intentionally remain non-ready:

- `alphafold3-private` and `pyrosetta-private`: these stay in the owner-only
  academic-assets path and are never copied into this cache.

The BoltzGen molecule artifact is public MIT data from
`boltzgen/inference-data@c3d36fd276e9caf098c75d4113c6d5eb320b1a4c`.
Its exact `mols.zip` is 391,401,102 bytes with SHA-256
`3d4f56ac4262e745bb3d09cfaa19099b1d01be208122d501667b952e45521e53`;
the artifact inventory digest is
`64cdf690708fbf7c2955c4113e1ba8bc7ac7c6bb5031ec1d15e909da1256e86b`.
The consumer binding is
`/opt/fs2/artifacts/boltzgen-inference-molecules`, and runtime integration must
materialize the ZIP contents there before passing that directory to
`--moldir`. The central directory proves exactly 45,227 unique flat-root PKL
files, 1,820,698,819 expanded bytes, with canonical path/size/CRC inventory
SHA-256 `8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc`.

License evidence is recorded in the catalog's `licenses` map. Source-specific
license conclusions come from the pinned source-qualification contract under
`models/cancer-immunotherapy/`; source artifacts retain their upstream names.

## Validate and stage after the integration gate

Run all offline checks:

```bash
./model-artifacts/run_checks.sh
```

Validate the target-specific reference-plane handoff. `terraform plan` must
contain zero resource changes and both explicit integration booleans must be
true. Values come from the integrated reference-data outputs and live state,
not from this repository's examples:

```bash
terraform -chdir=model-artifacts/terraform plan \
  -var-file=/secure/path/reference-plane.tfvars.json
```

Only after that plan is merged/applied, verify the exact H100 context and render
all available artifact Jobs. All target identities remain parameters:

```bash
export KUBECONFIG="${H100_KUBECONFIG:?set to the operator-private H100 kubeconfig}"
PROJECT_ID="${TARGET_PROJECT_ID:?set from private deployment inputs}"
REGION="${TARGET_REGION:?set from private deployment inputs}"
CLUSTER="${TARGET_CLUSTER_CONTEXT:?set from private deployment inputs}"
test "$(kubectl config current-context)" = "$CLUSTER"
python3 model-artifacts/render_jobs.py \
  --catalog model-artifacts/artifact-catalog.json \
  --project-id "$PROJECT_ID" --region "$REGION" --cluster "$CLUSTER" \
  --filesystem-id "$FILESYSTEM_ID" --filesystem-size-gib 2048 \
  --namespace "$REFERENCE_NAMESPACE" --local-queue "$REFERENCE_LOCAL_QUEUE" \
  --service-account "$REFERENCE_SERVICE_ACCOUNT" \
  --shared-filesystem-host-path "$REFERENCE_FILESYSTEM_HOST_PATH" \
  --cpu-pool-id "$REFERENCE_CPU_POOL_ID" \
  --cpu-pool-name "$REFERENCE_CPU_POOL_NAME" \
  --node-selector "$REFERENCE_CPU_NODE_SELECTOR_JSON" \
  --node-toleration "$REFERENCE_CPU_TOLERATION_JSON" \
  --reference-plane-source-commit "$REFERENCE_PLANE_COMMIT" \
  --source-commit "$GIT_COMMIT" \
  > /secure/path/public-artifact-jobs.json
kubectl apply -f /secure/path/public-artifact-jobs.json
```

Jobs are content-addressed and safe to rerun. Collect receipts only from
`/reference-data/model-artifacts/public/v1/receipts`; never copy the cached
object tree into Git. A deployment evidence document records the exact plan,
filesystem, namespace, queue/pool, resources, hashes, offline checks, retained
cache, and temporary Job/ConfigMap cleanup.

## Interrupted wrong-target attempt

On 2026-09-02 an initial attempt used a task-created 8 GiB PVC backed by the
legacy 128 GiB filesystem and scheduled Jobs on the system node. A manager
stopped the attempt. The 16 Jobs were deleted, then the exact task ConfigMap,
PVC, dynamically provisioned PV, and task-local Terraform state entry were
removed. The partial cache bytes were reclaimed with that PV; source discovery
bytes remain only in local operator state outside Git. See
`evidence/live-staging-cleanup-20260902.json`. Staging must not resume until the
reference-data branch is integrated and its reviewed foundation/workload apply
creates the dedicated namespace, queue, service account, and storage handoff.

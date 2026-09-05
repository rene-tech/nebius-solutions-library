# Secondary structure runtime images

This directory reproducibly builds four weight-free `linux/amd64` runtime
identities. Code, base images, model identities, artifact mounts, and unique
H100 build tags are locked in `image-lock.json`. Model weights, parameters,
CCDs, MSAs, and reference databases are never copied into an image.

Native AlphaFold3 is intentionally not built here. Its clean image, wrapper,
academic parameter binding, public-database binding, persistent JAX cache, and
H100 acceptance evidence are owned by the dedicated AlphaFold3 successor under
`models/cancer-immunotherapy/images/alphafold3/`. Current main records the
authorized academic-only parameter object as `alphafold3-parameters`, source
subpath `alphafold3/af3.bin.zst`, consumer path `/models/af3.bin.zst`, size
1,020,545,840 bytes, and SHA-256
`74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff`.
That asset is available to the authorized academic PoC and is not treated as
request-time license-gated. This publisher neither duplicates that image nor
claims its final database/runtime readiness.

## Exact identities and external artifacts

| Runtime | Code | Required external artifacts |
|---|---|---|
| ESMFold2 | Biohub ESM `827ec128e4cdaf80f7d6f95fb367a08980b34918` (`v3.4.0`) | trunk `8fc3ff471022fdce52c77030685eb775de0c00a3`, ESMC-6B `45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a`, and CCD SHA-256 `9ff44b19…38fc5` |
| ESMFold2-Fast | same code, distinct runtime/model/tag | Fast trunk `c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6`, the same exact ESMC-6B, and the same exact CCD |
| Protenix v2 | `2475421477ab414b571149ad4a875c390ff8a35d` (`v2.0.0`) | one composite artifact candidate `protenix-v2` at `/models/protenix-v2`, containing the required mirror-verified checkpoint candidate, four common files, `manifest.json`, and `.fs2-manifest-sha256` |
| OpenFold3 | `c4771653c5d0a3ebb0b3af71b05efd64bc44ee86` (`v0.5.0`) | OpenBind-0 checkpoint SHA-256 `bd43301c…e29e4` and `components.bcif` SHA-256 `473d845c…fcc0c` |

The scanner-free `-h100-r3` publication from clean image source
`ebed271a555488e87cd91157cd812101f0091fde`, accepted main
`9d48fe0ef380ec736e113a89215f3730534693ad`, and exact runtime adapter
`47ef64d479418eb823f2dc0761d8630be295b831` produced four immutable manifests.
The subsequent direct-command H100 run found that the ESMFold2, Fast, and
OpenFold executables depended on entrypoint-only environment activation. They
pulled in 101.300 s, 100.022 s, and 198.843 s respectively, then exited 127.
Protenix pulled in 76.405 s and passed its direct build smoke in 7 s, but that
generation is also rolled forward to one uniform self-activating executable
boundary. These digests are retained only as superseded evidence:

| Runtime | Immutable regional image |
|---|---|
| ESMFold2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:24f40e67f332ecf694cb0dbb06a4a2a6cb0d49d8ee8fffd2c1d96631c7d3af14` |
| ESMFold2-Fast | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2-fast@sha256:ad7948c23817f61304c570077a62e3734c4afecfb18a05ea7cb08bd0551f553e` |
| Protenix v2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/protenix-v2@sha256:beeaa5173f102656437724376bad858de54232ef7ad1e342b8c2534428775494` |
| OpenFold3 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@sha256:ca05bb15341045bb2876153faccb0d029a27b0050350d6b86a83a76b4fa73bb4` |

The historical corrected `-h100-r4` build candidates were published from clean source
`e6d20c7cb3abf5e172852f17a20c7e100daa1245` after each passed its direct
nonroot/network-disabled build smoke and direct CLI invocation:

| Runtime | Immutable regional image |
|---|---|
| ESMFold2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:e8fb269ff17e752ed8dd8f6c4689eaa55c0efc7adaffc156ccd9357bd075463d` |
| ESMFold2-Fast | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2-fast@sha256:ba55b9bb418d9714b21634c9fd6281f678529042bc3d0b8f06f184fa314a2577` |
| Protenix v2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/protenix-v2@sha256:27d816dc518b5dda205f9916205fbc4e2053a8109d9380b85628d9f0d968a644` |
| OpenFold3 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@sha256:d1d249fcd8aca464ff0ee0b6e78e0f9c1fe243e0ebd18acc3c4223070fcf203b` |

The repaired `-h100-r5` successors were then built from exact clean pushed
source `a1b5b6b24b2dae54a1f7caeba9981a7aaa60cc8f`. Each image passed the
repository-owned offline, network-disabled, non-root `10001:10001` smoke and
direct CLI check. SPDX documents were generated and hashed; the secondary
publisher did not produce a SLSA or signed provenance attestation, so none is
claimed. The immutable publication evidence is recorded under `evidence/`.

| Runtime | Immutable regional successor |
|---|---|
| ESMFold2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:870b9f647f41bb02cfcbf08d5eec6cdf6b5171e8771c776248c5865c2f762a4a` |
| ESMFold2-Fast | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2-fast@sha256:fc7b8687849511a04b04afd9c477bcc0fb85a2837eac6ac658609e8b7e2702e0` |
| Protenix v2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/protenix-v2@sha256:b90a02bdffe3eefa8a251eb1e3666f3748a72e68fdec0b3cd867c2f08b426af8` |
| OpenFold3 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@sha256:3686e5303cbe51b18949b5f5815336db8ca31100b72c8d4b676f848fb193b1de` |

The r5 images carry the repaired atomic handoff, frozen input/invocation
identity, bounded collectors, and shared workspace protocol. They are retained
as superseded evidence: their earlier semantic receipts do not transfer across
the later localization-marker protocol change.

The controller's production artifact localizer subsequently enriched each v1
runtime marker record with manifest, readiness, authorization, and localization
receipt identities. The r5 ESMFold2/Fast/Protenix images and the semantically
tested r6 OpenFold image rejected those additional fields before inference.
That qualification does not transfer across a runtime protocol change. Exact
clean pushed source `a25251748608f3f437277e3c1c3c91896d5dc482` therefore
published four new non-overwriting successors whose shared marker validator
accepts both the legacy eight-field and current enriched eleven-field v1 shapes
while retaining exact artifact, mount, content, manifest, file, and receipt
checks:

| Runtime | Immutable localization-compatible successor |
|---|---|
| ESMFold2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3` |
| ESMFold2-Fast | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2-fast@sha256:6eaf386a9bb4453d5048e16c28b8ca4236ae0f222185e33d5a7a49a1e1c8fa35` |
| Protenix v2 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/protenix-v2@sha256:ac8f7c2c35d2bc911281f9d4a8aa9779e2cb955cdb1c2c2d37eb31d89669980e` |
| OpenFold3 | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/openfold3-upstream@sha256:6b15da4b2258c0c385adc1dbc7799493f3768cb4881f7990cb957f2c3b6759e4` |

All four passed the publisher's offline network-disabled non-root smoke, direct
CLI check, exact generated-adapter-argv contract, registry readback, and SPDX
generation. Fresh exact-digest H100 Jobs then loaded the content-addressed
external artifacts and produced non-empty validated structures. The current
lock state is `qualified-exact-artifact-h100-semantic`, all four digests are
deployable, and their active workload projections use these exact identities.
Publication evidence remains in
`evidence/localization-marker-successor-publication-20260904.json`; the H100
receipts and four-model summary are retained in `evidence/live-h100-20260905/`.

This evidence qualifies H100 semantics and L1 regional image caching. It does
not qualify Blackwell portability or the L2-L4 snapshot and memory-retention
levels.

### H100 image-start evidence

Temporary task-owned Jobs used context `k8s-inference-h100` in project
`project-e00rene`, region `eu-north1`, and requested one H100 SXM5 80 GB GPU
each. No shared route or model Deployment was changed.

The first concurrent prepared-node run on reserved node
(the reserved capacity-block node) measured exact-digest pull times of
101.470 s (ESMFold2), 99.729 s (Fast), 0.306 s (Protenix; its layers were
already resident), and 194.623 s (OpenFold3). A second concurrent run on the
same prepared node measured warm digest checks of 55 ms, 59 ms, 57 ms, and
52 ms respectively. Approximate container-start-to-final-JSON times were
10.4 s, 10.4 s, 6.6 s, and 20.4 s. Every final JSON reported the exact package
and source revision, `cuda_available=true`, and `status=passed`.

A separate preemptible campaign used the stable `h100-1x` pool selector and
two nodes created at `2026-09-03T05:30:46Z`. Task-owned topology anchors kept
unrelated images on opposite nodes without hostname pinning. Exact-image cold
pulls where that target's layers were absent were 66.133 s for ESMFold2 on
preemptible node A, 152.868 s for OpenFold3 on
preemptible node B, 63.756 s for Fast on the latter node,
and 105.434 s for Protenix on the former. Approximate
container-start-to-final-JSON times were 11.5 s, 21.5 s, 11.2 s, and 5.8 s.
All task Jobs and topology anchors were deleted after evidence capture.

Those earlier image-start measurements were build-only GPU runtime tests. Their
smokes deliberately proved that `/models` and `/databases` were empty and did
not qualify inference. The successor qualification campaign on 2026-09-05 used
the operator-owned reference-data plane and the exact enriched localization
markers. ESMFold2, ESMFold2-Fast, Protenix v2, and OpenFold3 passed in 43.008 s,
39.991 s, 93.325 s, and 59.906 s of semantic runtime respectively. Every Job
and Pod succeeded, each result image matched the kubelet digest, and every
result contained a checksummed structure. Shared routes were unchanged during
the campaign.

OpenFold3 is an independent, non-equivalent backend; it is never reported as
native AlphaFold3. The Protenix v2 checkpoint was recovered from the immutable
third-party mirror `TMF001/protenix-v2-weights@653edab…ecc3` and validated as
1,859,785,497 bytes with SHA-256 `8f931f97…0d599`. It has not yet been
byte-compared with the publisher CDN object because that endpoint returned a
region-specific 403; the mirror provenance limitation remains explicit.

Protenix CPU prep and prediction both fail unless the single mounted artifact
manifest identifies the exact code, checkpoint, and common-data revisions,
binds the official `common.tar.gz` at 475,085,654 bytes and SHA-256
`08ea594f…4dbd`, and enumerates the checkpoint plus
all four required common files with byte counts and SHA-256 identities:

- `common/components.cif`
- `common/components.cif.rdkit_mol.pkl`
- `common/clusters-by-entity-40.txt`
- `common/obsolete_release_date.csv`

The GPU stage consumes the relocatable prep output and weights from the same
artifact root. It sets `PROTENIX_ROOT_DIR=/models/protenix-v2`; an explicit
`none` handoff disables MSA/template/RNA-MSA consumption. Precomputed MSA is
unsupported until every chain field and referenced file has a validated
relocation contract. No supported mode can invoke an online MSA server, and the
wrapper cannot accept pass-through arguments that change input, output, model,
or online behavior. Cache localization hashes all
five payload files once, writes their path-independent identities to
`manifest.json`, then atomically promotes the tree with one
`.fs2-manifest-sha256` ready marker. Both stages validate that one marker and
cheap file-size guards rather than rehashing the 1.86 GB checkpoint per run.
The required candidate localized-tree content digest is
`5e1c3b548af40752bb15f9f2ba06590e20e2b165e3fe9ab3fa99af9977574d48`;
the required composite-manifest/ready-marker acceptance digest is
`a093d28ecfc8374f143cc32ff713b0e6ad1124c095dbbca5af6e51b4f7dcc6b7`.
That identity hashes the artifact worker's deterministic compact JSON encoding plus
one trailing newline; omitting the newline produces a different, invalid identity.
The image validates the latter cheaply on every invocation, while the
controller-owned localizer must bind both immutable identities in its runtime
mount. The image build imports the installed wheel and executes its patched
`prep` callback with service functions instrumented to fail, proving the
`none` lane uses `use_msa=false`, `use_template=false`, and
`use_rna_msa=false`.

## Runtime boundaries

The exact scripts copied into the images are:

```text
/opt/fs2/run_esmfold2.py prepare-input|fold
/opt/fs2/run_protenix.py prep|pred
/opt/fs2/run_openfold3.py prepare|predict
```

Equivalent `/usr/local/bin/fs2-run-*` commands are the image runtime entry
points. The lock’s `runtime_contract.commands` and `required_mounts` are the
machine-readable adapter boundary. The stable command forms are:

```text
fs2-run-esmfold2 prepare-input --input-manifest RAW --output REQUEST [--sequence AA] --mode MODE --seed N
fs2-run-esmfold2 fold --input REQUEST --output-dir OUT --variant esmfold2|esmfold2-fast --seed N --runtime-localization-marker RUNTIME_MARKER [--smoke]

fs2-run-protenix prep --input RAW --output-dir PREP --processed-json PREP/processed.json --provenance-marker PREP/provenance.json --handoff-tar HANDOFF.tar.zst --output-artifact-id ID --msa-mode none --reference-root /models/protenix-v2 --reference-manifest /models/protenix-v2/manifest.json --runtime-localization-marker RUNTIME_MARKER
fs2-run-protenix pred --input INPUT/processed.json --input-marker INPUT/provenance.json --input-artifact-id ID --output-dir OUT --checkpoint /models/protenix-v2/checkpoint/protenix-v2.pt --common-dir /models/protenix-v2/common --msa-mode none --seeds CSV --sample-count N --disable-templates --disable-rna-msa --runtime-localization-marker RUNTIME_MARKER

fs2-run-openfold3 prepare --input-manifest RAW --query-json PREP/query.json --base-runner-yaml /opt/fs2/runtime/openfold3/runner-base.yaml --runner-yaml PREP/runner.yaml --provenance-marker PREP/provenance.json --handoff-tar HANDOFF.tar.zst --output-artifact-id ID --raw-input-sha256 SHA256 --msa-mode none --model-seeds CSV --offline
fs2-run-openfold3 predict --query-json INPUT/query.json --provenance-marker INPUT/provenance.json --input-artifact-id ID --expected-raw-input-sha256 SHA256 --output-dir OUT --checkpoint /models/openfold3/of3-ob-2025-06-30-174k.pt --ccd-path /databases/openfold3/components.bcif --runner-yaml WORK/runner.yaml --base-runner-yaml /opt/fs2/runtime/openfold3/runner-base.yaml --num-diffusion-samples 1 --num-model-seeds SEED_COUNT --model-seeds CSV --msa-mode none --use-templates false --runtime-localization-marker RUNTIME_MARKER
```

ESM production defaults are 20 trunk loops and 200 diffusion steps;
the deliberately smaller 1-loop/2-step profile is available only through the
explicit `--smoke` flag. A non-Hopper ESM portability invocation selects SDPA
for ESMC and disables ESMFold2 trunk `FLASH_ATTN_AVAILABLE` before model
construction. The full and Fast identities mount their distinct trunks at
`/models/esmfold2` and `/models/esmfold2-fast`, respectively, and share exact
`/models/esmc-6b` plus `/databases/esmfold2/ccd.pkl`. A successful fold writes
the structure and sibling `confidence.json`. The envelope contains bounded
mean pLDDT in its native normalized `[0,1]` scale, pTM, and ipTM summaries plus the exact relative structure filename,
SHA-256, and byte size; it never serializes unbounded per-token pLDDT.
The controller-issued localization marker already binds the read-only CCD
artifact's exact aggregate content identity. The fold boundary therefore keeps
the exact 417,306,584-byte stat guard but deliberately does not rehash that file
on every invocation; avoiding a redundant 417 MB read protects cold-start time
without weakening the controller/localizer trust boundary.

OpenFold's single wrapper surface emits a relocatable zstd handoff containing
exactly `query.json` and a path-independent provenance marker. The image-owned
base runner configuration is regenerated with the exact ordered seed list in
both stages. Prediction verifies `num_model_seeds == len(model_seeds)`, uses one
diffusion sample per seed, but deliberately does not forward
`--num-model-seeds` upstream because v0.5.0 would replace the exact configured
seed list with generated values. It always supplies the exact checkpoint, forces the MSA
server and templates off, and calls the public
`biotite.structure.info.ccd.set_ccd_path()` API so all CCD-dependent caches are
cleared before the packaged `run_openfold predict` API runs. The `none` lane
sets all three MSA switches on each Query and rejects every path-bearing Chain
field because the handoff contains no referenced file. Its CPU prepare stage
has no checkpoint, CCD, or database dependency. Protenix and OpenFold3 return
from their upstream runner and then write the
same `fs2.nebius.ai/structure-confidence/v1` envelope. Every upstream summary
is paired one-to-one with its structure, all accepted metrics are finite and
bounded, and the result set must exactly cover every requested seed/sample pair
(never merely a nonempty subset). The shared
machine-readable contract is installed at `/opt/fs2/confidence.schema.json`.

## H100 readiness versus Blackwell portability

These are H100-tagged images. A build/import check is not semantic readiness.

The image contract exposes deployment-owned, persistent writable compiler
caches only where the runtime is known to compile. The deployment must mount
the listed root read-write for UID/GID 10001 and isolate its contents by the
exact runtime image, model artifacts, and H100 SM identity; an image-owned
directory alone is not persistent evidence.

| Runtime/stage | Persistent mount | Exact runtime environment | Auxiliary L1+ cache state |
|---|---|---|---|
| ESMFold2 / Fast fold | none | none proven | no compiler cache; regional image-cache and external-artifact timing remain separate evidence |
| Protenix v2 prediction | `/cache/protenix` | `TRITON_CACHE_DIR=/cache/protenix/triton`; `CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton`; `TORCH_EXTENSIONS_DIR=/cache/protenix/torch-extensions`; `XDG_CACHE_HOME=/cache/protenix/xdg` | persistent compiler cache declared; first-versus-warm H100 compile timing pending |
| OpenFold3 prediction | `/cache/openfold3` | `TRITON_CACHE_DIR=/cache/openfold3/triton`; `TORCH_EXTENSIONS_DIR=/cache/openfold3/torch-extensions`; `XDG_CACHE_HOME=/cache/openfold3/xdg` | persistent compiler cache declared; first-versus-warm H100 compile timing pending |

The fixed user-level taxonomy is:

- L1: regional image cache.
- L2: a real GPU/process snapshot restored from shared filesystem or enhanced object storage.
- L3: that snapshot cached on local disk.
- L4: the model retained in system RAM for GPU swap.

Compiler caches, external-artifact caches, and their first-versus-warm timing
cannot qualify L2. No GPU/process snapshot exists for these images, and there
is no local-disk snapshot or system-RAM-retained model evidence. Every lock
entry therefore has `maximum_candidate_level: L1`, `qualified_level: L1`, and
exact-artifact H100 semantic evidence for the regional image identity; L2, L3,
and L4 are explicitly unavailable.

| Runtime | Exact H100 state | Blackwell state |
|---|---|---|
| ESMFold2 / Fast | exact successor digest qualified with trunk, ESMC-6B, and CCD | not qualified; only an explicit SDPA portability path exists |
| Protenix v2 | exact successor digest qualified with the localized composite v2 tree | **unsupported** in this image: pinned PyTorch 2.7.1+cu126 libtorch has neither Blackwell cubins nor PTX |
| OpenFold3 | exact successor digest qualified with OpenBind-0 plus exact CCD | not qualified |

Protenix’s task-owned layer-normalization extension is prebuilt with an SM90
cubin and compute_90 PTX, then its CUDA source, build metadata, and `nvcc` path
are removed from the runtime stage. This does **not** eliminate all runtime
compilation: pinned
`cuequivariance-ops-torch` 0.8.0 uses Triton JIT for triangle operations above
its fallback thresholds, so the runtime retains `gcc` only for Triton's small
Python launcher build. Build-only smoke requires that launcher compiler while
also requiring `nvcc` to remain absent, then compiles and removes one bounded
Python-extension probe below the Triton cache. `TRITON_CACHE_DIR=/cache/protenix/triton`,
`CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton`,
`TORCH_EXTENSIONS_DIR=/cache/protenix/torch-extensions`, and
`XDG_CACHE_HOME=/cache/protenix/xdg` are writable stable paths;
mount `/cache/protenix` persistently to retain a warmed shape cache. Exact H100
first-call versus warm-call measurements remain pending the semantic run. That
one prebuilt extension does not make the whole CUDA 12.6 image
Blackwell-portable. Blackwell requires a separate CUDA 13 target build and its
own semantic acceptance evidence.

## Build and publication

Validate without changing a registry:

```bash
./check.sh
./build-and-publish.sh \
  --adapter-worktree /path/to/corrected-runtime-adapter-worktree \
  --output-dir /tmp/fs2-structure-image-evidence
```

The build/publish runner first fetches the current `origin/main` and refuses to
continue unless that exact remote commit is an ancestor of the task `HEAD` and
the task worktree is clean. It reports the task head, fetched main head, merge
base, and ahead/behind counts on failure. Reviewer-red remediation may continue
to use `check.sh` while dirty, but an accepted successor must first integrate
the then-current `origin/main` and rerun the exact source and external runtime
cross-contract gates. This prevents a build or publication from the stale
pre-infrastructure base.

`check.sh` always executes the OpenFold3 parser against an exact
`c4771653c5d0a3ebb0b3af71b05efd64bc44ee86` (`v0.5.0`) checkout and applies
the audited one-block Protenix patch to an exact
`2475421477ab414b571149ad4a875c390ff8a35d` (`v2.0.0`) checkout. It fetches
those tagged sources when verified local checkouts are not supplied. It also
creates a separate depth-one object store, fetches the pinned catalog contract
directly by immutable revision rather than through a moving branch ref, and
requires exact commit
`a1ecc219f5e319be87cfa20d5a79af1e3674c6f0`, and verifies generator SHA-256
`e7ec850a96daaf7d9463d953490d263069406ff4f1b125d400d75390372994b8`.
That revision preserves the integrated catalog bytes and adds the scientific
localization foundation; it deliberately does not manufacture missing
secondary-model live receipts. Immutable promotion receipts remain an H100
semantic-acceptance gate.
The clean store must not contain superseded unreachable commit `80d3b940...`.
Tests pin the exact generated `runtime-integration.json` and Protenix manifest
bytes before checking their mount/content/manifest identities against this
image contract. The build runner invokes this check first, so these pinned
parser/source/output checks are not an optional skipped acceptance path.

Before review freeze, execute the actual adapter compiler output—not copied argv
fixtures—through these parsers:

```bash
python3 tests/verify_runtime_adapter_contract.py \
  --adapter-worktree /path/to/fs2-secondary-adapters-clean-codex-r20260903
```

The parser-shape unit fixtures are not cross-contract evidence. The integration
gate requires the adapter commit to be the exact clean pushed branch head and
contained in current main. It imports that concrete commit, derives a fail-closed
candidate compile fixture from each of the four model-owned adapter contracts,
compiles all four real plans through the current `runtime_artifacts` seam,
executes their exact argv through the image parsers, and validates exact
artifact mount identities and marker shape against the wrapper's immutable
expectations. It does not read the shared workload profiles or the removed
`scientific-execution-targets.json` map. The marker validator requires the exact
model, variant, stage, artifact IDs, mount roots, aggregate content digests,
optional manifest digests and subpaths. This gate requires canonical ESM trunk
identities, both Protenix localization digests, both OpenFold content digests,
Protenix's multi-seed CSV surface, and OpenFold preparation with no
reference-data mount.
AlphaFold3's separate clean successor retains its own generated-argv,
database-promotion, stable-selector storage, academic namespace, and nonroot
persistent-cache gates; passing this four-image publisher never substitutes for
those independent gates.

The current post-publication cross-contract gate passed against exact clean
pushed image and adapter source `a25251748608f3f437277e3c1c3c91896d5dc482`.
It executed every generated ESMFold2, ESMFold2-Fast, Protenix v2, and OpenFold3
argv through the actual image parsers and returned `failures=[]`. The verifier
also byte-compared every runtime launcher, wrapper, and helper to that exact
published source, including shared `runtime_localization.py` SHA-256
`1288717c61207ff53a46cb791dc734dbb1c2dc7d91089be6c2ff53c54ba65a44`.

The Protenix and OpenFold model-owned contracts declare their exact cache mount
roots and environment, and generated argv carries those environment values.
The verifier records cache and localization delivery as
`pending-external-activation`: it does not assert that a PVC, localization
receipt, route, or other deployment resource exists.

The build script consumes lock v2 `repository` and `tag` fields. Those
repositories exactly match the runtime catalog: `cancer-immunotherapy/esmfold2`,
`cancer-immunotherapy/esmfold2-fast`, `cancer-immunotherapy/protenix-v2`,
`cancer-immunotherapy/openfold3-upstream`. A concrete clean adapter worktree is
required and the script runs the external cross-contract verifier before any
Docker build. The registry root remains operator-configurable without weakening
the schema:

```bash
FS2_REGISTRY_ROOT=registry.example/project/repository \
  ./build-and-publish.sh --adapter-worktree /path/to/corrected-runtime-adapter-worktree \
    --output-dir /tmp/fs2-structure-image-evidence esmfold2
./build-and-publish.sh --adapter-worktree /path/to/corrected-runtime-adapter-worktree \
  --registry-root registry.example/project/repository esmfold2-fast
```

Publication is a separate explicit operation:

```bash
./build-and-publish.sh --publish \
  --skip-sbom \
  --adapter-worktree /path/to/corrected-runtime-adapter-worktree \
  --output-dir /tmp/fs2-structure-image-evidence
```

`--skip-sbom` is an explicit user-directed exception for a publication run
whose earlier local scan evidence is being preserved without regeneration. The
new receipt records `sbom_state=skip-user-directed` and `sbom_sha256=null`; it
does not relabel earlier evidence as belonging to the newly built image.

Before the build and again immediately before a push, the script inspects each
derived destination and refuses to overwrite an existing tag. It never logs in,
prints credentials, or emits a mutable alias. Build receipts record the selected
registry root, target, local identity, smoke result, explicit SBOM state/hash,
and—only after a successful push—the registry digest. Receipt v2 also binds the exact clean image
source Git revision, fetched `origin/main` revision and merge base, ahead/behind
state, and the clean pushed runtime-adapter branch/revision whose generated argv
passed the external parser and mount contract.

`fs2-image-smoke --build-only` is the intentionally weight-free package/API
check used during image construction. For each declared compiler cache it
exact-compares the image environment with the lock, requires the final process
to run as UID/GID `10001:10001`, and performs a bounded create/read/remove probe
in the cache mount root and every declared cache directory. This proves only
that the unmounted image filesystem is writable by the final nonroot user; it
does not claim that a deployment-owned PVC or another persistent mount is ready.
The publisher validates this structured evidence against `image-lock.json`
before any push. The default smoke mode fails without a semantic request, exact
mounted artifacts, an output directory, and an H100; it loads the model offline
and requires an exact canonical confidence envelope.
Every bound PDB/mmCIF must match its recorded hash/byte count and contain at
least ten atom records. Run it in a
network-disabled task-owned H100 Job, for example:

```bash
/usr/local/bin/fs2-image-smoke \
  --semantic-request /work/processed-request.json \
  --output-dir /outputs/semantic-smoke \
  --seeds 101
```

OpenFold3 additionally requires materialized `query.json` and `runner.yaml` from
its preparation stage. Protenix requires the matching extracted
`provenance.json` plus logical artifact ID and the single
`protenix-v2` composite artifact. These semantic runs
are valid only on the `k8s-inference-h100` H100 target with the exact mounts.

No model endpoint or shared service is deployed by this image task.

## Superseded preliminary publications

Eleven task-owned historical or rejected manifests are retained under
`superseded_publications`; all have `deployable: false`. The four newest entries
are the prior production candidates that reject the current enriched
localization-marker fields. The historical AF3 image evidence is owned by the
dedicated AF3 successor and is not repeated here. The corrected earlier
publications remain immutable evidence. The current successor digests passed
their exact-artifact offline semantic H100 runs; the retained receipts are
under `evidence/live-h100-20260905/`.

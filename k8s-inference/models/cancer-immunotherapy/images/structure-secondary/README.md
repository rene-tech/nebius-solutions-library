# Secondary structure runtime images

This directory reproducibly builds five weight-free `linux/amd64` runtime
identities. Code, base images, model identities, artifact mounts, and unique
H100 build tags are locked in `image-lock.json`. Model weights, parameters,
CCDs, MSAs, and reference databases are never copied into an image.

## Exact identities and external artifacts

| Runtime | Code | Required external artifacts |
|---|---|---|
| ESMFold2 | Biohub ESM `827ec128e4cdaf80f7d6f95fb367a08980b34918` (`v3.4.0`) | trunk `8fc3ff471022fdce52c77030685eb775de0c00a3`, ESMC-6B `45b0fa5d7fb06faefbd5e3b89bdcef35d564e79a`, and CCD SHA-256 `9ff44b19…38fc5` |
| ESMFold2-Fast | same code, distinct runtime/model/tag | Fast trunk `c6c7958d63f5f2f1f0fed0bb9462316f8ccceea6`, the same exact ESMC-6B, and the same exact CCD |
| Protenix v2 | `2475421477ab414b571149ad4a875c390ff8a35d` (`v2.0.0`) | one composite artifact `protenix-v2` at `/models/protenix-v2`, containing the canonical checkpoint, four common files, `manifest.json`, and `.fs2-manifest-sha256` |
| AlphaFold3 | `85c4d20505fd5cef05eac22b534d4e793971ae69` (`v3.0.4`) | privately staged academic `/models/af3.bin.zst` and official public databases rooted at `/databases` |
| OpenFold3 | `c4771653c5d0a3ebb0b3af71b05efd64bc44ee86` (`v0.5.0`) | OpenBind-0 checkpoint SHA-256 `bd43301c…e29e4` and `components.bcif` SHA-256 `473d845c…fcc0c` |

OpenFold3 is an independent, non-equivalent backend; it is never reported as
native AlphaFold3. The Protenix v2 checkpoint was recovered from the immutable
third-party mirror `TMF001/protenix-v2-weights@653edab…ecc3` and validated as
1,859,785,497 bytes with SHA-256 `8f931f97…0d599`. It has not yet been
byte-compared with the unavailable publisher CDN object, so that limitation is
preserved in the lock and evidence.

Protenix CPU prep and prediction both fail unless the single mounted artifact
manifest identifies the exact code, checkpoint, and common-data revisions,
binds downloader SHA-256 `27da2585…dd89`, and enumerates the checkpoint plus
all four required common files with byte counts and SHA-256 identities:

- `common/components.cif`
- `common/components.cif.rdkit_mol.pkl`
- `common/clusters-by-entity-40.txt`
- `common/obsolete_release_date.csv`

The GPU stage consumes the enriched prep output and weights from the same
artifact root. It sets `PROTENIX_ROOT_DIR=/models/protenix-v2`; an explicit
`none` handoff disables MSA/template/RNA-MSA consumption, while `precomputed`
enables only MSA data already bound into the enriched input. No mode can invoke
an online MSA server, and the wrapper cannot accept pass-through arguments that
change input, output, model, or online behavior. Cache localization hashes all
five payload files once, writes their path-independent identities to
`manifest.json`, then atomically promotes the tree with one
`.fs2-manifest-sha256` ready marker. Both stages validate that one marker and
cheap file-size guards rather than rehashing the 1.86 GB checkpoint per run.

## Runtime boundaries

The exact scripts copied into the images are:

```text
/opt/fs2/run_esmfold2.py prepare-input|fold
/opt/fs2/run_protenix.py prep|pred
/opt/fs2/run_alphafold3.py data|inference
/opt/fs2/prepare_openfold3.py
/opt/fs2/run_openfold3.py
```

Equivalent `/usr/local/bin/fs2-run-*` commands are the image runtime entry
points. The lock’s `runtime_contract.commands` and `required_mounts` are the
machine-readable adapter boundary. The stable command forms are:

```text
fs2-run-esmfold2 prepare-input --input-manifest RAW --output REQUEST [--sequence AA] --mode MODE --seed N
fs2-run-esmfold2 fold --input REQUEST --output-dir OUT --variant esmfold2|esmfold2-fast --seed N [--smoke]

fs2-run-protenix prep --input RAW --output-dir PREP --processed-json ENRICHED --msa-mode none|precomputed
fs2-run-protenix pred --input ENRICHED --output-dir OUT --msa-mode none|precomputed --seeds CSV

fs2-run-alphafold3 data --input-json RAW --output-dir DATA --processed-json-output PROCESSED --seeds CSV
fs2-run-alphafold3 inference --processed-json PROCESSED --output-dir OUT --seeds CSV [--num-diffusion-samples N]

prepare_openfold3.py --input RAW --output-dir PREP --msa-mode none|precomputed --seeds CSV --offline
fs2-run-openfold3 --query-json PREP/query.json --output-dir OUT --runner-yaml PREP/runner.yaml --prepared-marker PREP/prepared-query.fs2.json --seeds CSV
```

ESM production defaults are 20 trunk loops and 200 diffusion steps;
the deliberately smaller 1-loop/2-step profile is available only through the
explicit `--smoke` flag. A non-Hopper ESM portability invocation selects SDPA
for ESMC and disables ESMFold2 trunk `FLASH_ATTN_AVAILABLE` before model
construction. The full and Fast identities mount their distinct trunks at
`/models/esmfold2` and `/models/esmfold2-fast`, respectively, and share exact
`/models/esmc-6b` plus `/databases/esmfold2/ccd.pkl`. A successful fold writes
the structure and sibling `confidence.json`. The envelope contains bounded
mean pLDDT, pTM, and ipTM summaries plus the exact relative structure filename,
SHA-256, and byte size; it never serializes unbounded per-token pLDDT.

AlphaFold3 does not expose an unrestricted upstream remainder. `data` accepts a
raw input JSON and runs only the CPU data pipeline; `inference` accepts the
processed JSON and its deterministic `.fs2.json` handoff marker, then runs only
GPU inference. The raw input is rewritten with the exact requested `modelSeeds`
before CPU processing, and inference rejects any processed JSON/marker whose
seeds, digest, or database root differs. Both commands use the exact native
command `/opt/alphafold3-venv/bin/python /opt/alphafold3/run_alphafold.py` with
explicit `--model_dir=/models` and `--db_dir=/databases`; protected upstream
arguments cannot be overridden.

OpenFold preparation validates the pinned external CCD and emits an immutable
query marker and a `runner.yaml` containing the exact ordered seed list.
Prediction validates that list, always supplies an explicit checkpoint, omits
the upstream random `--num-model-seeds` override, forces the MSA server off,
defaults templates off, and calls the public
`biotite.structure.info.ccd.set_ccd_path()` API so all CCD-dependent caches are
cleared before the packaged `run_openfold predict` API runs. Protenix,
AlphaFold3, and OpenFold3 return from their upstream runner and then write the
same `fs2.nebius.ai/structure-confidence/v1` envelope. Every upstream summary
is paired one-to-one with its structure, all accepted metrics are finite and
bounded, and result count is capped at 16 seeds by 16 samples. The shared
machine-readable contract is installed at `/opt/fs2/confidence.schema.json`.

## H100 readiness versus Blackwell portability

These are H100-tagged images. A build/import check is not semantic readiness.

| Runtime | Exact H100 state | Blackwell state |
|---|---|---|
| ESMFold2 / Fast | pending semantic run with exact trunk, ESMC-6B, and CCD | not qualified; only an explicit SDPA portability path exists |
| Protenix v2 | pending exact-checkpoint H100 run; CPU installed-path teardown previously exited 139 | **unsupported** in this image: pinned PyTorch 2.7.1+cu126 libtorch has neither Blackwell cubins nor PTX |
| AlphaFold3 | pending official-parameter and database H100 run | not qualified |
| OpenFold3 | pending OpenBind-0 plus exact CCD H100 run | not qualified |

Protenix’s task-owned layer-normalization extension is prebuilt with an SM90
cubin and compute_90 PTX, then its source/compiler paths are removed from the
runtime stage. This does **not** eliminate all runtime compilation: pinned
`cuequivariance-ops-torch` 0.8.0 uses Triton JIT for triangle operations above
its fallback thresholds. `TRITON_CACHE_DIR=/cache/protenix/triton` and
`CUEQ_TRITON_CACHE_DIR=/cache/protenix/cueq-triton` are writable stable paths;
mount `/cache/protenix` persistently to retain a warmed shape cache. Exact H100
first-call versus warm-call measurements remain pending the semantic run. That
one prebuilt extension does not make the whole CUDA 12.6 image
Blackwell-portable. Blackwell requires a separate CUDA 13 target build and its
own semantic qualification.

## Build and publication

Validate without changing a registry:

```bash
python3 -m unittest discover -s tests -v
./build-and-publish.sh --output-dir /tmp/fs2-structure-image-evidence
```

The build script consumes lock v2 `repository` and `tag` fields. The registry
root is operator-configurable without weakening the schema:

```bash
FS2_REGISTRY_ROOT=registry.example/project/repository \
  ./build-and-publish.sh --output-dir /tmp/fs2-structure-image-evidence esmfold2
./build-and-publish.sh --registry-root registry.example/project/repository esmfold2-fast
```

Publication is a separate explicit operation:

```bash
./build-and-publish.sh --publish --output-dir /tmp/fs2-structure-image-evidence
```

Before the build and again immediately before a push, the script inspects each
derived destination and refuses to overwrite an existing tag. It never logs in,
prints credentials, or emits a mutable alias. Build receipts record the selected
registry root, target, local identity, smoke result, SBOM hash, and—only after a
successful push—the registry digest.

`fs2-image-smoke --build-only` is the intentionally weight-free package/API
check used during image construction. The default smoke mode fails without a
semantic request, exact mounted artifacts, an output directory, and an H100; it
loads the model offline and requires a non-empty PDB/mmCIF result. Run it in a
network-disabled task-owned H100 Job, for example:

```bash
/usr/local/bin/fs2-image-smoke \
  --semantic-request /work/processed-request.json \
  --output-dir /outputs/semantic-smoke \
  --seeds 101
```

OpenFold3 additionally requires `--runner-yaml` and `--prepared-marker` from
its preparation stage; Protenix additionally requires the matching
`--msa-mode` and the single `protenix-v2` composite artifact. These semantic runs
are valid only on the `k8s-inference-h100` H100 target with the exact mounts.

No model endpoint or shared service is deployed by this image task.

## Superseded preliminary publications

Four pre-review tags were pushed before the independent findings arrived. Their
digests and reasons are retained under `superseded_publications`; all have
`deployable: false`. No OpenFold3 manifest was published. Corrected `-h100-r2`
tags must not be published until the build, no-JIT, artifact-closure, generated
argv, offline semantic-smoke, and layer-history gates pass.

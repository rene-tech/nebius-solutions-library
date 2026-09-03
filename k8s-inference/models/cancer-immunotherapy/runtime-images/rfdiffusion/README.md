# RFdiffusion scientific runtime

A source-attested, digest-pinned RFdiffusion v1.1.0 runtime for the shared H100
cluster, with a production adapter contract.

**Qualification state is authoritative in `image-lock.json` under `qualification`,
not in this file.** Evidence is bound to an exact image digest and never transfers
between digests: no receipt from r6, r7 or r8 says anything about the current image.
A digest is qualified only once `evidence/` holds a receipt naming that digest.

This is an independent successor. It starts from `main` and inherits no image, no
lock and no evidence from the mixed BindCraft/RFdiffusion branch. Where that work
was technically sound the same conclusions were re-derived here from primary
sources; where it was not, the reasons are recorded in `image-lock.json` under
`image.supersedes`.

## Identity

| What | Value |
| --- | --- |
| Upstream | `RosettaCommons/RFdiffusion` tag **v1.1.0** = `9273ef67335acaf91df0150473a274759229cdf6` |
| Source archive | `sha256:b8a29d4d5bd7b60eba40d49da4dbb324685eb409bdbdc7d088c187514ef3f7b9`, 8,107,250 bytes |
| Base image | `pytorch/pytorch@sha256:0279f7aa…` — PyTorch 2.3.0, CUDA 12.1, Python 3.10.14, linux/amd64 |
| DGL | `2.3.0+cu121`, wheel `sha256:0423c4e8…` |
| Checkpoint | `Base_ckpt.pt`, `sha256:0fcf7d7c…`, 483,616,107 bytes, BSD-3-Clause |
| Registry | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/rfdiffusion` |
| Tag | `9273ef67…-cuda121-r10` |

`v1.1.0` was resolved from the GitHub tags API rather than copied forward. The
superseded runtime at `models/structure/runtime/rfdiffusion` pins `86507b65`, which
is upstream main as of 2026-07-15 and not a release at all.

`ActiveSite_ckpt.pt` is byte-for-byte the same *size* as `Base_ckpt.pt`. The
checkpoint is therefore always matched on sha256 and never on size.

## Why r9, and what each predecessor got wrong

Both prior images are refused as a basis, for provenance rather than for behaviour:

- **r7** (`sha256:df6cb154…`) is the newest and the weakest. Its SLSA provenance
  records no VCS revision at all — only local build contexts — so nothing ties the
  image to a source commit. It also carries no H100 semantic evidence; the branch's
  semantic-validation, post-roll and scan records all still name r6.
- **r6** (`sha256:e502a326…`) is the only prior image with a real diffusion run. It
  is still refused as a source of truth because its OCI adapter label names commit
  `3475ce0e…`, which is a dangling object reachable from no branch or remote ref.
  An identity nobody can check out is not provenance.

- **r8** (`sha256:9aae23f0…`) was this successor's first image and *is* correctly
  attested — its SLSA provenance names clean commit `267cce49…`. It is superseded on
  a real defect its own exact-digest H100 run found: the adapter left
  `inference.schedule_directory_path` unset, so upstream tried to create its IGSO3
  schedule cache inside the read-only `/opt/rfdiffusion` and died after loading the
  checkpoint, before any diffusion. r9 passes the override and the end-to-end
  `main()` argv tests now pin it.

- **r9** (`sha256:c63e8229…`) was genuinely H100-qualified for `design-backbone` on two
  nodes and produced a byte-identical structure to r6 for the same seed. Superseded so a
  single digest could carry both operations, and to close a file-descriptor leak on the
  upstream stdout pipe.
- **r10** (`sha256:89eae6d0…`) succeeded on `design-backbone` and its `scaffold-motif`
  run produced a *correct* design — all 12 motif identities preserved, upstream
  reporting ~0.19 Å sampled motif RMSD — which the wrapper then wrongly rejected at
  47.956 Å. The validator compared raw coordinates, and RFdiffusion recentres its
  designs. Re-measured on those exact bytes the offset is a 47.96 Å translation with
  1.0° of rotation and the superposed CA RMSD is **0.1129 Å**. The failed receipt is
  kept at `evidence/superseded/`.
- **r11** (`sha256:1c0dce76…`) carries the superposition fix but was built before the
  scientific artifact localization foundation landed on main, so it predates the
  delivery semantics this runtime must consume.

`build_rfdiffusion.py` makes the r6/r7 provenance failures structurally impossible:
it refuses a dirty tree, and after pushing it reads the SLSA provenance back out of
the registry and fails unless the recorded VCS revision equals the commit the build
ran from. It already refused one image on exactly that gate.

## Adapter handoff

The scientific batch adapter owner keeps the typed public request schema and
translates it into this image's CLI. **That CLI and the internal
`rfdiffusion-parameters/v1` schema are frozen while they work** — see
`contract/README.md` for the handoff, `contract/fixtures/*/golden-argv.json` for the
exact argv each request produces, and `FrozenContractTests` for the enforcement.

## Layout

```
Dockerfile                          digest-pinned, weights-free, non-root
requirements.lock                   hash-pinned additions, installed --require-hashes
fetch_verified.py                   sha256-verified build-time downloader
runtime_entrypoint.py               the production adapter
build_rfdiffusion.py                build, publish, verify attestations
image-lock.json                     the immutable identity and the superseded chain
qualification/stage_checkpoint.py   stage the checkpoint as a content-addressed generation
qualification/render_job.py         render the semantic Job for a published digest
qualification/validate_result.py    independent acceptance gate over an exported run
evidence/                           live H100 receipts
tests/                              offline contract tests
run_checks.sh                       every offline check
```

## The adapter contract

```
python /opt/fs2/runtime_entrypoint.py run \
  --request <request.json> --input-manifest <input-manifest.json> \
  --output <dir> [--artifact-root <dir>] [--checkpoint-artifact-id <id>] \
  [--cache-level <level>] [--timeout-seconds <int>]

python /opt/fs2/runtime_entrypoint.py probe [--allow-cpu]
```

Parameters are `fs2-serve.nebius.ai/rfdiffusion-parameters/v1`; the result envelope
is `fs2-serve.nebius.ai/scientific-run-result/v1`. Operations are `design-backbone`
and `scaffold-motif`.

The order is: bound the request, resolve and verify the checkpoint, run upstream as
a shell-free argv vector, verify the artifact markers, and only then report success.
Success is never inferred from an exit code.

### Bounded contigs

A contig is `/`-separated. Each segment is a diffused span `N-M`, a motif span
`<chain><start>-<end>`, or `0` for a chain break. Nothing else parses. This is not
cosmetic: the contig list is concatenated into the Hydra override
`contigmap.contigs=[...]`, so the grammar is the only thing between a caller and
arbitrary upstream configuration. `ContigInjectionTests` covers fifteen escape
attempts, including `76-76] inference.deterministic=False [`.

Ceilings: 4 contig groups, 32 segments each, 512 residues total, 64 designs,
`diffuser_T` in 1..200, 64 hotspots.

### Deterministic seed

RFdiffusion v1.1.0 **has no `inference.seed`**. `run_inference.py` calls
`make_deterministic(i_des)` once per design, so the per-design seed is the design
index. A seed therefore maps onto `inference.design_startnum` with
`inference.deterministic=True`, and the envelope reports the design index that
actually seeded each design. Emitting `inference.seed=<n>` — as the superseded
wrapper did — sets a key Hydra does not have.

### Artifact markers

Per design, upstream must have written `<prefix>_<i>.pdb` and `<prefix>_<i>.trb`;
trajectories are optional and recorded when present. The `.trb` is upstream's own
run metadata and carries `torch.cuda.get_device_name()`, which is what proves the
diffusion ran on a GPU. A recorded device of `CPU` is a **failed** run, not a
degraded one.

`inference.cautious` defaults to `True` upstream, which silently skips a design whose
`.pdb` already exists. The adapter refuses to start unless the output directory is
empty, so a stale file can never be verified as a fresh design.

## Accelerator scope

**Qualified on H100 (sm90) only. Every other GPU family is unqualified.**

This runtime pins torch 2.3.0/cu121 and a prebuilt DGL cu121 wheel. Those ship a
fixed set of compiled kernels, and neither the cubins nor the PTX they carry have
been audited here, so there is no basis for claiming the image runs on another
family — "it does not hard-code a device name" is not evidence that it works. The
predecessor NIM lane is the cautionary case: it is recorded as
`incompatible-sm103` precisely because a family assumption went untested.

What is true: nothing in the image hard-codes a device selector, and
`render_job.py` selects hardware through one parameterised label,
`accelerator.fs2.nebius/class`, with no pool id and no capacity-source pin. So
adding a family is a matter of running it and recording per-family evidence, not
of editing this runtime. Accelerator breadth belongs in the Terraform resource
profiles; it is not a property this image asserts.

## Weights and the artifact plane

The image carries no model weights. The upstream v1.1.0 archive ships none either,
so nothing has to be deleted after extraction.

The checkpoint is mounted from the artifact plane and located through the input
manifest's *relative* path under `--artifact-root`. That is how the mount-path
conflict is resolved rather than papered over: the artifact catalog binds
`rfdiffusion-checkpoints` at `/models/rfdiffusion-checkpoints`, while the superseded
images hard-coded `FS2_ARTIFACT_ROOT=/models/rfdiffusion`. This image hard-codes
neither and works at whatever mount point the plane chooses.

`qualification/stage_checkpoint.py` promotes the checkpoint into

```
<root>/generations/rfdiffusion-base-checkpoint/sha256/<generation>/
```

using the `fs2-flat-tree-inventory/v1` digest and the localization plane's
`.fs2-runtime-tree.json` marker schema, so the tree moves to that plane without
restaging. Promotion is atomic and never overwrites an existing generation.

Artifact delivery is `catalog-accepted-localization-pending`. The
`rfdiffusion-checkpoints` catalog entry is now **accepted on main** (`9d48fe0e`), and
this runtime's checkpoint identity matches it exactly — `0fcf7d7c…`, 483,616,107
bytes, from the `files.ipd.uw.edu` URL the catalog names. What is still pending is
**live content-addressed localization and promotion**: nothing has published an
RFdiffusion generation onto the shared plane yet, so qualification reads a task-owned
generation on the shared qualification claim, staged with the localization plane's own
inventory algorithm and marker schema so it is portable without restaging. Because the
runtime resolves artifacts by sha256 through the manifest's relative paths, no image
change is needed when the canonical plane publishes.

**The route is closed.** `route_exposed` is `false` and this runtime is not servable
until the adapter/controller execution contract is reconciled (see `contract/`).

## Cache level

The envelope records a submitter-declared cache level from
`cold-registry-pull`, `image-local`, `artifact-local`, `image-and-artifact-local`,
`warm-process`, and always states `gpu_snapshot_used: false`. These are image and
filesystem cache levels. This runtime uses no GPU memory snapshot and no CRIU
restore, and nothing here is described as one.

## Checks

```bash
./run_checks.sh                                   # offline: compile, lock, golden-argv drift, 63 contract tests
python3 build_rfdiffusion.py --check              # lock and runtime inputs agree
python3 build_rfdiffusion.py --no-push            # build locally, no attestations
python3 build_rfdiffusion.py --record             # publish and record the digest
```

Live evidence is in `evidence/`.

## H100 acceptance

Published digest `sha256:c63e8229…`, run twice on the shared cluster
(`project-e00rene` / `eu-north1`, context `k8s-inference-h100`, namespace
`fs2-models`). In both runs the kubelet `imageID` was exactly the published
digest, and the checkpoint was re-verified by sha256 inside the container.

| | warm-image node | true-cold node |
| --- | --- | --- |
| Node | `…e00nycmfmarxv6kq9w` (elastic, autoscaled) | `…e00m0hsph76ajt9sdb` (capacity block) |
| Admission | Kueue `inference-models` | unqueued, node-pinned |
| Image pull | 0.30 s (layers resident) | 7.25 s |
| Model ready | 9.4 s | 35.9 s |
| 50-step diffusion | 20.5 s | 19.6 s |
| Adapter total | 31.4 s | 57.1 s |
| Schedule → semantic complete | — | ~67 s |

Both produced a 76-residue all-glycine backbone with a complete N/CA/C backbone on
every residue, CA-CA spacing within 0.125 Å of 3.8 Å, and a 30.5 Å extent. Upstream
recorded `NVIDIA H100 80GB HBM3` in the `.trb` for both.

**Determinism.** Both runs used seed 8100 on *different* H100 nodes and produced a
byte-identical structure, `sha256:78600be2f90811a2b5168c9f196825bb58468249b211ac176842f2cedb1d9806`
— committed at `evidence/design/design_8100.pdb`, so the claim is re-checkable. The
superseded r6 image, built independently on the mixed branch, recorded that same
output digest for the same seed. An independently rebuilt image reproducing a prior
image's exact bytes is real evidence that `seed → inference.design_startnum` is the
correct mapping.

**Cache level, honestly.** The first run's plan declared `cold-registry-pull`, but
that node already held the r8 layers and the pull took 296 ms, so the declaration was
wrong. The receipt records the declaration as made *and* the observed level, rather
than quietly rewriting it. The true-cold run is the honest cold-start number. Neither
is a GPU snapshot; this runtime uses none.

**Known cold-start cost.** On the true-cold run, checkpoint load plus IGSO3 schedule
calculation took 28.1 s against 20.1 s of actual diffusion, because the schedule cache
starts empty in every container. That is a fast-start optimisation, tracked in
`image-lock.json` under `follow_ups`, not a correctness problem.

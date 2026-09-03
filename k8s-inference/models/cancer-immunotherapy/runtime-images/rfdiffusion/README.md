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
| Tag | `9273ef67…-cuda121-r12` |

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
qualification/stage_checkpoint.py   verify/place an archival interim run input (not localization)
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

The shared localization contract publishes the checkpoint as the one-file
`rfdiffusion-base-checkpoint` generation on the public reference-data host plane. It
uses `verified-copy`, not an archive transform: source SHA-256 and byte count are
verified before `Base_ckpt.pt` is copied into private staging, the one-file
`fs2-raw-file/v1` identity is reverified, and the sealed generation is published by
atomic rename. The canonical mount is
`/opt/fs2/artifacts/rfdiffusion-base-checkpoint`, passed directly as
`--artifact-root`; the input manifest names `Base_ckpt.pt` relative to it.

The exact generation is
`7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c` and its
in-generation marker is
`abd2a8127d0bd1b3cbd51d5ffc14a3351f805e15f593c8224ee94de57e3e4599`.
On the live H100 cluster, the r12 image admitted that marker, re-hashed all
483,616,107 bytes and deserialized the checkpoint with PyTorch 2.3.0. The evidence
is under `../../artifact-localization/evidence/`.

The broader eight-file `rfdiffusion-checkpoints` artifact remains a separate catalog
identity. The historical `qualification/stage_checkpoint.py` helper and qualification
claim remain only to explain the already-recorded r12 semantic runs; new localization
checks use the canonical generation.

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

Published digest `sha256:3f18dd9c…`, both operations run on the shared cluster
(`project-e00rene` / `eu-north1`, context `k8s-inference-h100`, namespace
`fs2-models`, admitted through Kueue `inference-models`). In both runs the kubelet
`imageID` was exactly the published digest and the checkpoint was re-verified by
sha256 inside the container.

| | `design-backbone` | `scaffold-motif` |
| --- | --- | --- |
| Node | `…e00pwgcqf990qy867k` | `…e00rb3a26wtq7cjf88` |
| Residues | 76 (contig `76-76`) | 32 (contig `10-10/A23-34/10-10`) |
| Glycine fraction | 1.0 | 0.625 — the 12 motif residues are not glycine |
| CA–CA deviation | 0.125 Å | 0.137 Å |
| Model ready | 13.0 s | 12.0 s |
| 50-step diffusion | 19.1 s | 19.6 s |
| Adapter total | 33.8 s | 32.9 s |

Upstream recorded `NVIDIA H100 80GB HBM3` in the `.trb` for both.

**Motif preservation.** All 12 residues of the ubiquitin α-helix (`ILE GLU ASN VAL
LYS ALA LYS ILE GLN ASP LYS GLU`) kept their identity, with a superposed CA RMSD of
**0.1129 Å** against a 1.5 Å limit. The design sits **47.96 Å** from the reference
after a 0.98° rotation, which is RFdiffusion recentring its output — measuring raw
coordinates would call this a destroyed motif, and on r10 it did. The receipt records
the aligned value, the unaligned value and the rigid-body move, so the size of what
was removed is visible rather than assumed.

**Determinism.** Seed 8100 has now produced the byte-identical structure
`sha256:78600be2…` from three independently built images (r6 on the mixed branch, r9
and r12 here) across four different H100 nodes. Both structures are committed under
`evidence/design/`, so the claims stay re-checkable.

**Cache level, honestly.** Both runs landed on nodes that already held the layers, so
the ~290 ms pulls are not cold pulls and **this digest has no cold-start measurement**.
The cold-start numbers in the r9 receipt (7.25 s pull, 28.1 s checkpoint + IGSO3,
20.1 s diffusion, ~67 s schedule-to-complete) remain valid as scale but belong to a
different digest. No GPU snapshot is used and nothing here is described as one.

**Scope.** One run of each operation. No throughput, contention, elasticity,
multi-design fan-out or snapshot measurement.

# Proteina-Complexa scientific-batch runtime

A weights-free CUDA 12.6 runtime for NVIDIA's Proteina-Complexa, plus the
shell-free batch contract the scientific batch controller calls, and the
harness that qualifies all three model variants on real H100 hardware.

`image-lock.json` is the single source of truth. Every identity below is pinned
there, and `tests/test_proteina_complexa_runtime.py` fails if this document,
the entrypoint and the lock ever disagree.

## Exact identities

| What | Identity |
| --- | --- |
| Upstream source | `NVIDIA-BioNeMo/Proteina-Complexa` @ `54058860d43444c7289873f77d3e50b5b02348cd` |
| Source archive | `sha256:4a9448653fe9ae4e9e46c3204ef0e3c6ac9563a4cc5626c7a11d8441c485fb3b`, 3,880,947 bytes, 632 files |
| Framework | PyTorch `2.7.0+cu126`, CUDA ABI 12.6, `ptxas` 12.6.85 |
| Builder base | `nvidia/cuda:12.6.3-cudnn-devel-ubuntu24.04@sha256:50efab39…9cce3` |
| Runtime base | `nvidia/cuda:12.6.3-base-ubuntu24.04@sha256:c87e7893…5ee0e` |
| Dependency lock | `requirements.lock`, `sha256:0fb7e844…c2a71ed`, fully pinned |
| Registry | `cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/proteina-complexa` |
| Runs as | `10001:10001`, no shell in the contract path |

### The six public checkpoints

Each variant is a *pair*: a score model and its partial autoencoder. Mixing
pairs is the failure this contract exists to prevent, so all six are pinned by
byte count and SHA-256 and verified before a GPU is touched.

| Variant | Score model | Partial autoencoder | LoRA | Extra |
| --- | --- | --- | --- | --- |
| `protein` | `complexa.ckpt` (2,934,289,381 B) | `complexa_ae.ckpt` (4,100,101,779 B) | no | — |
| `ligand` | `complexa_ligand.ckpt` (1,790,554,392 B) | `complexa_ligand_ae.ckpt` (4,100,184,649 B) | r=32 | ligand features |
| `ame` | `complexa_ame.ckpt` (1,792,013,880 B) | `complexa_ame_ae.ckpt` (4,100,197,925 B) | r=32 | `USE_V2_COMPLEXA_ARCH=True`, motif + ligand |

All three are NVIDIA Open Model License, `entitlement_state: not-required`.
RosettaFold3 (`rf3_foundry_01_24_latest_remapped.ckpt`, 3,038,876,446 B,
BSD-3-Clause) is the reward and evaluation folding model for `ligand` and
`ame`; it is bound and generation-verified on every run, and *exercised* when a
request asks for `reward_model: upstream-default`.

### Where the checkpoints come from

Not from a claim this task fills. Each artifact is one **immutable public
generation** on the shared reference-data host plane, promoted by the ingestion
successor's terminal run `r20260903b` (four generations, zero failures):

| artifact | generation |
| --- | --- |
| `complexa-protein` | `eaaf891e…607536` |
| `complexa-ligand` | `61247c8d…d9cea5` |
| `complexa-ame` | `d38c622e…29afa5a` |
| `rosettafold3-checkpoint` | `d909fe65…3164bce` |

Host root `/mnt/fs2-reference-data/data`, sub-path
`scientific-localization/public/generations/<artifact id>/sha256/<generation>`,
mounted read-only. The plane root is mounted and the prefix carried in the
sub-path, which is the convention the localization foundation set. Every GPU
node in this cluster carries the required `storage.fs2.nebius/reference-data`
label.

Both artifacts are `visibility: public` and host-path, so this consumer reads
no tenant-private tree, needs no mixed-plane machinery, and never places a
public byte on the academic claim.

Before a GPU is touched the entrypoint reproduces the plane's own admission
rules: the reserved `.fs2-runtime-tree.json` marker must name this artifact,
generation, sub-path, plane, visibility, licence and inventory algorithm; the
marker's own document digest must equal the one the promotion receipt
published; and an `fs2-tree-inventory/v2` digest **recomputed from the mounted
bytes** must reproduce the generation name. A document can be edited; a
recomputed tree digest cannot.

### Target structures are an artifact too

A run that loads two multi-gigabyte checkpoints and then cannot find its target
has failed on a missing artifact, so the target structures are pinned like any
other: their public identity is the upstream source archive at
`54058860`, each file is pinned by digest, and each is verified before model
load. They are bound by an **image-baked** read-only working directory
(`/opt/fs2/complexa/workdir/assets -> /opt/fs2/source/assets`), never by a
writable `.env` and never by a symlink created at run time — the binding is
part of the image digest, and the entrypoint verifies it rather than repairing
it.

## What this image is, and is not

It **is** the upstream project at one reviewed revision with a pinned
dependency closure, a non-root user, caches that resolve under `/tmp`, and one
in-image entrypoint that turns a JSON request into a validated Complexa run.

It is **not** servable. `image.qualification.servable` is `false` and stays
false until both the three variants are qualified *and* the scientific batch
controller route is exercised end to end. This slice does not own or deploy
that route.

No weights are baked in. `runtime_probe.py` fails the build if any
weight-shaped file appears inside the source tree.

## Runtime contract

Shell-free by construction: `command` is an argv list whose first element is
the interpreter.

```
python /opt/fs2/complexa/runtime_entrypoint.py run \
    --request     /opt/fs2/complexa/request-<variant>.json \
    --output-root /workspace/<variant> \
    --cache-level image-local
```

`python /opt/fs2/complexa/runtime_entrypoint.py describe` prints the descriptor
(variants, checkpoint pairs, shared artifacts, GPU-snapshot posture) and is one
of the image's build-time smoke checks.

Request — `fs2-serve.nebius.ai/proteina-complexa-batch-request/v1`:

```json
{
  "variant": "ligand",
  "task_name": "39_7V11_LIGAND",
  "samples": 1,
  "batch_size": 1,
  "nsteps": 400,
  "seed": 20260903,
  "reward_model": "none",
  "verify_content_digests": true
}
```

Result — `fs2-serve.nebius.ai/proteina-complexa-batch-result/v1`, written to
`<output-root>/result.json` alongside `upstream.log` and the produced
structures. It carries `terminal_state` (`PASS` only after upstream exits
zero), the resolved `argv`, `artifact_verification`, `cuda` device identity,
`phases`, `validation`, and a `gpu_snapshot` block that always reports
`captured: false, restored: false` — nothing here captures or restores a device
snapshot, so nothing here may claim one.

Mounts the caller must provide:

| Path | Contents | Mode |
| --- | --- | --- |
| `/opt/fs2/artifacts/complexa-<variant>` | that variant's generation | read-only |
| `/opt/fs2/artifacts/rosettafold3-checkpoint` | the RF3 generation | read-only |
| `/workspace` | outputs | read-write as gid 10001 |
| `/tmp` | caches | read-write |

The two artifact mounts are sub-paths of one `hostPath` volume on the plane
root; only `/workspace` is a claim, and it holds nothing but this run's own
output.

The output volume must be writable by the runtime user. On
`csi-mounted-fs-path-sc` the volume root is root-owned, so the pod needs
`fsGroup: 10001`; `fsGroupChangePolicy: OnRootMismatch` keeps that from
becoming a recursive ownership walk over the artifact tree.

Placement is capability-driven. The plan selects `nebius.com/gpu: "true"` and
the entrypoint admits any device PyTorch reports as CUDA-capable, recording its
name and compute capability. There is no device-name allowlist and no H100-only
check. H100 (sm90) is the present acceptance target; sm100 parts are outside
the compiled `TORCH_CUDA_ARCH_LIST`, which is a build-input change recorded in
the lock, not a code change.

## Upstream defects this contract absorbs

Five are registered in `image-lock.json` → `upstream_contract_defects`; none
needs an upstream patch.

1. **Relative `target_path`.** The target dictionaries ship
   `./assets/target_data/...`, so any process whose working directory is not
   the source root dies in the conditional-feature constructor. The entrypoint
   resolves each target against `/opt/fs2/source` and passes an absolute
   override.
2. **The CLI wants a writable `.env`.** The packaged `complexa` console script
   does; `proteinfoundation.generate` only calls `load_dotenv()`, which
   tolerates an absent file. The contract invokes the module directly. This is
   what unblocked a predecessor attempt recorded as
   `forward_result: blocked`.
3. **Unset `DATA_PATH` / `RF3_EXEC_PATH`.** Read by upstream configs, never set
   by the image. Both are now image environment defaults.
4. **Two-pass LoRA load.** Lightning first reports the LoRA tensors as
   unexpected keys, which is indistinguishable from a genuinely skipped
   adapter. The entrypoint requires the re-application marker for `ligand` and
   `ame` and forbids it for `protein`.
5. **The upstream AME default task has no bundled structure.** `M0096_1chm`
   resolves through a directory that does not exist in the source tree. The
   contract admits only tasks whose structure ships with the image.

## Qualification

`qualification/` holds the harness, split so each stage can be reviewed alone:

| Script | Role |
| --- | --- |
| `render_plan.py` | render the ConfigMap and the variant Jobs as pure data |
| `submit_plan.py` | apply the plan, then collect node, image-phase and schedule-to-complete evidence |
| `validate_result.py` | re-derive the verdict from the artifacts without importing the entrypoint |
| `assemble_evidence.py` | turn the receipts and the verdict into the two evidence documents |

`render_plan.py --reward-model upstream-default --variant ligand --variant ame`
renders the RosettaFold3 runs. It refuses `--variant protein` with a reward
model, because the protein pipeline's reward is AlphaFold2 and no
`alphafold2-params` generation is published on this plane — the merged
localization contract declares that artifact with a `proteina-complexa`
consumer bound by `AF2_DIR`, but its binding handoff still reports
`generations_published: false`.

Run the offline suite with `./run_checks.sh`. Live evidence lands in
`evidence/`.

### Why the predecessor digests are not enough

`sha256:d3f3c9bc…a91d8` and `sha256:f4e06b60…3679ca` are both recorded as
superseded and not deployable. The second one's only H100 evidence was an
AlphaFold2 multimer parameter load through ColabDesign, driven by a
`bash -lc` wrapper that wrote a `.env` file. That exercises a *reward-model
dependency* of the protein pipeline — it is not Proteina-Complexa inference,
and it qualified no variant. Both also carry no OCI referrer, so neither has
attached SLSA provenance, and both were built from an unmerged branch commit.

Their source tree, however, is sound: all 632 files of `/opt/fs2/source` in
`f4e06b60` are byte-identical to the pinned upstream archive
(`evidence/source-equivalence.json`). The rebuild therefore changes provenance
and contract, not upstream code.

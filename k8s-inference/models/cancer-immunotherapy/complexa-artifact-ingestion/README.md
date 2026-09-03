# Proteina-Complexa and RoseTTAFold3 artifact ingestion

Proteina-Complexa cannot be qualified on H100 until its checkpoints are on the
cluster. This directory acquires them from their pinned upstream revisions,
proves every byte, and hands the image worker an immutable path.

## What is ingested

| Artifact | Upstream | Pinned revision | Files | Bytes |
| --- | --- | --- | --- | --- |
| `complexa-protein` | `nvidia/NV-Proteina-Complexa-Protein-Target-160M-v1` | `ffed199e` | `complexa.ckpt`, `complexa_ae.ckpt` | 7,034,391,160 |
| `complexa-ligand` | `nvidia/NV-Proteina-Complexa-Ligand-Target-160M-v1` | `bc90c8b2` | `complexa_ligand.ckpt`, `complexa_ligand_ae.ckpt` | 5,890,739,041 |
| `complexa-ame` | `nvidia/NV-Proteina-Complexa-AME-160M-v1` | `9743d749` | `complexa_ame.ckpt`, `complexa_ame_ae.ckpt` | 5,892,211,805 |
| `rosettafold3-checkpoint` | `files.ipd.uw.edu/pub/rf3` | `foundry-production-b02eed6a-checksum-lock` | `rf3_foundry_01_24_latest_remapped.ckpt` | 3,038,876,446 |

All four are ungated: the six NVIDIA files resolve without a token under the
NVIDIA Open Model License, and the RF3 reward checkpoint is served directly
under BSD-3-Clause. No credential is read, stored, or needed, and no artifact
byte is committed to Git.

The per-file sizes and SHA-256 digests in `ingestion-contract.json` come verbatim
from the accepted public artifact catalog in `k8s-inference/model-artifacts/`,
which is the authority on what these artifacts are. Only those identities were
taken from it, never its cache-path contract.

They are restated in this contract rather than read from the catalog because the
staging job runs in-cluster from a ConfigMap and cannot read the repository. That
duplication is guarded: `CatalogAgreementTests` compares every revision, file
digest, byte count, licence and resolved URL against the catalog manifests, so a
divergence fails the suite instead of quietly leaving two answers to what a
pinned digest is.

## Two planes, and why the pipeline has three steps

The tenant-private academic claim is **ingress only**. It is where bytes from the
public internet land, because it is the volume a downloading pod can write. The
**canonical** home for these public checkpoints is the Terraform-managed
reference plane at `/mnt/fs2-reference-data/data`, which is where the models look
and which has room to spare. Nothing public stays on the academic claim; only
PyRosetta remains tenant-private there.

**Staging** (`fetch_artifacts.py`) downloads into a task-owned private directory
on the ingress claim and publishes each file under its contracted name only after
the full stream has hashed to the contracted digest. Until then the bytes live
under a `.part` suffix. The resume path is the reason that suffix exists: the
smallest file here is 1.7 GiB and the claim had 35 GiB free.

**Promotion** (`promote_generations.py`) moves the verified files onto the
reference plane and publishes each directory as an immutable generation. Staging
proved the bytes that arrived over the network; promotion proves the bytes that
land on the canonical volume, which is a different claim and the one a consumer
depends on.

**Reclaim** (`reclaim_staging.py`) releases the ingress copy. It is a separate
entry point rather than a flag for two reasons: it runs as the account that owns
the ingress directories, which is not the account that owns the reference plane,
and it is the one irreversible step in the pipeline.

Promotion owns no promotion logic. Measuring the tree, building the marker,
writing it, and the rename that commits the generation all come from the reviewed
localization successor's `fs2_localization.localization`. A second implementation
of any of those would be a second answer to "what is this tree", and a
content-addressed generation exists precisely so there is one.

## Where the bytes land

```
<claim>/scientific-ingestion/fs2-proteina-complexa-r20260903/staging/<artifact_id>/         # ingress, released after promotion
<host_root>/scientific-localization/public/generations/<artifact_id>/sha256/<generation>/   # canonical, immutable, public
```

A generation is named by the digest of its own content, so different bytes are a
different path and an existing generation is never rewritten. The rename that
publishes it is the commit point: a consumer sees the whole verified tree or no
tree, never a half-written one.

That rename has to stay inside one filesystem, so a cross-plane promotion cannot
be a move. It copies into a reserved temporary directory *beside* where the tree
will be published, verifies the digest of every byte as it writes, and only then
renames. Crossing filesystems therefore costs a full copy and requires
`--allow-cross-filesystem-copy` to say so out loud; a same-filesystem promotion
stays a free rename and is the default.

Re-running promotion is safe and cheap in the way that matters: if the generation
already exists, the existing tree is re-measured rather than trusted — this tool
proved the bytes it staged and has proved nothing about bytes another writer
published under the same name — and the redundant copy is released instead of
being left on the plane.

Reclaim never deletes on the strength of a receipt. It opens the published
generation, checks the marker is byte-identical to the one promotion recorded and
that the file set and sizes still match, and only then releases the ingress
directory. A `.part` file is never removed: an unfinished download is worth more
than the space it occupies, because re-fetching it costs hours.

Each generation carries its own terminal marker, `.fs2-runtime-tree.json`,
written *inside* the tree before the rename. A consumer that mounts only the
generation can admit it from that marker instead of rehashing gigabytes at
start-up. The marker holds no timestamp, node, pod, run ID, or duration, so two
promotions of the same tree produce byte-identical markers and the marker's own
digest can be pinned by a handoff. When something happened belongs on the
staging receipt, which is an event; the marker is an identity.

The inventory digest excludes the marker, which is why writing the marker into
the tree does not change the tree's name.

## Running it

```bash
python render_ingestion_jobs.py stage \
  --name fs2-complexa-stage-"${RUN_ID}" --namespace "${NAMESPACE}" --run-id "${RUN_ID}" \
  --image "${PYTHON_IMAGE}" --claim "${CLAIM}" --config-map "${TOOLS}" \
  --staging-sub-path scientific-ingestion/fs2-proteina-complexa-r20260903/staging \
  --artifact-id complexa-protein --artifact-id complexa-ligand \
  --artifact-id complexa-ame --artifact-id rosettafold3-checkpoint \
  --node-selector storage.fs2.nebius/shared-cache=true \
  --continue-on-artifact-error | kubectl apply -f -

python render_ingestion_jobs.py promote \
  --name fs2-complexa-promote-"${RUN_ID}" --namespace "${NAMESPACE}" --run-id "${RUN_ID}" \
  --image "${PYTHON_IMAGE}" --claim "${CLAIM}" --config-map "${TOOLS}" \
  --verifier-config-map "${VERIFIER}" --host-root "${HOST_ROOT}" \
  --staging-sub-path scientific-ingestion/fs2-proteina-complexa-r20260903/staging \
  --artifact-id complexa-protein --artifact-id complexa-ligand \
  --artifact-id complexa-ame --artifact-id rosettafold3-checkpoint \
  --node-selector storage.fs2.nebius/shared-cache=true \
  --supplemental-group 65532 --supplemental-group 1000 --no-reclaim | kubectl apply -f -

python render_ingestion_jobs.py reclaim \
  --name fs2-complexa-reclaim-"${RUN_ID}" --namespace "${NAMESPACE}" --run-id "${RUN_ID}" \
  --promotion-run-id "${RUN_ID}" --image "${PYTHON_IMAGE}" --claim "${CLAIM}" \
  --config-map "${TOOLS}" --host-root "${HOST_ROOT}" \
  --staging-sub-path scientific-ingestion/fs2-proteina-complexa-r20260903/staging \
  --node-selector storage.fs2.nebius/shared-cache=true \
  --supplemental-group 65532 --supplemental-group 1000 --dry-run | kubectl apply -f -
```

`${VERIFIER}` is a ConfigMap holding the reviewed successor's `fs2_localization`
package (`__init__.py`, `localization.py`, `primitives.py`), delivered the same
way its own jobs deliver it. No project, region, registry, cluster, storage
class, node pool, or GPU type is written into the renderer; every one of them is
a flag.

Promotion must land on a node that can reach **both** planes: the ingress claim's
CSI driver is registered only on nodes carrying the storage capability label, and
the reference plane is a host mount. Selecting the storage capability plus a
non-GPU pool finds one; neither job requests a GPU or tolerates a GPU taint.

Three ownership properties shape these pods and are not negotiable:

- The ingress claim root is setgid and group-writable by GID 65532. The pods
  *join* that group and never set `fsGroup`. Kubernetes applies `fsGroup`
  ownership to the whole volume rather than to the sub-path a pod mounts, so
  setting it here would recursively rewrite the ownership of the tenant-private
  AlphaFold 3 and PyRosetta trees that share this claim.
- The reference plane is owned by UID 1000 and is not world-readable, so the
  copy step runs as that account and reaches the ingress claim through its group.
- Deleting on the ingress side requires owning it, so the reclaim step runs as
  UID 65532 and reads the reference plane through *its* group. Each step writes
  only to the side it owns, and each mounts the other side read-only.

`--continue-on-artifact-error` keeps one artifact's failure from stopping the
rest, and records it in the receipt. RF3 is staged last for the same reason: its
availability must never gate the six public Complexa files.

## Probes

Verifying a tree proves the bytes. The probes prove two further claims that a
digest alone does not:

- `probes/complexa_loader_probe.py` opens every checkpoint with `torch.load`
  inside the published Proteina-Complexa runtime image. A checkpoint can be
  byte-perfect and still fail to open.
- `probes/generation_visibility_probe.py` re-hashes every contracted file in
  full **from an H100 node**, read-only. Publication happens on a CPU node;
  that the consumer node class sees exactly those bytes through the same host
  root is a separate claim, and it is the one the models depend on.

Both refuse a mount without its terminal marker before reading anything, so a
regression cannot pass by loading from somewhere unexpected.

## What is published

Ingested live on 2026-09-03 into `project-e00rene` / `eu-north1`, cluster
`k8s-inference-h100`. All seven files were fetched from their pinned revisions,
verified on arrival, verified again as they were copied onto the reference plane,
and re-hashed a third time from an H100 node.

| Artifact | Generation (`fs2-tree-inventory/v2`) | Bytes |
| --- | --- | --- |
| `complexa-protein` | `eaaf891e89935b909f13bece3ff1e8c4a1ae43d0e2378b834e07ca74e2607536` | 7,034,391,160 |
| `complexa-ligand` | `61247c8dbf261307d708be53decfda69f21e73ff421556662366045c30d9cea5` | 5,890,739,041 |
| `complexa-ame` | `d38c622eaa0dad419f0ff0af72f36ab49299c533f5f56bbf08fa180e829afa5a` | 5,892,211,805 |
| `rosettafold3-checkpoint` | `d909fe65e86670b0a18a7494dd06811d301d0899e30778442e8ca6a343164bce` | 3,038,876,446 |

Each lives at
`/mnt/fs2-reference-data/data/scientific-localization/public/generations/<artifact_id>/sha256/<generation>`.
`evidence/binding-handoff.json` carries the exact mount sub-paths, marker
digests, and per-file identities a consumer should pin; the staging, promotion,
and reclaim receipts beside it record what happened and when.

The 21,856,218,452 ingress bytes were released after the generations were
confirmed, returning the academic claim to its pre-ingestion 35.6 GiB free with
the AlphaFold 3, PyRosetta, and localization trees untouched.

## Checks

`./run_checks.sh` compiles the modules, validates the shipped contract, and runs
the test suite. The transport tests run against a real loopback HTTP server that
speaks ranges, truncates streams, refuses ranges, and lies about content, because
every failure worth catching here is a transport failure and a mocked `urlopen`
cannot exhibit one.

The promotion tests need the reviewed localization successor. While that work is
on its own branch, point `FS2_LOCALIZATION_ADAPTERS` at the directory holding its
`localization.py` and `primitives.py`; once it lands here they find it on their
own and the skip disappears. They are never run against a vendored copy, because
a copy is the thing content addressing exists to prevent.

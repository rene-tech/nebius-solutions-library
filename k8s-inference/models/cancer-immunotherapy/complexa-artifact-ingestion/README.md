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

The per-file sizes and SHA-256 digests in `ingestion-contract.json` are consumed
verbatim from the pinned manifests in candidate `58e84e51`. Only those identities
were taken from it; its mutable final-consumer-path contract was not.

## Two steps, and why they are two

**Staging** (`fetch_artifacts.py`) downloads into a task-owned private directory
and publishes each file under its contracted name only after the full stream has
hashed to the contracted digest. Until then the bytes live under a `.part`
suffix. The resume path is the reason that suffix exists: the smallest file here
is 1.7 GiB, and the claim has no room to start any of them over.

**Promotion** (`promote_generations.py`) re-hashes the staged files *where they
now live*, then publishes the directory as an immutable generation. Staging
proved the bytes that arrived over the network; promotion proves the bytes that
are on the volume, which is the claim a consumer actually depends on.

Promotion owns no promotion logic. Measuring the tree, building the marker,
writing it, and the rename that commits the generation all come from the reviewed
localization successor's `fs2_localization.localization`. A second implementation
of any of those would be a second answer to "what is this tree", and a
content-addressed generation exists precisely so there is one.

## Where the bytes land

```
<claim>/scientific-ingestion/fs2-proteina-complexa-r20260903/staging/<artifact_id>/   # task-private, transient
<claim>/scientific-localization/public/generations/<artifact_id>/sha256/<generation>/ # immutable, public
```

A generation is named by the digest of its own content, so different bytes are a
different path and an existing generation is never rewritten. The rename that
publishes it is the commit point: a consumer sees the whole verified tree or no
tree, never a half-written one. Staging therefore has to sit on the same
filesystem as the generations root, because a cross-device rename is not atomic
and the successor refuses one.

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
kubectl -n "${NAMESPACE}" create configmap "${CONFIG_MAP}" \
  --from-file=fetch_artifacts.py --from-file=ingestion-contract.json

python render_ingestion_jobs.py \
  --name fs2-complexa-stage-"${RUN_ID}" --namespace "${NAMESPACE}" --run-id "${RUN_ID}" \
  --image "${PYTHON_IMAGE}" --claim "${CLAIM}" --config-map "${CONFIG_MAP}" \
  --staging-sub-path scientific-ingestion/fs2-proteina-complexa-r20260903/staging \
  --artifact-id complexa-protein --artifact-id complexa-ligand \
  --artifact-id complexa-ame --artifact-id rosettafold3-checkpoint \
  --node-selector storage.fs2.nebius/shared-cache=true \
  --continue-on-artifact-error | kubectl apply -f -
```

No project, region, registry, cluster, storage class, node pool, or GPU type is
written into the renderer; every one of them is a flag.

Two properties of the proof-of-concept claim shape the pod and are not
negotiable:

- The volume driver is registered only on nodes carrying the storage capability
  label, so staging selects on that label and tolerates no GPU taint. Staging is
  network bound and has no business occupying an H100. It asks for one CPU and
  2 GiB, which is what fits on the storage-capable CPU node and is ample for a
  workload whose ceiling is a single SHA-256 pass.
- The claim root is setgid and group-writable by GID 65532. The pod *joins* that
  group and never sets `fsGroup`. Kubernetes applies `fsGroup` ownership to the
  whole volume rather than to the sub-path a pod mounts, so setting it here
  would recursively rewrite the ownership of the tenant-private AlphaFold 3 and
  PyRosetta trees that share this claim.

`--continue-on-artifact-error` keeps one artifact's failure from stopping the
rest, and records it in the receipt. RF3 is staged last for the same reason: its
availability must never gate the six public Complexa files.

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

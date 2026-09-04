# Runtime artifact localization

A scientific runtime consumes a **directory**. Upstream may publish an archive
or one raw immutable file. Confusing archive and tree is what broke both primary
adapters: BoltzGen was given
`--moldir` pointing at a mount holding `mols.zip`, and Proteina-Complexa was
given `AF2_DIR` pointing at a mount holding `alphafold_params_2022-12-06.tar`.
Neither model opens an archive, so both failed at artifact load rather than at
startup, which is the worst place to find out.

This directory turns an upstream source into a tree and proves the result. A raw
file such as RFdiffusion `Base_ckpt.pt` uses `verified-copy`: the object itself
is placed under its immutable filename and is never described as an archive or
an extraction.

## Two identities, never one

| | Source provenance | Runtime-tree identity |
| --- | --- | --- |
| Answers | where the bytes came from | what the runtime will read |
| Recorded as | filename, byte size, SHA-256, source URI, upstream revision, license | entry count, total bytes, entry pattern, inventory digest |
| Computed from | the upstream object | the localized filesystem and path identity |
| Qualifies a mount | never | always |

`catalog/runtime/contracts/scientific-artifact-localization.json` declares both
for every artifact. Archive-backed entries use `archive`; raw entries use
`file`, and schema/runtime parsing require exactly one. The source digest and
generation digest are separate fields, so no caller can quietly substitute one
for the other.

Legacy flat archive trees use `fs2-flat-tree-inventory/v1`: a path-sorted JSON array of
`{"bytes", "crc32", "path"}` with sorted keys, no whitespace, and one trailing
newline. It contains no host path, mount root, timestamp, owner, or permission,
so the same tree hashes identically wherever it is localized. That is what makes
one verified identity safe to mount at several consumer paths.

## What fails closed

`verify_localized_tree` is the adapter preflight. It refuses a mount that:

- still contains its source archive, whether or not the tree is also there;
- holds fewer entries than the contract (partial tree);
- holds more entries or bytes than the contract (wrong tree);
- holds an entry outside the contracted path pattern;
- holds the right count and size but different bytes (identity mismatch);
- contains a symbolic link, a nested directory, or a non-regular entry;
- fails a content spot check against a bound probe entry.
- for a raw-file contract, does not contain exactly the contracted filename,
  byte count, and SHA-256 as its only content entry.

Extraction refuses traversal, absolute and nested member names, duplicates,
symlinks and non-regular members, and verifies the archive digest before writing
anything. A destination that is not empty is refused rather than merged into.

## A generation, not a path

A verified tree at a mutable path can change after it was verified. Every
runtime binding is therefore an **immutable generation**:

```
<prefix>/generations/<artifact_id>/sha256/<tree digest>
```

The digest names the directory, so different bytes are a different path and an
existing generation is never rewritten. Publication is a `rename` within one
filesystem, which is the commit point: a consumer sees either no generation or
the whole verified one. Staging happens in a private `.staging-` directory under
the same artifact root, so an interrupted run leaves a temporary directory the
next run reclaims by age, never a partial final tree. Promoting a generation
that already exists is a no-op, so restaging cannot destroy bytes a workload is
already mounting.

### One marker, sealed inside

Each generation carries `.fs2-runtime-tree.json`, written **before** the rename
so the same operation publishes the tree and its marker together. It is a single
flat document; `manifest_digest` is the SHA-256 of exactly its bytes, so a
consumer that hashes the file and a producer that computed the digest cannot
disagree. It carries no timestamp, node, duration or run ID, so two promotions
of one tree produce byte-identical markers and a handoff can pin the digest.
When something happened is on the staging **receipt**, which is an event; the
marker is an identity.

Every inventory algorithm excludes that one reserved name at the root, so
sealing the marker never moves a published digest. Admission reads the marker
and cross-checks the mount with a recursive file and directory count, which
walks the tree but reads no content: affordable on gigabytes, where rehashing is
not.

### Four identity algorithms

| Algorithm | Covers | Used by |
| --- | --- | --- |
| `fs2-flat-tree-inventory/v1` | flat files, CRC-32 | the four public trees |
| `fs2-tree-inventory/v2` | recursion and directories | a nested tree we stage |
| `fs2-tree-manifest/v1` | every file by SHA-256, every symlink by target | PyRosetta |
| `fs2-raw-file/v1` | one filename, byte count, and content SHA-256 | RFdiffusion `Base_ckpt.pt` |

The third is not ours. The academic-assets plane already identifies its
installed trees that way, so this module reproduces that algorithm exactly
rather than publishing a second, weaker name for bytes that already have one; a
cross-contract test runs both implementations over one fixture and requires the
same digest. The marker always names which algorithm produced its digest.
The fourth serializes `{algorithm, filename, bytes, sha256}` canonically, so the
runtime-tree generation remains distinct from the source object's digest while
still being reproducible from the mounted file alone.

## What exists and what does not

Six public generations are atomically published on the reference-data host
plane and qualified for read-only model use. The original five archive-backed
producer Jobs and terminal receipts are in
[`evidence/public-generation-publication-20260903.json`](evidence/public-generation-publication-20260903.json);
two-node marker admission and model-native loader results are in
[`evidence/public-generation-node-qualification-20260903.json`](evidence/public-generation-node-qualification-20260903.json).
The checked-in handoff joins those records and marks only these public entries
`qualified`:

| Artifact | Generation | Marker SHA-256 |
| --- | --- | --- |
| `alphafold2-params` | `cdbb7c7c475442712c73f8f8ea40b42fb5dd4fb5c1bf81fdb4642ca9e27f5ac4` | `da4d1936b6bb9c83ea4dc046cdc05131b0b2caf92cda71e086837f0f786d176f` |
| `alphafold2-params-bindcraft` | `9e25d394b1a7296f7705a5be794c5e29b853beb967835db088069f7cc007aa4f` | `25cad364aa28e5cf282a877d123ad938ea048a957ad8185307b5542c301406e0` |
| `boltzgen-inference-molecules` | `8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc` | `7f0e2c401abd73c1d4ff6deb6719e027db6ee9a75f7b7ed940b1e63ff54bbae4` |
| `colabdesign-mpnn-weights-soluble` | `54da6672d5677ab27bea0939bbbc591f8877484175a182736ca79af045d0f146` | `471cd4bcd0964be0c2f462668d01885e9db268e14fed04ebe02b693491690660` |
| `colabdesign-mpnn-weights-vanilla` | `2602ff1e01c8bdfd5773334e5724fcf0bdfecb3963100f05ad67ad6a5824ee4f` | `07ee17ecbc3c2a5e50327461f3cde311c35a7fad18f7d92e244e220e15329fc8` |
| `rfdiffusion-base-checkpoint` | `7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c` | `abd2a8127d0bd1b3cbd51d5ffc14a3351f805e15f593c8224ee94de57e3e4599` |

Both existing H100 nodes admitted the original five in-generation markers. The
immutable Proteina-Complexa, BindCraft and BoltzGen images then loaded their exact
mounts. The RFdiffusion r12 image separately admitted the raw-file marker on one
H100 node, re-hashed the entire checkpoint and deserialized it with PyTorch.
These were CPU-only artifact checks scheduled on H100 nodes, so they created no
GPU allocation or quota change. Source archives were absent from archive-backed
runtime mounts; the raw source file was truthfully present in its one-file mount.

The installed PyRosetta tree at the academic claim's
`pyrosetta-bindcraft/site-packages` predates this work. It is the promotion
*input*: a mutable install path, not an immutable generation, and its existence
is not evidence that the content-addressed generation has been published.

A binding becomes `promoted` only when a terminal promotion receipt exists for
that artifact, and `qualified` only when node admission and its model-native
probe pass. The six public entries now meet that rule. The tenant-private
PyRosetta generation does not: it remains `rendered`, and the aggregate
`generations_published` field therefore remains false.

## Where the trees live

Storage authority is chosen **per artifact**, not per run:

- **Public** artifacts (BoltzGen molecules, both AlphaFold2 parameter trees, both
  ColabDesign MPNN weight sets, and RFdiffusion `Base_ckpt.pt`) live on the
  Terraform-managed public
  model-artifact plane: the host root `/mnt/fs2-reference-data/data`, mounted on
  every node labelled `storage.fs2.nebius/reference-data=true`, the H100s
  included. A consumer reaches it by node label, not by claim.
- **PyRosetta** is licensed and tenant-scoped, so its generation lives only on
  the claim `fs2-academic-poc/academic-assets-runtime-rwx` under
  `scientific-localization/private`.

The two planes are addressed differently and the handoff keeps that difference
rather than flattening it: a host plane entry carries `host_root`, the resolved
`host_path` and the node selector that reaches it, and a claim entry carries
`namespace` and `claim`. A marker that named a claim for a host directory, or a
host root for a claim, would describe a location nobody can mount, so
`generation_marker` refuses each mismatch. The object store that the scientific
artifact store provisions is the **result** store; it is not this plane, and a
runtime mounts a filesystem.

Public bytes are deliberately kept off the academic claim: that volume exists
for one licence chain, and filling it with general artifacts would freeze it
into a role it was never provisioned for. **Neither is a global cache.**

PyRosetta is promoted from the tree the academic plane installed, and that
install path is recorded as `promoted_from` with `runtime_bindable: false`. It is
where the bytes were built, not a name that can only ever mean those bytes.
Promotion shares the data by hard link rather than copying it, because the claim
has gigabytes of headroom and the tree is 3.2 GB; the receipt reports linked and
copied bytes separately so the claim is provable rather than assumed.

That only works from one mount. Two bind mounts of the same volume are separate
mount namespaces, and `os.link` across them returns `EXDEV` even though the
bytes share a filesystem, so mounting the source read-only alongside the
destination — which looks like the safer design — would quietly write a second
full copy. The claim is therefore mounted once and both paths are addressed
beneath it, a copy is refused unless `--allow-copy` budgets for it, and a
promotion whose source and destination are on different claims is refused
outright rather than falling back.

The source keeps its protection from the tool instead of from a read-only
mount: the promotion only ever reads it, refuses any writable source file, and
never chmods a shared inode, since `chmod` follows the inode and would rewrite
the tree it was promoting from.

Two properties of the academic claim decide how a staging job must be written:

- The `mounted-fs-path.csi.nebius.ai` driver is registered only on nodes
  labelled `storage.fs2.nebius/shared-cache=true`, which the reference-data pool
  is not. Selecting on that storage capability, and tolerating no GPU taint,
  places staging on a CPU node that can actually mount the volume and cannot
  land on a GPU.
- The claim root is setgid and group-writable by GID 65532. Writing means
  joining that group, never setting `fsGroup`: Kubernetes applies fsGroup
  ownership to the whole volume rather than the sub-path a pod mounts, so
  `fsGroup` here would recursively rewrite the ownership of PyRosetta and
  AlphaFold 3.

`--fs-group` is therefore refused on a claim-backed run, and the supplemental
group is applied by default rather than left to the operator to remember.

## Running it

Each plane has its own owner, and the renderer defaults to the right one rather
than making the operator remember it.

Public artifacts stage onto the host plane. The rendered Job mounts a `hostPath`,
tells the localizer which plane it is writing to, and runs as `1000:1000`, which
owns `/mnt/fs2-reference-data/data`. Only labelled nodes mount that root, so a
render without the label is refused rather than scheduled somewhere the
directory is absent:

```bash
python render_localization_jobs.py stage \
  --artifact-id boltzgen-inference-molecules --artifact-id alphafold2-params \
  --namespace fs2-models --run-id "${RUN_ID}" \
  --image "${REGISTRY}/boltzgen@sha256:..." --python /opt/venv/bin/python \
  --config-map "${CONFIG_MAP}" --claim "${ACADEMIC_CLAIM}" \
  --plane host-path --host-root /mnt/fs2-reference-data/data \
  --tree-prefix scientific-localization/public \
  --node-selector storage.fs2.nebius/reference-data=true | kubectl apply -f -

# A raw file uses the same Job and generation layout. The renderer emits
# --fetch-file-to/--file, never archive flags or extraction semantics.
python render_localization_jobs.py stage \
  --artifact-id rfdiffusion-base-checkpoint \
  --namespace fs2-models --run-id "${RUN_ID}" \
  --image "${REGISTRY}/rfdiffusion@sha256:3f18dd9c4aac4fec472e2b0419988676eb938ccf2801760226fa822a6738a1b8" \
  --python python --config-map "${CONFIG_MAP}" --claim "${ACADEMIC_CLAIM}" \
  --plane host-path --host-root /mnt/fs2-reference-data/data \
  --tree-prefix scientific-localization/public \
  --node-selector storage.fs2.nebius/reference-data=true | kubectl apply -f -

python render_localization_jobs.py qualify \
  --artifact-id boltzgen-inference-molecules \
  --namespace fs2-models --run-id "${RUN_ID}" --model-id boltzgen \
  --image "${REGISTRY}/boltzgen@sha256:..." --python /opt/venv/bin/python \
  --config-map "${PROBE_CONFIG_MAP}" --claim "${ACADEMIC_CLAIM}" --queue inference-models \
  --plane host-path --tree-prefix scientific-localization/public \
  --node-selector storage.fs2.nebius/reference-data=true \
  --probe-file probes/boltzgen_moldir_probe.py \
  --probe=/opt/venv/bin/python \
  --probe=/opt/fs2-localization/fs2_localization/boltzgen_moldir_probe.py \
  --probe=--moldir --probe=/opt/fs2/artifacts/boltzgen-inference-molecules | kubectl apply -f -
```

A tree another plane installed is promoted rather than staged. The Job mounts
the claim **once**, addresses the installed tree and the generation root beneath
that single mount, shares the bytes by hard link, verifies the result under the
producer's own algorithm, seals the marker inside and publishes by rename:

```bash
python render_localization_jobs.py promote \
  --artifact-id bindcraft-pyrosetta-installed-tree \
  --namespace fs2-academic-poc \
  --claim academic-assets-runtime-rwx \
  --source-claim academic-assets-runtime-rwx \
  --config-map "${CONFIG_MAP}" --image "${REGISTRY}/bindcraft@sha256:..." \
  --node-selector storage.fs2.nebius/shared-cache=true | kubectl apply -f -
```

Ownership is a property of a volume, not a rule about claims, so the renderer
defaults it for exactly one claim and refuses to guess for any other.

- `fs2-academic-poc/academic-assets-runtime-rwx` joins GID 65532 as a
  supplemental group by default, because that claim's root is setgid and
  group-writable by it, and `--fs-group` is refused there outright: Kubernetes
  applies fsGroup to the whole volume rather than the sub-path a pod mounts, so
  on a claim that also holds PyRosetta and AlphaFold 3 it would recursively
  rewrite their ownership.
- Any other claim must be told what it needs, with `--supplemental-group` to
  join the group that owns it or `--fs-group` if the workload owns the volume
  outright. Applying the academic claim's GID to a customer PVC would either
  fail to write or write files that volume's owner never asked for.

No prefix is mounted as a `subPath`. The prefix is carried in the paths the tool
writes and created by the init container, because on the first run for a new
prefix the directory does not exist yet and a `subPath` that does not exist
cannot be mounted — mounting one would deadlock the run that exists to create it.

## What admission pins

`qualify` renders a `marker` step that names, and the localizer checks, every
one of: the generation, the sub-path, the marker's own `manifest_digest`, the
plane kind and its host root or namespace and claim, the visibility, and the
identity algorithm. Bytes that are right in the wrong place, under the wrong
licence, or measured by the wrong algorithm are still wrong. The marker file
must also be the canonical serialization of what it parses to, so a padded or
reordered document cannot carry a digest for bytes a reader never sees.

Admission then counts the mount recursively — files, directories and symlinks —
without reading content. A tree identified by `fs2-tree-manifest/v1` may hold
symlinks, because that algorithm covers them by target; one identified by a file
inventory may not. A declared count that does not match the mount is refused.

## What happens when publication fails

Every terminal state of a run leaves the same receipt: a marker that cannot be
written, a link that cannot be made, a rename that cannot commit, and a
generation that already exists but does not verify all produce a `rejected`
receipt naming the reason, not a traceback. Staging this run owns is removed on
the way out, because the claim it sits on has gigabytes of headroom rather than
tens of them.

An existing generation is never trusted on the strength of its name. This tool
proved the bytes it staged and nothing about bytes someone else published under
the same digest, so a reused target is verified in place, its marker digest
compared, and only then reported as a success.

The receipt reports `bytes_linked` and `bytes_copied`, so "no second copy" is a
measured claim. Promotion fails closed rather than publishing if the source is
writable, if the linked tree does not reproduce the contracted digest, or if the
staging directory is not on the filesystem it publishes into.

No project, region, registry, cluster, storage class, or GPU pool is hardcoded.
The renderer refuses an image reference that is not an immutable digest, and the
artifact identities come only from the checked-in contract.

The staging and qualification workloads run the **same verifier module** the
control plane runs, delivered as a small package through a ConfigMap, so an
in-cluster receipt and a control-plane preflight cannot disagree.

## Probes

Verifying the tree proves the mount. The probes prove the model actually reads
it, which is a different claim and the one that was silently false before:

- `probes/boltzgen_moldir_probe.py` loads real Chemical Component Dictionary
  entries from `--moldir` with the runtime's own loader, sampling one- to
  five-character codes because CCD codes are not all three characters.
- `probes/proteina_af2dir_probe.py` calls ColabDesign's own
  `get_model_haiku_params` against `AF2_DIR`, taking the same resolution path
  and the same multimer parameter set an evaluate stage selects.
- `probes/rfdiffusion_checkpoint_probe.py` hashes the exact raw-file generation,
  verifies its marker, and deserializes `Base_ckpt.pt` with the RFdiffusion
  image's PyTorch runtime.

The archive-backed probes refuse a directory that still contains an archive
before doing anything else, so a regression cannot pass by loading from
somewhere unexpected.

Every probe reports `node_digest` rather than the node. The downward API gives a
pod the opaque Nebius instance ID, and these receipts are checked into a public
repository, so the raw value may not appear here; `tests/test_public_export.py`
enforces that. A truncated SHA-256 of the node name still tells a reader whether
two receipts came from the same machine, which is all this field was ever for.
The instance ID stays in the private run record.

### RFdiffusion raw-file live evidence

On 2026-09-03, task-owned Job `fs2-localize-stage-sfraw-r1` in namespace
`fs2-models`, cluster context `k8s-inference-h100`, project `project-e00rene`,
region `eu-north1`, ran the immutable RFdiffusion r12 image
`sha256:3f18dd9c4aac4fec472e2b0419988676eb938ccf2801760226fa822a6738a1b8`.
It fetched and verified the exact 483,616,107-byte `Base_ckpt.pt`, copied one
file and 483,616,107 bytes (zero links), and atomically published generation
`7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c`
with marker `abd2a8127d0bd1b3cbd51d5ffc14a3351f805e15f593c8224ee94de57e3e4599`
in 36.704 seconds.

Task-owned Job `fs2-localize-qualify-rfdiffusion-sfraw-probe-r1` then ran on a
regular capacity-block node from the `h100-reserved-8x` pool, selected as
`nvidia-h100-sxm5-80gb` with reference-data storage. It requested no GPU. The
init container admitted the exact in-generation marker; the model-side probe
re-hashed the full file and deserialized `config_dict`, `final_state_dict` and
`model_state_dict` with PyTorch 2.3.0 in 1.893 seconds. Its privacy-safe node
identity is `5e3ad019c6df4d8b`. The stage receipt, marker admission and probe
report are in `evidence/`.

Both Jobs and both ConfigMaps were deleted after evidence capture. The immutable
public generation remains intentionally available for RFdiffusion consumers.
No GPU quota, shared service, deployment or B300 resource was changed; the
existing Qwen and Cosmos deployment image digests were unchanged after cleanup.

## Binding handoff

`render_localization_jobs.py handoff` emits
`evidence/binding-handoff.json`: per artifact, the plane and sub-path to mount,
the paths each consumer reads, the archive provenance, the tree identity a
preflight will require, and the marker digest a consumer pins. Every value is
derived from the contract by the same module that writes the marker, so a
consumer following the handoff and a control-plane preflight cannot disagree.

The renderer itself still emits `rendered` only: it cannot claim readiness it
did not establish. The checked-in handoff is promoted beyond that generated
baseline only by joining the exact terminal receipts and probes above. Its
evidence block deliberately keeps the still-pending private PyRosetta generation
separate from the six qualified public generations.

## The BindCraft AlphaFold2 mount is a second tree, not the same one

The published BindCraft image admits `/models/alphafold2` only through
`artifact_gate.verify_manifest`, which reads `FS2_ARTIFACT_MANIFEST` and checks
`artifact_kind` and `source_revision` against the runtime. Upstream publishes no
such document, so **the sixteen-file upstream tree does not run that image**.

`alphafold2-params-bindcraft` is therefore a separate identity: same archive
provenance, seventeen-entry tree, its own inventory digest, and the manifest
declared as a generated entry bound by its own digest. Proteina-Complexa keeps
reading the sixteen-entry `alphafold2-params` tree, because ColabDesign resolves
parameter files by name in `AF2_DIR` and an admission document is not part of
that contract.

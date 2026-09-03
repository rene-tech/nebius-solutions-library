# Runtime artifact localization

A scientific runtime consumes a **directory**. Upstream publishes an **archive**.
Confusing the two is what broke both primary adapters: BoltzGen was given
`--moldir` pointing at a mount holding `mols.zip`, and Proteina-Complexa was
given `AF2_DIR` pointing at a mount holding `alphafold_params_2022-12-06.tar`.
Neither model opens an archive, so both failed at artifact load rather than at
startup, which is the worst place to find out.

This directory turns the archive into the tree, and proves the result.

## Two identities, never one

| | Archive provenance | Extracted-tree identity |
| --- | --- | --- |
| Answers | where the bytes came from | what the runtime will read |
| Recorded as | filename, byte size, SHA-256, source URI, upstream revision, license | entry count, total bytes, entry pattern, inventory digest |
| Computed from | the compressed object | the localized filesystem |
| Qualifies a mount | never | always |

`catalog/runtime/contracts/scientific-artifact-localization.json` declares both
for every artifact. The two digests are separate fields, and `RuntimeTreeBinding`
rejects them being equal, so no caller can quietly substitute one for the other.

The tree digest is `fs2-flat-tree-inventory/v1`: a path-sorted JSON array of
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

### Three identity algorithms

| Algorithm | Covers | Used by |
| --- | --- | --- |
| `fs2-flat-tree-inventory/v1` | flat files, CRC-32 | the four public trees |
| `fs2-tree-inventory/v2` | recursion and directories | a nested tree we stage |
| `fs2-tree-manifest/v1` | every file by SHA-256, every symlink by target | PyRosetta |

The third is not ours. The academic-assets plane already identifies its
installed trees that way, so this module reproduces that algorithm exactly
rather than publishing a second, weaker name for bytes that already have one; a
cross-contract test runs both implementations over one fixture and requires the
same digest. The marker always names which algorithm produced its digest.

## What exists and what does not

The volumes exist. The generations do not, yet.

`evidence/binding-handoff.json` is **generated from the contract**: every path,
tree identity and marker digest in it is derived, and none of it has been
published. No staging or promotion Job has run for any path it names, so nothing
is present at any `sub_path` in that document. It records this itself, with
`evidence.state: "rendered"`, empty `promotion_receipts` and `node_probes`, and a
`plane_state` that describes the volume kept separate from a `binding_state` that
describes the generation.

The installed PyRosetta tree at the academic claim's
`pyrosetta-bindcraft/site-packages` predates this work. It is the promotion
*input*: a mutable install path, not an immutable generation, and its existence
is not evidence that the content-addressed generation has been published.

A binding becomes `promoted` only when a terminal promotion receipt exists for
that artifact, and `qualified` only when a node probe has admitted the mount.
Deploying those Jobs and recording that evidence is deliberately separate work.

## Where the trees will live

Storage authority is chosen **per artifact**, not per run:

- **Public** artifacts (BoltzGen molecules, both AlphaFold2 parameter trees, both
  ColabDesign MPNN weight sets) live on the Terraform-managed public
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
copied bytes separately so the claim is provable rather than assumed. A hard
link makes `chmod` follow the inode, so a writable source is refused at both the
linking step and the sealing step instead of being silently rewritten.

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
the installed tree read-only at `/source` and this tool's own generation root at
`/trees`, shares the bytes by hard link, verifies the result under the
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

Both refuse a directory that still contains an archive before doing anything
else, so a regression cannot pass by loading from somewhere unexpected.

Every probe reports `node_digest` rather than the node. The downward API gives a
pod the opaque Nebius instance ID, and these receipts are checked into a public
repository, so the raw value may not appear here; `tests/test_public_export.py`
enforces that. A truncated SHA-256 of the node name still tells a reader whether
two receipts came from the same machine, which is all this field was ever for.
The instance ID stays in the private run record.

## Binding handoff

`render_localization_jobs.py handoff` emits
`evidence/binding-handoff.json`: per artifact, the plane and sub-path to mount,
the paths each consumer reads, the archive provenance, the tree identity a
preflight will require, and the marker digest a consumer pins. Every value is
derived from the contract by the same module that writes the marker, so a
consumer following the handoff and a control-plane preflight cannot disagree.

Being derived is exactly why it is not evidence of publication. The document
says so in its own `evidence` block; see **What exists and what does not**.

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

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

## Where the trees live

For the proof of concept the trees are staged into the Terraform-owned
tenant-private claim `fs2-academic-poc/academic-assets-runtime-rwx`, alongside
the PyRosetta and AlphaFold 3 assets, under the sub-path
`scientific-localization/public/<artifact_id>`. The sub-path keeps these public
artifacts separable from the tenant-private ones. **This is not a global cache.**

Two properties of that claim decide how a staging job must be written:

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

`--fs-group` therefore defaults to unset and belongs only to a claim the
workload owns outright.

## Running it

Stage into a regional shared volume, then prove the trees on the node that will
use them:

```bash
python render_localization_jobs.py stage \
  --artifact-id boltzgen-inference-molecules --artifact-id alphafold2-params \
  --namespace fs2-models --run-id "${RUN_ID}" \
  --image "${REGISTRY}/boltzgen@sha256:..." --python /opt/venv/bin/python \
  --claim "${CLAIM}" --config-map "${CONFIG_MAP}" \
  --tree-prefix scientific-localization/public \
  --run-as-user 65532 --run-as-group 65532 --supplemental-group 65532 \
  --node-selector storage.fs2.nebius/shared-cache=true \
  --node-selector capacity.fs2.nebius/pool=system | kubectl apply -f -

python render_localization_jobs.py qualify \
  --artifact-id boltzgen-inference-molecules \
  --namespace fs2-models --run-id "${RUN_ID}" --model-id boltzgen \
  --image "${REGISTRY}/boltzgen@sha256:..." --python /opt/venv/bin/python \
  --claim "${CLAIM}" --config-map "${PROBE_CONFIG_MAP}" --queue inference-models \
  --probe-file probes/boltzgen_moldir_probe.py \
  --probe=/opt/venv/bin/python \
  --probe=/opt/fs2-localization/fs2_localization/boltzgen_moldir_probe.py \
  --probe=--moldir --probe=/opt/fs2/artifacts/boltzgen-inference-molecules | kubectl apply -f -
```

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
`evidence/binding-handoff.json`: per artifact, the claim and sub-path to mount,
the paths each consumer reads, the archive provenance, and the tree identity a
preflight will require. Every value is derived from the contract, so a consumer
following the handoff and a control-plane preflight cannot disagree.

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


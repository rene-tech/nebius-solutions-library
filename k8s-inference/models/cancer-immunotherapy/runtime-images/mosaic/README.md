# Mosaic scientific-batch runtime

This directory builds, publishes and qualifies one runtime identity: the
Escalante Bio `mosaic` protein design framework driving the pinned Boltz-2 plus
ProteinMPNN minibinder recipe behind the canonical `mosaic-batch` wrapper
contract. It was split out of the combined Complexa/BoltzGen/mosaic image work
so Mosaic proceeds independently.

Mosaic ships no weights of its own. It is a JAX framework that reimplements
other protein property models behind one interface, so the image carries code
only and every component checkpoint arrives from the external artifact plane.

## Exact identities

| Thing | Identity |
|---|---|
| Upstream | `escalante-bio/mosaic` at `70fec525423f5f87156a1a957b4a4048f9f8e676`, MIT |
| Source archive | SHA-256 `41d30b2a…f2d1a`, 37,978,589 bytes |
| Adapter contract | candidate commit `6551d870`, `bin/mosaic-batch` SHA-256 `edd017c6…d046e7`, `recipe.json` SHA-256 `cbfc7a88…8e0fe` |
| Boltz-2 checkpoint | `boltz2_conf.ckpt`, SHA-256 `090e82ac…1428e1`, 2,286,561,469 bytes |
| Boltz-2 molecules | `mols.tar`, SHA-256 `39e076d9…7d1fd7`, 1,855,662,080 bytes, consumed extracted |
| ProteinMPNN | `v_48_020.pt`, SHA-256 `c9cb4a67…42f5bd`, 6,681,301 bytes |

`image-lock.json` is the machine-readable form and the only place a digest is
recorded. The upstream repository publishes no git tags, so the commit is the
version.

## What the image is, and is not

The build installs the pinned source with its locked dependency set, then
deletes the three ProteinMPNN checkpoints upstream ships inside the package
tree before installation. The published image's own probe reports
`embedded_weights: []`. Boltz-2, its molecule tree and ProteinMPNN are mounted,
never baked.

`build_mosaic.py` never vendors the adapter contract. It materialises
`bin/mosaic-batch` and `recipe.json` straight out of commit `6551d870`,
byte-verifies both against the pinned SHA-256 identities, and only then lets
them into the image. Publication is non-overwriting: an existing target tag
aborts the build, so a correction always becomes a new tag and the superseded
digest stays readable in the lock.

## Runtime contract

The image exposes exactly the two canonical subcommands and nothing else:

```text
/opt/fs2/bin/mosaic-batch run-shard --request R --input-manifest M --recipe /opt/fs2/mosaic/recipe.json --recipe-sha256 SHA --shard-index N --seed S --output DIR
/opt/fs2/bin/mosaic-batch aggregate --request R --input-manifest M --shards DIR --expected-shards N --staging-manifest TMP --output-manifest OUT --atomic-rename
```

`run-shard` verifies the recipe digest and the shard seed against the request,
resolves the target FASTA by opaque artifact ID and checks its size and
SHA-256, verifies both external checkpoints by SHA-256, then runs the recipe:
Boltz-2 binder features with empty MSAs for both chains, the four weighted
objective terms, and `simplex_APGM` at the recipe's step size and momentum. It
writes the shard result, the candidate metrics and the binder structure.

`aggregate` content-addresses every shard output, writes the aggregate record,
commits the manifest through a staging file and one atomic rename, and emits an
artifact index. It fails closed unless `FS2_RUNTIME_IMAGE_DIGEST` supplies the
admitted image digest.

Every cache default resolves under `/tmp`, because the canonical Job runs with
`readOnlyRootFilesystem` and mounts only the request ConfigMap, `/workspace`
and a `/tmp` emptyDir.

## Binder structure serialisation

Mosaic optimises a relaxed 20-way distribution over sequence space, so the
Gemmi structure the Boltz-2 writer returns names every binder residue `UNK`.
Writing that out verbatim discards the design: the canonical adapter validator
cannot recover a sequence from it, and a structural validator counts zero
standard residues. `_binder_pdb` therefore writes the designed one-letter
identities into the residue-name column in residue order and refuses to emit
anything whose residue count disagrees with the sequence the same shard
reported. `adapter-v2` and `adapter-v4` are retained in the lock as
semantic-validation-failed precisely because they lacked this.

## Contract defects reported upstream

Four defects were found while qualifying against the canonical adapter. All are
recorded in `image-lock.json` with an exact location and a resolution; three of
them need an adapter change and are reported back to the primary adapter owner:

1. `recipe.json` and `artifact-lock.json` pin a stale Boltz-2 artifact-manifest
   digest. The referenced file hashes differently, identically at `6551d870`
   and at main, so any consumer verifying that pointer fails closed. The
   checkpoint and content digests themselves are correct.
2. The rendered aggregate Job carries no env and no argv flag that could supply
   the admitted image digest, yet the canonical validator requires the
   aggregate to carry it. A strictly canonical plan can never validate.
3. The rendered Job mounts no artifact plane, so a strictly canonical Job gives
   the runtime no checkpoints at all.
4. The rendered Job sets `readOnlyRootFilesystem` with no writable cache
   location, which aborts Boltz at import in its numba-cached MSA featuriser.
   This one is resolved entirely inside the image and needs no adapter change.

## Qualification

`qualification/render_plan.py` imports the canonical adapter from its pinned
commit and renders the real plan, so the argv that reaches the GPU is the argv
the canonical renderer produces. `qualification/submit_plan.py` applies that
plan, rewriting only the cluster wiring the scientific batch plane has not
provisioned yet and recording every deviation in its receipt.

Artifact delivery is explicitly transitional. The canonical regional artifact
plane does not publish a Mosaic root yet, so qualification reads a task-scoped
copy on the preserved qualification claim. The runtime resolves everything by
SHA-256, so it binds to the canonical plane unchanged once that plane exists.

Production controller Jobs keep immutable model assets and tenant request
bytes on separate mounts. `FS2_ARTIFACT_ROOT` names the read-only model plane;
`FS2_INPUT_ARTIFACT_ROOT` names the stage workspace containing
`inputs/<artifact-id>`. Direct historical Jobs that omit the latter retain the
single-root behavior, while an explicitly supplied input root never falls back
to the model plane.

`evidence/` holds the live H100 receipt. Run the offline suite with:

```bash
./run_checks.sh
```

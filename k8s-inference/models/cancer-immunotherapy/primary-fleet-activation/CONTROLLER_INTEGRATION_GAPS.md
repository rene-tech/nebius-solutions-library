# Mosaic and RFdiffusion controller integration gap

The production controller compilers and semantic collectors use the public
identities `mosaic` and `rfdiffusion`. They are registered globally, but both
profiles remain `candidate-unqualified` with `route_exposed: false`. The
activation fragments still pin older accepted images whose request-artifact
path contract is incompatible with the companion's separate writable workspace
and read-only model mounts. Split-root successors now exist, but must first be
integrated into the image locks and aggregate identities and then pass a live
controller semantic run.

Exact published successor identities:

| Runtime | Immutable successor digest |
| --- | --- |
| Mosaic v6 | `sha256:853cb34b36e940303c126e11e9e66c7643efa15c4ab48861c73013018e477a92` |
| RFdiffusion r13 | `sha256:f31902e0fbece8e7f823b36e47b79ec02fe0bc545a44131188f9194f13711f19` |

## Exact mismatch

The companion materializes request artifacts beneath the only writable stage
workspace, while model artifacts remain at their immutable localization mounts:

| Runtime | Request artifact path produced by the controller | Immutable model artifact path |
| --- | --- | --- |
| Mosaic | `${FS2_INPUT_ARTIFACT_ROOT}/inputs/<artifact UUID>` | `${FS2_ARTIFACT_ROOT}/mosaic/boltz/{boltz2_conf.ckpt,mols}` and `${FS2_ARTIFACT_ROOT}/mosaic/proteinmpnn` under `/opt/fs2/artifacts` |
| RFdiffusion motif | `${FS2_INPUT_ARTIFACT_ROOT}/inputs/<artifact UUID>` where `FS2_INPUT_ARTIFACT_ROOT=<workspace>/shards/<index>` | `/opt/fs2/artifacts/rfdiffusion-base-checkpoint/Base_ckpt.pt` |

The fragment-pinned Mosaic image resolves both target inputs and model weights
from `FS2_ARTIFACT_ROOT`. Pointing that variable at `/opt/fs2/artifacts` hides
the request FASTA; pointing it at the writable workspace hides every model
weight. The fragment-pinned RFdiffusion image similarly resolves both the
checkpoint and motif PDB relative to the single `--artifact-root`.
Backbone-only generation does not dereference the design-constraint artifact,
but motif scaffolding does.

The controller already freezes `FS2_INPUT_ARTIFACT_ROOT` at the paths above so
the successor image has no additional caller-controlled path surface. For
RFdiffusion it also passes the identical value as the explicit
`--input-artifact-root` argv operand; the environment is not a fallback for an
omitted command contract. The successor behavior contract is:

1. Mosaic `_target_sequence` resolves only the verified target pointer beneath
   `${FS2_INPUT_ARTIFACT_ROOT}/inputs/<artifact UUID>`. It continues to resolve
   Boltz-2, molecule, and ProteinMPNN assets only beneath
   `FS2_ARTIFACT_ROOT=/opt/fs2/artifacts`.
2. RFdiffusion resolves only the verified non-checkpoint manifest pointer
   beneath
   `--input-artifact-root=${FS2_INPUT_ARTIFACT_ROOT}=<workspace>/shards/<index>`.
   It continues to resolve `artifact.rfdiffusion.base-ckpt` only beneath the
   distinct pinned
   `--artifact-root=/opt/fs2/artifacts/rfdiffusion-base-checkpoint`.
3. Both implementations reject absolute paths, `..`, symlinks, digest/size
   drift, and any input manifest entry not selected by the controller.

Mosaic aggregate execution has a separate exact environment contract. The
accepted runtime reads `FS2_RUNTIME_IMAGE_DIGEST`; the controller projects that
same key and its collector verifies the emitted aggregate against it. No alias
such as `FS2_EXPECTED_RUNTIME_IMAGE_DIGEST` is valid.

## Promotion gate

Integrate the immutable successor images above into their image locks and
activation execution identities, then run all of the following before changing
either route flag:

- Mosaic target-FASTA design and aggregate through the real companion;
- RFdiffusion backbone and motif runs through the real companion;
- model-artifact localization preflight for every frozen mount;
- nonterminal deterministic zstd handoff and real overlay materialization;
- terminal semantic collection using the exact registered collector/validator
  IDs (`mosaic-boltz2-proteinmpnn-v1` and `rfdiffusion-v1-1-0`);
- traversal, symlink, replacement-race, oversize, nonzero-exit, and stale
  completion-identity negatives.

Until those image-level runs pass, the current controller code is integration
ready but deliberately non-dispatchable. Existing H100 semantic evidence is not
evidence that the new split-root controller path ran.

## Deferred combined identity refresh

The activation fragments intentionally are not rehashed on this isolated model
branch. Their `fs2-path-set-sha256-v1` recipe closure must be expanded after the
shared companion, execution-map, registry, workspace helper, and all four
primary model adapters are merged. At minimum the final closure must name:

- `components/control-plane/src/fs2_serve/scientific_batch/companion.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/execution.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/production_registry.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/staged_workspace.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/verified_input.py`;
- the exact model-specific compiler/collector module.

Then refresh all affected runtime recipe and aggregate execution identities once
from the combined tree. Until that serialized refresh, the primary activation
recipe-digest gate is expected to reject the branch; bypassing or independently
rehashing one fragment would assert an incomplete production recipe.

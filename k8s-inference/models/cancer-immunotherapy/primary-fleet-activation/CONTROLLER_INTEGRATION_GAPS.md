# Mosaic and RFdiffusion controller integration gap

The production controller compilers and semantic collectors use the public
identities `mosaic` and `rfdiffusion`. They are registered globally, and both
profiles use the schema-supported `active` bridge with `route_exposed: true`.
The activation fragments, image locks, and serialized profiles pin
the split-root successor images. Each exact successor passed its bounded
image-level H100 workflow with independent request and model roots. Neither has
yet passed the public platform controller submission, companion collection,
and scheduler-admission sequence required for `qualified` state.

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

The retired Mosaic image resolved both target inputs and model weights from
`FS2_ARTIFACT_ROOT`: pointing it at `/opt/fs2/artifacts` hid the request FASTA,
while pointing it at the writable workspace hid every model weight. The
retired RFdiffusion image likewise resolved both checkpoint and motif PDB
relative to one `--artifact-root`. Those predecessor constraints explain why
their semantic evidence was never transferred to the successors.

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

## Qualification gate

The immutable successors above are integrated and their direct H100 workflow
checks passed. Run all of the following through the public platform before
promoting either profile from `active` to `qualified`:

- Mosaic target-FASTA design and aggregate through the real companion;
- RFdiffusion backbone and motif runs through the real companion;
- model-artifact localization preflight for every frozen mount;
- nonterminal deterministic zstd handoff and real overlay materialization;
- terminal semantic collection using the exact registered collector/validator
  IDs (`mosaic-boltz2-proteinmpnn-v1` and `rfdiffusion-v1-1-0`);
- traversal, symlink, replacement-race, oversize, nonzero-exit, and stale
  completion-identity negatives.

Until those platform runs pass, the serialized controller entries remain
dispatchable in `active` state but are not represented as fully `qualified`.
Their public-completion and scheduler-eligibility receipt fields remain null.
The exact successor H100 evidence proves the split-root runtime behavior, but
is not a public service completion or Kueue admission receipt.

## Serialized identity state

The active recipe closure includes the shared companion, execution-map,
registry, workspace helper, and exact model compiler/collector. In particular
it names:

- `components/control-plane/src/fs2_serve/scientific_batch/companion.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/execution.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/production_registry.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/staged_workspace.py`;
- `components/control-plane/src/fs2_serve/scientific_batch/adapters/verified_input.py`;
- the exact model-specific compiler/collector module.

Runtime and workload recipe digests, model-artifact manifest digests, and
execution identities are derived from the combined tree. The H100 semantic
receipt is exact; only public-completion and scheduler-eligibility receipts
remain null until the public acceptance run supplies them.

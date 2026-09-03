# BindCraft, RFdiffusion, and ProteinMPNN runtime images

This package builds four deliberately distinct `linux/amd64` runtime images on
the digest-pinned PyTorch 2.3.0 / CUDA 12.1 base in `image-lock.json`. BindCraft
uses the hash-pinned official `jaxlib 0.4.28+cuda12.cudnn89` wheel so it consumes
that base CUDA/cuDNN stack instead of silently downloading a second toolkit:

| Image ID | Exact source | Relationship | Default-image artifacts |
|---|---|---|---|
| `bindcraft-academic` | BindCraft `7cd4ace1…` | Native requested workflow; requires academic PyRosetta | No PyRosetta, AlphaFold2 parameters, or ProteinMPNN weights |
| `freebindcraft-open-fallback` | FreeBindCraft `28c43fc4…` | Open, derived, explicitly **non-equivalent** fallback | No PyRosetta, AlphaFold2 parameters, or ProteinMPNN weights |
| `rfdiffusion` | RFdiffusion `9273ef67…` | Exact requested open runtime | No checkpoints |
| `proteinmpnn` | ProteinMPNN `8907e667…` | Exact shared sequence-design component | Repository checkpoints are removed in the same build layer |

The images are runtimes, not model bundles. A normal process start verifies an
external `fs2.nebius.ai/external-model-artifact-manifest/v1` and every listed
file before executing the caller's argv. A manifest contains
`artifact_kind`, the runtime's exact `source_revision`, and nonempty `files`
entries with a safe relative `path`, lowercase `sha256`, and `size_bytes`.
Symlinks, path escapes, missing files, and mismatched content fail closed.

The native BindCraft and RFdiffusion images consume the corrected batch
interfaces directly from repository commit
`3475ce0ee8efdf2d3ccbcc65651ab11fe7cb34fe` through a read-only BuildKit
additional build context. BindCraft contains that commit's
`bindcraft-batch` executable. The distinct open fallback contains the same
commit's `freebindcraft-batch` executable and an image-owned typed runner that
requires both PyRosetta-forbidden flags and emits the adapter's exact output
contract from real OpenMM and FreeSASA results. RFdiffusion contains its
`rfdiffusion-batch` executable and a task-owned typed runner that verifies
the external Base checkpoint, translates only the adapter's typed contigs,
hotspots, diffusion steps, and seed, and then calls the pinned upstream CLI.
No raw caller-supplied Hydra override is accepted by this path.
ColabDesign's bundled ProteinMPNN `.pkl` directories are removed after package
installation and are covered by the artifact-free image smoke; workflows mount
those weights separately alongside the AlphaFold2 parameters.

## Academic BindCraft materialization

The native BindCraft image never downloads, accepts as a build argument, or
contains PyRosetta. The authorized academic environment installs the exact
wheel once into its tenant-private volume. Ordinary runtime Pods mount only
`pyrosetta-bindcraft/site-packages` read-only at
`/opt/fs2/academic/pyrosetta-bindcraft/site-packages`, put that path first in
`PYTHONPATH`, and join supplemental group `65532`. They omit `fsGroup` and do
not run a request-time license-receipt init container. The runtime verifies that
PyRosetta imports from that exact mount before executing. The exact artifact,
installed-environment, installer-evidence, and owner-authorization digests stay
in deployment metadata; licensed bytes remain outside every image. AlphaFold2
parameters use the separate external artifact manifest gate.

The mounted object is the canonical `ArtifactMaterialization`
`bindcraft-pyrosetta-installed-tree`: a 3,287,122,494-byte installed tree with
`fs2-tree-manifest/v1` digest
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`.
The 1,667,097,173-byte source wheel remains distinct provenance at SHA-256
`4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242`.
The authorization and install-receipt digests are metadata only; they are not
request fields or init gates.

FreeBindCraft remains a different repository, image name, identity label, and
catalog relationship. It is not presented as native BindCraft equivalence.
FreeSASA 2.2.1 is built from its SHA-pinned source distribution and is required
at runtime. OpenMM 8.2.0's CUDA 12 conda package, its matching GNU runtime
libraries, and PDBFixer are pinned in the image. The wrapper rejects an
environment in which PyRosetta becomes importable.

## Build and publish

Inspect the exact plan and assert that target tags do not exist:

```bash
python3 build_images.py plan
python3 build_images.py check-targets
```

Build and run offline import/CLI checks:

```bash
python3 build_images.py build
python3 build_images.py smoke
```

Publish only the locked, never-before-used tags:

```bash
python3 build_images.py push
```

`push` checks each target immediately before writing, enables BuildKit max-mode
provenance and SBOM attestations, resolves the regional digest, pulls that exact
digest, reruns the offline smoke, and incrementally writes
`evidence/published-images.json`. It refuses to overwrite any existing tag.
Docker and Skopeo use the existing credential helper; the script never reads or
prints Docker configuration or credentials.

The image smoke command is intentionally artifact-free:

```bash
docker run --rm --network none IMAGE@sha256:... --fs2-image-smoke
```

It proves imports, CUDA 12 build identity, and absence of embedded checkpoints
and PyRosetta. It is not semantic model evidence. Semantic acceptance is
recorded separately from these packaging checks in
`evidence/h100-semantic-validation.json`; no import-only probe is treated as
model readiness.
RFdiffusion's CUDA DGL wheel loads `libcuda.so.1` at import time, so its
driverless smoke records the installed DGL/SE(3) distributions and defers the
three GPU-linked imports explicitly. The same command on the H100 canary must
return an empty `gpu_required_imports_deferred` list before publication is
accepted.

## Final native successor: production filters and four external trees

`cuda121-r16` is the final native image. `r14` and `r15` are superseded and
must not be consumed: `r14` could not produce truthful production evidence, and
`r15`, which fixed that, predates the controller's runtime-localization marker
flag. All three are built from the same BindCraft revision `7cd4ace1…`; the tag
advances because the runtime contract changed, never to overwrite a tag.

`r14` could not produce truthful production evidence because:

* The design metrics read `Average_InterfaceResidues` and `Average_BuriedSASA`,
  which upstream never writes. Both were read with a `"0"` default, so every
  accepted design reported zero interface residues and zero buried area. The
  columns upstream actually writes, and that
  `settings_filters/default_filters.json` thresholds, are
  `Average_n_InterfaceResidues` and `Average_dSASA`. `r15` reads the real names
  and rejects a run whose statistics are missing or non-numeric instead of
  substituting a zero.
* `hotspot_geometry_validated` was a hard-coded `true`. Upstream treats
  `target_hotspot_residues` as a loss preference, and the interface residues it
  records are binder-side, so no upstream column answers whether the binder
  reached the requested target site. `r15` measures it: for every requested
  hotspot it records the closest heavy-atom approach to the binder chain in the
  accepted complex at upstream's own 4.0 Å contact criterion, and fails the
  shard when no requested hotspot is in contact.
* Design depth and MPNN breadth were overridden with literals weaker than the
  SHA-256-pinned production template - one validation recycle instead of three,
  and one sampled MPNN sequence instead of twenty. A run therefore looked
  production-equivalent while searching a twentieth of the sequence space.
  `r15` takes these from the admitted template and only departs from it when an
  operator sets the corresponding variable explicitly.
* `Average_Binder_Energy_Score` is the PyRosetta score `default_filters.json`
  requires to be negative, so `r15` records it and rejects an exactly-zero
  score. Trajectory statistics must also be non-empty, not just the final
  design table.

`r15` also closes the external-tree gap. The image removes both ColabDesign
ProteinMPNN weight directories and contains no PyRosetta and no AlphaFold2
parameters, so four trees arrive from outside it, and the runtime now verifies
all four by full content before any model code runs:

| Role | Mounted at | Identity algorithm |
|---|---|---|
| `pyrosetta-site-packages` | `/opt/fs2/academic/pyrosetta-bindcraft/site-packages` | `fs2-tree-manifest/v1` |
| `alphafold2-params` | `/models/alphafold2` | `fs2-flat-tree-inventory/v1` |
| `colabdesign-mpnn-weights-vanilla` | `…/colabdesign/mpnn/weights` | `fs2-flat-tree-inventory/v1` |
| `colabdesign-mpnn-weights-soluble` | `…/colabdesign/mpnn/weights_soluble` | `fs2-flat-tree-inventory/v1` |

`runtime/tree_identity.py` implements both algorithms and keeps each one
byte-identical to the component that publishes the tree it guards:
`fs2-tree-manifest/v1` to `academic-assets/scripts/install_tree.py`, which
installs the licensed tree, and `fs2-flat-tree-inventory/v1` to the
scientific-localization staging receipts, which publish the public trees. A
digest only this runtime could reproduce would prove nothing about the tree the
publisher actually shipped. Reading every byte is affordable and measured on the
eu-north1 shared filesystem: 3,287,122,494 bytes of PyRosetta in 14.85 s at
211 MiB/s, 5,587,959,437 bytes of AlphaFold2 in 5.36 s at 994 MiB/s, and each
26 MB MPNN tree in under 0.04 s. No metadata-only shortcut is offered, because a
size-and-shape check passes on a tree whose contents were swapped.

Identity authority is split on purpose. The licensed PyRosetta tree is what the
image is licensed around, so its `fs2-tree-manifest/v1` digest
`a93d68e1…` is pinned in the image and a declaration naming a different one is
refused. The three public trees are published by the platform's artifact plane,
which is their authority, so their identities arrive in the per-run declaration
and are then proven against the bytes actually mounted. Both MPNN roots are
additionally required to be the directories `colabdesign.mpnn` itself imports,
so a declared root the model would never read cannot be admitted.

**Tree locations are inputs, never constants.** The shared filesystem that
carries these trees is re-published as its handoff evolves, so neither the image
nor the renderer holds a layout: `FS2_BINDCRAFT_EXTERNAL_TREES` points at an
`fs2.nebius.ai/bindcraft-external-tree-admission/v1` document giving each role's
root and expected identity. A path move that changes no bytes therefore needs no
rebuild.

### Runtime localization marker

Both `run-trajectory` and `aggregate` require
`--runtime-localization-marker <absolute path>`. The shared controller
(`fs2_serve.scientific_batch.models.StageInvocation`) rejects any
runtime-artifact stage whose argv omits the canonical
`<working directory>/.fs2/runtime-localization.json`, writes that file itself,
and exports the same path as `FS2_RUNTIME_LOCALIZATION_MARKER`.

The wrapper reads the marker rather than ignoring it. The controller owns the
file's schema, so only what it must be is required - an absolute path to a
readable JSON object, agreeing with the environment variable when that is set -
and the wrapper records its path, size and SHA-256 in the shard output. When the
marker carries a `generation` string and the admission document carries one too,
they must match: a generation that rolls between scheduling and execution then
fails the shard instead of quietly designing against different weights than the
scheduler chose. `aggregate` applies the same check across shards, which is the
only place separately-scheduled Pods can be compared, so one result set can
never carry two provenances.

The ProteinMPNN lane is a request parameter, not an image constant:
`FS2_BINDCRAFT_MPNN_WEIGHTS` selects `original` (vanilla) or `soluble`, and with
the variable unset the pinned advanced template decides. Both weight directories
are removed from the image and mounted per run.

`run-trajectory` accepts a pre-created empty output directory, because the
controller creates the stage working directory before starting the container. A
non-empty one is a re-entered attempt and is rejected rather than merged into.

`qualification/render_semantic_job.py` renders the acceptance run from an
`fs2.nebius.ai/bindcraft-external-tree-handoff/v1` document. It replaces the
hand-written native Pod, which pinned a superseded digest and created an *empty*
`weights_soluble` package - so it read no soluble weights while appearing to
test that lane. The rendered Job runs the design stage as an init container and
the aggregate as the main container over one workspace, because the shard output
lives on the Pod's own volume and two Jobs could not share it. Both stages enter
the image through its outer entrypoint, carry the adapter's argv with the pinned
`default_4stage_multimer.json` and `default_filters.json` digests and the
localization marker, and mount all four trees read-only; only the design stage
requests an accelerator:

```bash
python3 qualification/render_semantic_job.py \
  --handoff <four-tree-handoff.json> \
  --image cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/bindcraft@sha256:<r15> \
  --run-id <run> --job-name <job> | kubectl apply -f -
```

Passing `default_filters.json` is the semantic bar, not a formality: it requires
`Average_n_InterfaceResidues` ≥ 7, `Average_dSASA` ≥ 1, `Average_dG` ≤ 0,
`Average_Binder_Energy_Score` ≤ 0, `Average_Hotspot_RMSD` ≤ 6,
`Average_ShapeComplementarity` ≥ 0.6, `Average_pLDDT` ≥ 0.8 and
`Average_i_pTM` ≥ 0.5 across 54 active thresholds. The earlier `r12` evidence
below was recorded with `no_filters` and its mean pLDDT of 0.79 would not pass
this set.

### Live acceptance is gated on the immutable four-tree handoff

The `r15` semantic acceptance run has **not** been executed. The localization
candidate that publishes the three public trees is blocked, and this task is
instructed not to bind the mutable `scientific-localization/public/<artifact_id>`
paths or its three-tree handoff, but to wait for the immutable
`/sha256/<tree>` four-tree handoff with in-generation stable terminal markers and
recursive PyRosetta identity. The image, the runtime contract, the renderer and
their tests do not depend on those final paths and are complete; the run is the
one remaining step. Two requirements the handoff must satisfy for this runtime:

* A terminal marker written *inside* a tree root changes that tree's
  `fs2-flat-tree-inventory/v1` digest, because the inventory enumerates every
  regular file in the flat root. Markers must live outside the tree roots, or
  the published identity will not match the mounted tree.
* The AlphaFold2 root must keep a `manifest.json` declaring `artifact_kind`
  `bindcraft-af2-params` and `source_revision` `7cd4ace1…`. The shared outer
  entrypoint's `fs2.nebius.ai/external-model-artifact-manifest/v1` gate runs on
  every non-smoke command and reads it.

## H100 semantic acceptance

All live runs used project `project-e00rene`, region `eu-north1`, kube context
`k8s-inference-h100`, and capacity-block H100 SXM5 80 GB nodes. RFdiffusion ran
the corrected `run-shard`/`aggregate` interface for 50 diffusion steps with its
exact external Base checkpoint. ProteinMPNN ran its real CLI against its exact
external checkpoint. Native BindCraft mounted exact AF2 and soluble ProteinMPNN
weights plus the private PyRosetta tree, then completed a 140-step design and
the full ProteinMPNN, AF2, PyRosetta, filter, rerank, aggregate, and canonical
output-validation path. The accepted BindCraft candidate recorded iPTM 0.67,
mean pLDDT 0.79, interface dG -44.25, and shape complementarity 0.66. Its peak
cgroup memory was 11,520,126,976 bytes and sampled GPU memory was 7,241 MiB;
no cgroup OOM or memory-limit event occurred with the 96 GiB request / 128 GiB
limit. Temporary Pods were deleted after evidence capture. No model service was
deployed.

The explicitly non-equivalent FreeBindCraft lane then ran the same bounded
PDL1 target through its real `/opt/fs2/bin/freebindcraft-batch` `run-trajectory`
and `aggregate` operations. The full path completed 140 design iterations,
soluble ProteinMPNN sequence generation, two-model AF2 validation, a real
OpenMM CUDA energy evaluation, FreeSASA interface scoring, atomic aggregation,
and independent `freebindcraft-v1-0-5` output validation. One candidate passed
with iPTM 0.67, mean pLDDT 0.80, buried SASA 1,802.15 Å², shape
complementarity 0.66, and OpenMM energy -27,405.941865 kJ/mol. Peak cgroup
memory was 11,523,444,736 bytes and sampled GPU memory was 8,304 MiB, with no
OOM event. The accepted `r9` digest is
`sha256:6d44aba5780c2b74985db037045e06e732f4e867795d33a6313c5faa95bd9e30`.
The rejected `r7` and `r8` canaries exposed, respectively, an unenforced
trajectory bound/DAlphaBall mode problem and a CPU-only OpenMM package; neither
is semantic acceptance evidence.

After the Terraform H100 node roll, the same three requested digest references
were pulled again through the approved cluster registry path and exercised in
task-labelled Pods. BindCraft passed CUDA/JAX/PyRosetta imports plus the typed
trajectory CLI help path; RFdiffusion passed CUDA/DGL/model imports, upstream
Hydra help, and a valid corrected-wrapper aggregate invocation; ProteinMPNN
passed CUDA/module imports and upstream CLI help. This bounded post-roll check
is recorded in `evidence/h100-postroll-import-cli-smoke.json` and does not
replace the full semantic evidence above. It contacted no Forge cluster, used
no Forge credential or namespace, and left zero task Pods or Jobs behind.

Trivy 0.70.0 HIGH/CRITICAL `--ignore-unfixed` results are recorded in
`evidence/vulnerability-scans.json`. The report is evidence, not a zero-finding
claim or an implicit policy exception.

The corrected native successor is `cuda121-r12` at digest
`sha256:fcc5b0da20f8bee01e78ad042ba597f7596cd52322a542ff4e48c855abae0177`.
The approved H100 private mount resolved PyRosetta dist-info release
`2026.29+releasequarterly.80a0635615` and tree digest
`a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d`.
An explicit `pyrosetta.version()` check returned the PyRosetta-4 2026.29
banner; an unsupported API is recorded as an API limitation, never as an
installation failure.

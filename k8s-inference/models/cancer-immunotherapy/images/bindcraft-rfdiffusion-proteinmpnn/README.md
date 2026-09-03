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
`bindcraft-batch` executable. RFdiffusion contains its `rfdiffusion-batch`
executable and a task-owned typed runner that verifies
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
Its optional FreeSASA metric engine is not included; the upstream open
Biopython fallback is used, while OpenMM and PDBFixer are pinned in the image.

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

Trivy 0.70.0 HIGH/CRITICAL `--ignore-unfixed` results are recorded in
`evidence/vulnerability-scans.json`. The report is evidence, not a zero-finding
claim or an implicit policy exception.

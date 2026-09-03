# Complexa and BoltzGen runtime images

This directory builds the first cancer-immunotherapy runtime-image group. Each
image is a batch runtime, not a deployed service. Sources, base images,
dependency locks, target tags, smoke commands, and the L1/L2 boundary are bound
in `catalog.json`.

## Supply boundary

The default images contain code and runtime dependencies only. Checkpoints,
reference data, external evaluation binaries, and JAX compilation caches belong
under `/opt/fs2/artifacts`, which is supplied by the separate artifact-cache
plane. In particular:

- Proteina-Complexa does not include Complexa, AlphaFold2, RoseTTAFold3,
  ProteinMPNN, or other reward-model artifacts.
- BoltzGen never enables upstream `DOWNLOAD_WEIGHTS`; all BoltzGen/Boltz-2
  checkpoints and inference molecules stay external.
No baked-weight variant is produced by this task.

## Reproducibility and CUDA portability

Every Git source and codeload archive is pinned and checksum-verified. Python
dependency locks include artifact hashes. Builder, runtime, and `uv` images are
digest-pinned.

The Proteina-Complexa upstream image is `nvcr.io/nvidia/pytorch:24.08-py3`.
NVCR returned HTTP 403 during this task, so the user-approved substitution uses
the publicly pullable official `nvidia/cuda:12.6.3-cudnn-*` images and installs
PyTorch `2.7.0+cu126`. The dependency lock makes upstream's otherwise implicit
Atomworks compatibility substitutions explicit: SciPy 1.16.1, Einops 0.8.1,
and Biotite 1.6.0. BoltzGen uses PyTorch `2.7.1+cu126` and the source-era
cuEquivariance `0.10.0` package set; later `0.11` packages are incompatible
with the pinned source/PyTorch API. Neither runtime checks a GPU product name.
Complexa's compiled `torch-scatter` extension includes
Turing, Ampere, Ada, and Hopper targets plus Hopper PTX forward compatibility.

Complexa’s production runtime installs the digest-pinned CUDA 12.6 NVCC package
`cuda-nvcc-12-6=12.6.85-1` and asserts both `ptxas` and `nvcc` exist. This keeps
the default image external-weight-only while allowing JAX’s AF2 parameter
loader to compile on H100.

## Commands

All orchestration uses argument vectors and does not execute model input through
a shell.

```bash
python3 build.py check
python3 build.py inspect proteina-complexa
python3 build.py build proteina-complexa
python3 build.py smoke proteina-complexa
python3 build.py publish proteina-complexa
```

Repeat for `boltzgen`. `publish` re-inspects the exact destination,
refuses an existing tag, repeats the offline/no-network smoke tests, pushes, and
verifies the remote digest. It also creates an SPDX JSON SBOM outside Git and
writes its SHA-256 plus the immutable digest reference to
`evidence/publish-receipt.json`.

The GPU smoke command for each image is recorded in `catalog.json`. Run it as a
short-lived Job on the shared H100 cluster with the image consumed by digest,
capture the JSON output, and delete the Job. This task must not create a
persistent model Deployment.

## Qualification semantics

Import, CLI, CUDA ABI, and one-device tensor probes qualify the image only.
They never make a model Ready. Model acceptance additionally requires exact
external artifacts, a real model/domain forward on project-e00rene H100, and a
semantic output validator that rejects input visualizations, diffusion
sentinels, incomplete protein backbones, and degenerate coordinates.

The accepted Proteina-Complexa runtime is
`proteina-complexa@sha256:d3f3c9bc5a2285b09932eb05a57ef73da3201bc69b77462420c0d42a0aaa91d8`.
With the two externally staged checkpoints it completed a two-step PD-L1
binder generation on a preemptible H100. The generated two-chain, 179-residue
PDB passed structure and artifact-hash validation.

BoltzGen has two separately qualified execution paths:

- Portable: `boltzgen@sha256:e2bb0a15c7585916ed650e851903bfe55643141f6521e36cdcb382670c0d3c02`
  with `--use_kernels false`. An external-checkpoint 20-step design generated
  a 302-residue, two-chain CIF with 2,140 atoms and a 49.32 Angstrom maximum
  per-chain extent. The generated CIF, not the input visualization, passed.
- Optimized: tag `31d9d9b9c72245b4ed6fe8742d6fbf4e1a3552a0-cueq-v3`, digest
  `sha256:9c3230424e02d725dc145b8f21a18f283910e1beba1f37466598ee832813820e`.
  This adds the Python headers required for Triton's runtime CUDA-driver
  extension. On a preemptible H100, `Using kernels: True` completed a 20-step
  design. The generated 287-residue, two-chain CIF contained 2,001 atoms and
  passed the same artifact, backbone, diversity, finite-coordinate, and
  per-chain geometry checks. Exact Jobs, argv, times, resource IDs, artifacts,
  and output hashes are in `evidence/h100-qualification-receipt.json`.

The final Trivy scan found no fix-available critical vulnerability in the
optimized BoltzGen digest. It found three in the Go binary bundled by pinned
WandB 0.23.1 in Complexa. Inference did not exercise that logger binary, but
the upstream autoencoder imports WandB, so it was not removed or upgraded
without a separate compatibility forward. Exact CVEs and fixed versions are
recorded in the H100 qualification receipt; they are not silently waived.

The ptxas-v1 image qualification on H100 reports CUDA 12.6 `ptxas`; AF2/model
forward attempts remain non-ready because the mounted PVC lacks AF2 parameters
and the upstream CLI requires a writable project `.env`.

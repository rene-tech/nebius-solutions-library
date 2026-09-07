# Standalone OpenFold3 Preview2 H100 qualification

This is a distinct upstream Preview2 runtime, **not** the current OpenFold3
OpenBind scientific profile and **not** NVIDIA NIM. The exact v4 image passed
three cached fresh-process trials and a separate first-compilation trial on H100.
Public HTTP/MCP qualification remains separate and pending root deployment.

The archived native image is
`nvcr.io/nim/openfold/openfold3@sha256:6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2`.
On 2026-09-07 at 09:51 UTC its registry returned HTTP 403 using the existing
authorized client. No exact cached local or cluster image was found. The parent
approved the explicitly labelled upstream fallback; the archived NIM record is
not rewritten as if it qualified this runtime.

## Exact package and artifacts

- Upstream tag `0.4.2`, Git revision
  `4a0eaeaeae8ca1d815d0a97d8eb45d639b91a47e`, Apache-2.0 source.
- PyPI wheel `openfold3-0.4.2-py3-none-any.whl`, SHA256
  `44040099bd7828dd3dd9574069187f5e89b49ce11bbd640877a4d0b35292442a`.
- Preview2 checkpoint `of3-p2-155k.pt`, 2,287,928,196 bytes, SHA256
  `af09eac4f29cef856633af07558cb143226fe95ebbef2c20921769d4a5f4bee4`.
  The S3 ETag and full downloaded SHA256 are both checked during image build.
- CCD `components.bcif`, 63,393,643 bytes, SHA256
  `473d845c8b250b188dbed9bf505ae206692a178a2a7c4869bf8f9de707ffcc0c`,
  retained CC0-1.0 database artifact.

The upstream [checkpoint compatibility table](https://openfold-3.readthedocs.io/en/latest/parameters_reference.html)
requires `>=0.4.0,<0.5.0` for Preview2. The existing regional OpenBind image is
reused only for dependencies: its installed 0.5.0 package is explicitly removed
and replaced with the exact 0.4.2 wheel. Its external OpenBind checkpoint and
batch wrapper are not used. GPU compatibility still requires actual inference.

## Runtime and measurement contract

The adapter uses the unchanged upstream `predict` preset, `32-true` trainer,
seed 42, one diffusion sample, the provided inline A3M, and no remote MSA service
or templates. It supports protein chains with inline main A3M or single-sequence
mode; unsupported scientific settings are rejected, never silently discarded.
No claim of numerical identity with NIM is made.

A single GPU module is loaded with the upstream checkpoint loader and moved to
CUDA before the source-clock `MODEL_READY` marker. Every request creates a fresh
upstream trainer/data module/output writer around that exact module. First
inference compilation belongs to first-output latency, not model-ready time.
The per-image/driver/SM runtime-cache PVC is retained between fresh Pods. Weights
are image-baked, so image acquisition, existing image cache, OS page cache and
fresh process are reported separately; these are not network weight downloads.

The original validator is unchanged at
`catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/openfold3-native/validate_openfold3.py`
(SHA256 `679b3e027b18e78b4646569e8c6395fb5f62c4647704bb5089aa2385a20d11f5`).
Both original distinct request IDs use its exact 20-aa query-only-MSA fixture.
An additional different 40-aa protein request exercises genuine varied input.
All responses must preserve native shape, finite actual upstream confidence
scores and nontrivial CIF output with at least 100 atom rows. Scores map directly
from `sample_ranking_score`, `avg_plddt`, `gpde`, `ptm`, and `iptm`; none is invented.

The isolated runner creates only task-labelled Pods and one 4-GiB compile-cache
RWX claim. It captures Pod creation/scheduling/container readiness, source logs,
node/GPU/image identity, original inputs, full private outputs and validation
receipts. Production deployment and route integration remain parent-owned.

## Reproduce

Build `models/structure/openfold3-preview2/Dockerfile` from its own directory,
push the regional image, then resolve its immutable digest. Invoke `probe.py`
with `create`, then `validate`, using explicit `--kubeconfig`, `--image`,
`--repetition`, and private `--output` directory. Delete only that owned Pod with
`delete` after preserving its output. Repeat with new repetition numbers.

CPU tests: `components/control-plane/.venv/bin/python -m pytest acceptance/h100-fleet/openfold3-standalone -q`.

## Measured results and integration

On 2026-09-07, exact image
`sha256:1e35247f0de8be59119ddf875c2631173a8c533c90f7b21e1c01ef70a069c00c`
passed both original native requests plus the different 40-residue protein in
each of r03–r06: twelve real CIF outputs. H100 SM90, driver 580.159.04, one GPU,
6 CPU/32 GiB requests and 16 CPU/64 GiB limits, `MAX_JOBS=8`, 8-GiB shared memory.

For the three cached fresh processes r04–r06, container-to-source-GPU-ready is
31.437549 seconds median (30.136136–33.684628); Pod-create-to-ready is 35.684628
seconds median (31.136136–36.437549). First submitted request to validated output
is 12.889124 seconds median (11.980028–13.017618). These are direct isolated Pod
measurements, not public activation times. Readiness-to-probe submission gaps
are recorded separately and are not inference latency.

The separate r03 first-compilation output took 397.640487 seconds: native
upstream CUDA attention JIT compilation occurred after model-ready. The exact
image/driver/SM compile tree must be retained or this cost can recur. OS page
cache was neither evicted nor controlled. No p95 is estimated from three runs.
Earlier v2/r01 CCD text-decoding and v3/r02 MSA filename adapter failures remain
in the private receipts; they are not successful v4 trials. The fixes preserve
the binary CCD and original inline A3M, rather than changing the input or oracle.

`qualification.json` has full source clocks, Pod/image/GPU identities, hashes and
per-output oracles. `integration.json` and `hardware-binding.json` bind the
selected independent runtime and retained task cache. The source manifest is
`models/structure/openfold3-preview2/k8s.yaml`, not the archived shared NIM
manifest. Its cache ABI is deployment-selected; only H100 is measured. Production
uses Terraform-owned storage: adopt the existing compile-cache PVC or copy its
entire tree into the new claim before bootstrap, preserving source storage.

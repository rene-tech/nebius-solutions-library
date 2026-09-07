# Standalone OpenFold3 Preview2 H100 qualification

This is a distinct upstream Preview2 runtime, **not** the current OpenFold3
OpenBind scientific profile and **not** NVIDIA NIM. The exact v4 image passed
three cached fresh-process trials and a separate first-compilation trial on H100.
It is deployed and passed both original inputs over public HTTP and MCP.
Public request timing is kept separate from process startup.

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

The production destination `openfold3-preview2-cache-rwx-3668448b` was created
by Terraform and seeded before startup on 2026-09-07. CPU-only `copy_compile_cache.py`
copied and verified 69 files / 35,127,243 bytes, including the image/driver/SM
path above. The final metadata pass completed at 11:02:47 UTC. Hashes, ownership,
modes and Ninja dependency mtimes are preserved; both copy Pods were deleted
and the source claim is retained. Production source-clock `MODEL_READY` was
11:05:56.934284 UTC. This readiness observation is not public acceptance.

`public_verify.py` reuses the existing public transport and unchanged native
validator for the two original 20-aa request IDs over HTTP and MCP, with valid
TLS and exact operation-to-Pod/image/model binding. The different 40-aa input
remains in the direct qualification above. Public results are reported
separately from fresh-process startup and native JIT compilation.

The clean post-repair public cohort passed all four outputs between
11:33:58.846 and 11:34:51.280 UTC. HTTP request-to-validated-result times were
10.650 and 10.718 seconds; MCP times were 12.203 and 11.731 seconds. All four
operations completed on their first attempt with the exact qualified v4 image,
upstream source revision and independently verified production Pod UID. See
`public-results.json`; these are already-hot requests, not startup trials.

`public-repair-window.json` separately preserves the first accepted operation
that waited for the common renderer's missing `component=model-runtime` label.
The preexisting gateway network policy required that label. After the normal
controller release, the original request recovered and passed; its 1,506.195-second
activation interval is repair time, not model startup. The common renderer fix
did not change the model image, template identity, inputs, or validator.

## Optional standalone Preview2 GPU snapshot

The exact v4 image passed three matched normal loads and three fresh
donor-deleted CUDA+CRIU restores. Both original 20-aa structural request IDs
passed on every trial. This is standalone Preview2, not the OpenBind scientific
profile. The original donor was deleted; restores mount the captured shared-FS
bundle read-only with private writable scratch.

| Boundary, median (min–max), n=3 per mode | Normal | Restore |
|---|---:|---:|
| Container start → observed application ready | 37.451 s (30.806–37.916) | 9.817 s (9.446–9.883) |
| Pod-create request → observed application ready | 41.689 s (35.710–41.707) | 14.398 s (14.197–14.407) |

See [snapshot-qualification.json](snapshot-qualification.json) and
[snapshot-bundle.json](snapshot-bundle.json). Existing images, localized
weights, exact compile tree and shared-FS caches remain retained. No host cache
eviction, new-node or reserved-RAM guarantee is claimed. Original inputs were
also used before capture; these are not unseen-input trials. Application
readiness requires the actual completed CUDA restore event before HTTP health.
All donor, first-restore, paired-trial and publication Pods are deleted.

The native bash activation command, cwd `/opt/fs2/openfold3-preview2`, UID/GID
10001, image, model parameters, request settings and resource limits are
unchanged. The first donor failed before model loading because the generic
snapshot PATH hid this image's conda-only Python; that negative receipt is
retained. The qualified variant prepends the exact existing conda bin only to
the snapshot supervisor PATH and uses its absolute interpreter for address
initialization. The native activation still establishes the original worker
environment. Frozen v8 launcher/source bytes were not changed.

Successful capture took 1.523 seconds for CUDA checkpoint, 75.397 seconds for
CRIU and 0.752 seconds for required fsync. The 11,341,560,585-byte/119-file
bundle is retained on `fs2-fleet-snapshots-rwx-r20260907`, subpath
`of3-preview2-r02`. The qualification manifest digest is
`9494710486412a4bfd004effe6b92e456bcc12c1c92a7a06f632031d5ec54ea0`.
The generic hash helper's envelope digest is separately recorded in the
private publication receipt; it is a different serialization of the same files.

`snapshot_probe.py` reproduces donor/validation/capture/deletion. Use
`publish_existing_bundle.py`, `run_serving_pairs.py --model openfold3
--address-python /opt/openfold3/.pixi/envs/openfold3-cuda12/bin/python3`, then
`report_serving_pairs.py` and `build_serving_bundle.py --restore <actual-pod-receipt>`.
Production selection and public fallback tests are a separate release step;
normal-load remains the default.

# Medical/media H100 qualification — 2026-09-07

SDXL, NV-Segment-CT and NV-Reason-CXR-3B each passed two distinct real outputs
on an initial empty-weight-cache trial and three fresh-process retained-cache
trials. These are isolated direct-pod qualifications, not public activation
measurements. The probes did not change production workloads; the release
owner subsequently deployed the three small models with Terraform.

| Model | Container start → application ready, median (min–max), n=3 | Pod creation → application ready, median | Output checks |
|---|---:|---:|---|
| SDXL | 11.702 s (11.629–11.704) | 14.704 s | Two distinct, nonconstant 512×512 PNGs; direct PNG and base64 JSON |
| NV-Segment-CT | 11.100 s (10.958–11.360) | 14.100 s | Two distinct synthetic NIfTI masks, 35 and 204 foreground voxels |
| NV-Reason-CXR-3B | 79.395 s (77.115–82.521) | 82.395 s | Both pinned CXR fixtures pass unchanged reasoning/answer oracle |
| Evo2-40B v5, 2 H100 | 23.548 s (23.217–26.169) | 28.548 s | Both original historical Hopper DNA20 fixtures; retained OS page cache |

The runtime's own SDXL/Segment load timer excludes Python imports and is not
the full startup number above. Exact source-clock lines, Kubernetes start/ready
times, image-pull events, GPU identities and separate acquisition trials are
in `startup-results.json`. No percentile is estimated from three trials.
Kubernetes container-start timestamps have one-second granularity; source log
timestamps have finer precision. NV-Segment-CT's existing validator did not
record client response durations; the report leaves those null and retains
server HTTP-200 timestamps instead.

SDXL uses the same pinned model, FP16, payload, dimensions, steps and seeds.
Its former B300-specific exact encoded-PNG hash/length is not a cross-hardware
oracle. Structural checks remain enforced, and both decoded H100 PNG hashes
were identical across all four repetitions. The medical examples are
non-clinical synthetic/public benchmark fixtures; no patient data was used.

## Integration and cache handoff

`integration.json` contains immutable eu-north1 image references, model
revisions, resources, entrypoints, readiness paths, fixtures and first source
clock receipts. The three source manifests now use the literal
`deployment-profile-abi-v1` in place of the old B300 driver/SM suffix and have
no baked-in B300 selector. The release owner must supply the model capability
selector and substitute the live ABI `driver-580.159.04-sm90` throughout the
annotations and environment paths to reproduce these measured cache keys.
No executable, image, model setting or request changed for these three models.

| Retained task cache (source) | Source PV | Durable manifest claim (destination, if copying) |
|---|---|---|
| fs2-mm-sdxl-cache-20260907 | pvc-0c61a7a4-b5f8-4228-8327-3588ad8e2a67 | sdxl-cache |
| fs2-mm-nv-segment-ct-cache-20260907 | pvc-eccd512c-8c1b-4e4d-9015-d634da404d5d | nv-segment-ct-cache |
| fs2-mm-nv-reason-cxr-3b-cache-20260907 | pvc-6e9419cf-17df-4caf-9c5c-f1e1f1b3778a | nv-reason-cxr-3b-cache |

These three PVs are directories in the same `csi-mounted-fs-path-sc` shared
filesystem (`mounted-fs-path.csi.nebius.ai`, volumeHandle equal to PV name),
not separate block disks. Their declared capacities do not reserve independent
space. Parent expanded the shared filesystem to 1 TiB via Terraform during
this task. Original generated PVC manifests are retained in each private r01
directory cited in `integration.json`.

Two supported handoff choices, performed by the release owner:

1. Adopt the exact retained claim into Terraform and point the durable model
   Pod volume at it; preserve its PV binding and data. This avoids a copy.
2. Terraform creates the durable destination claim first. A bounded CPU-only
   copy Pod mounts the matching source read-only at `/source` and destination
   read-write at `/destination`. Verify the destination is empty and the two
   resolved PVC/PV names differ, then copy the complete cache tree with
   `cp -a /source/. /destination/`, preserving HF relative symlinks. Run with
   appropriate destination ownership. Recheck pinned snapshot filenames/blob
   sizes and run both public semantic fixtures before releasing the source.
   Reuse compile caches only with the exact tested image and ABI; otherwise
   copy weight artifacts and let the new ABI build its own cache namespace.

All small-model probe Pods were deleted after their validated repetitions.
All task PVCs remain intentionally retained; no source cache is authorized for
deletion until the release owner's public route/cache acceptance is complete.

## Evo2 status

The retained image's service wrapper required one SM103 B300, while its exact
upstream Vortex loader already supports layer placement over all visible GPUs.
The explicit `evo2_h100.py` adapter requires exactly two H100 SM90 GPUs and
asserts that model parameters reside across cuda:0 and cuda:1. It preserves
the model checkpoint, upstream kernels, BF16/Transformer-Engine FP8 projection
precision and generation contract. Five hardware/device-context tests pass.
Only v5 is qualified: r06–r08 passed both original historical Hopper/NIM
requests, after r05 and four additional same-process stress requests also
passed. Twelve total v5 outputs passed. Do not enable earlier candidates.

Rejected initial wrapper-only candidate (do not deploy):
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/evo2-runtime@sha256:0dd47e109dbe6cf7f406c68364fe4d6577d65ef1bc345254aee9433d281f50ca`.
The image inherited `CUDA_VISIBLE_DEVICES=0`. With two devices explicitly
exposed, its native FlashAttention call failed at 08:11:36 UTC with
`no kernel image is available for execution on the device`; cuobjdump confirms
SM103-only cubins. Negative receipts are retained under `evo2/preflight` and
`evo2/preflight-sm103`. The successor Dockerfile now bakes visibility `0,1`
and rebuilds the exact SHA256-bound FlashAttention 2.8.3.post1 sdist for SM90
in a separate build stage, copying only the runtime package into the image.
PyTorch 2.8.0+cu129, Transformer Engine and model versions stay unchanged.

The intermediate v4 candidate, subsequently rejected, is
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/evo2-runtime@sha256:d4253700c8242da9b6546edd0c51853e441362a4a48056bd5cc284d4075416ce`.
Its unchanged normal server entrypoint selects the explicit `h100-2x` backend
from the image's baked `FS2_EVO2_PROFILE`. No Pod command or visibility patch
is needed. Native FlashAttention and Transformer Engine FP8 tests passed on
both H100s at source-clock 08:29:50.255284767 UTC; this kernel preflight is not
full-model readiness. The first full model process began at 08:31:27 UTC.

Although v4 initially produced valid outputs, r04 later returned malformed
DNA repeatedly. Those failures remain in the denominator and disqualify v4.
The upstream Vortex loop transferred tensors between GPUs but did not set the
current CUDA device for every block's native/Triton launches. v5 wraps each
existing block call in its actual `torch.cuda.device` context, preserving
layer placement, kernels, precision and all request/oracle semantics.

Qualified image:
`cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/evo2-runtime@sha256:383f9979021bd3fe018c4dbba675610e0a5f7282b2164db1d8386611351de6f5`.
`Dockerfile.evo2-h100-adapter` layers this exact adapter over the SM90 runtime;
`Dockerfile.evo2-h100` also supports rebuilding the complete runtime.
`evo2-qualification.json` pins the three final source-clock receipts, model,
image, artifact, original validator and negative history. All three have
process `read_bytes=0`; 23.548 seconds is a retained OS-page-cache result,
not a disk-cold or public activation figure. No v5 disk-cold figure is claimed.

Its exact 82,253,491,694-byte checkpoint was downloaded, merged and verified on
`fs2-mm-evo2-40b-cache-20260907`, a 192Gi RWO `compute-csi-default-sc` claim,
PV `pvc-e5e41f44-b65f-4915-ad9f-77bf15bc45c8`, Nebius block disk
`computedisk-e00fjnwc9qk5nfzj2t`. These measurements use that block volume.
Staging completed at 08:26:32.396422945 UTC. The verified merged checkpoint
SHA256 is `dd299612b1c1cdded0dfdcaf4d16f98fc97458261d80f4d662429f0ccb316bc3`.
The CPU-only staging Pod and auxiliary kernel-test Pods have been deleted.

The rejected v4 first full process reached application READY at 08:47:07.882863205 UTC:
940.882863 seconds after container start. Weight load took 914.931334 seconds
including imports; first-use compilation took 25.318832 seconds. This was
prelocalized checkpoint storage, but first-process disk I/O, not a download.
Both devices hold model parameters (41,125,446,656 and 41,117,041,664 bytes).

The release owner will create the normal shared-filesystem destination
`evo2-40b-cache-rwx-ecc3e914`. `cache_copy.py` mounts the source read-only,
copies the checkpoint, verifies its complete destination SHA256, then preserves
the materialization receipt and exact-image compile cache. It never changes
claims or deletes the RWO source. `probe.py --existing-cache-pvc ...
--cache-cohort ...` records a separate fresh-process shared-FS qualification;
copy/hash activity makes that an explicitly cache-conditioned measurement.
The RWO source and hot r08 Pod remain retained until this handoff is verified.

### Evo2 oracle reconciliation

The current packaged B300 oracle expected `ATTTTTTTTTTTTTTTTTTT` and
`TGTTTTTTTTTTTTTTTTTT`. H100 returned `ATCGATCGATCGATCGATCG` and
`GATTACAGATTACAGATTAC` twice each, failing that profile. Those failures and
the original packaged validator remain unchanged in the retained evidence.

The outputs exactly match a separate, preexisting Hopper/NIM oracle: clean
committed validator `120295691593480127e600a0f900495dcc90e25e` (2026-08-18),
SHA256 `113747668c537e83c61648e193419248696e144d5b0598b1f6ca7d38efabe496`,
plus all six reopened H200 NIM responses from the 2026-08-17 legacy cohort.
Both original request payloads are identical. `evo2-hopper-oracle.json` pins
the earlier image, source commit, fixture payloads and all six response hashes.
`evo2_hopper_validate.py` uses those exact expected sequences while retaining
the existing protocol, DNA20, finite timings, disabled-logits/probabilities,
and distinct-request checks. It does not claim general NIM numerical parity.
The first explicit historical-profile confirmation passed at 08:55:30–32 UTC.
The long readiness-to-confirmation gap is oracle investigation time, not
inference latency. Later cached repetitions use this profile from the outset.

## Public surface checks

All twelve public outputs passed on 2026-09-07 between 08:36:58.972 and
08:37:52.175 UTC: two original inputs for each of SDXL, Segment and CXR on
each surface. Redacted request timings, operation IDs and independently bound
runtime identities are in `public-results.json`. Private full semantic and
operation receipts are under `medical-media/public-all-r04` in the task's
private artifact root.

`public_verify.py` exercises two original semantic inputs per model through
both public HTTP and generic MCP `invoke_model`. Credentials stay in memory;
TLS verification is enabled. It independently discovers deployment route
revisions with MCP `list_models`, then checks each operation's bound Pod UID,
immutable runtime image, HF weight revision and deployment spec digest. Route
revision and weight revision are deliberately recorded as separate identities.
For SDXL only the first response envelope changes from direct PNG to base64
JSON, because the generic public operation protocol transports JSON. Exact
generation parameters, seeds, steps and decoded H100 PNG hashes are unchanged.
The direct-PNG qualification above is retained separately.

Use the control-plane virtualenv, which supplies the MCP client dependencies:

```sh
components/control-plane/.venv/bin/python acceptance/h100-fleet/medical-media/public_verify.py \
  --credential-bundle /path/to/private/final-stack-output.json \
  --output /path/to/new/private/public-verification
```

These requests exercise already-hot routes, not cold activation. Earlier
verifier attempts are retained: operations succeeded, but the initial harness
incorrectly compared a dynamic deployment revision with the HF weight revision.
That is verifier calibration evidence, not a model execution failure.

## Reproduction

Run `probe.py --help` for bounded create/validate/delete actions. Use a distinct
repetition output directory under a private artifact root. Model source
manifests, generated Pod/PVC specs, application logs, node/GPU identities,
events and semantic receipts are retained per trial. For Evo2, supply the
successor immutable `--image` on every action. The `stage` action is CPU-only;
wait for it to complete and release its RWO mount before starting GPU trials.

Reduce receipts without response bodies:

```sh
python3 acceptance/h100-fleet/medical-media/summarize.py \
  --private-root /home/tux/.local/state/fs2-h100-fleet-r20260907/medical-media \
  --output acceptance/h100-fleet/medical-media/startup-results.json
```

Validation: five Evo2 hardware/device tests and thirteen cohort tests pass (including
H100 hardware fences, unchanged fixtures and separate runtime/route identities),
all new Python files compile, and all four source
manifests pass Kubernetes client dry-run validation. GPU-performance and
media-serving skills guided the separation of startup boundaries, cache
cohorts and semantic output checks.

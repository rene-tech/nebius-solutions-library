# NVIDIA Cosmos Transfer 2.5 — Scientific AI App

Platform App identity: `cosmos-transfer2-5-2b` (DNS-label-safe), for NVIDIA's `cosmos-transfer2.5-2b`, under **Physical AI and Robotics**. This is a regular shared App, independently managed from `cosmos3-nano`. Normal consumer App grants control access; the earlier test-principal allowlist is removed in the shared-App promotion. The owner tenant in revision history records initial operator provenance, not a private consumer deployment.

On 2026-09-20 the user explicitly confirmed that the organisation's NVIDIA agreement covers production NIM use on this platform. This confirmation is recorded as operator attestation; a working API key alone is not production-license evidence.

The production workflow is orchestrated by the Scientific AI workbench's skill and reasoner, without OSMO. The exact hosted Qwen reference baseline was checked first and its evidence is retained in `../paidf-chat/`. The temporary hosted Qwen Apps have been retired, their runtime resources and weight PVCs deleted, their active catalog entries archived, and their reference-only key revoked. Audit tombstones, model-download receipts and reference results remain retained. The explicit public Token Factory provider profile preserves the NVIDIA stage sequence/settings while declaring its different caption, prompt and verifier models. Cosmos Transfer remains hosted on Scientific AI. Customer readiness still requires the complete public workbench preview/approval/batch release gate.

## Delivery contract

New native `transfer-video` admissions have exactly one platform execution
attempt, including HTTP failures, invalid output, lost responses and expired
worker leases. This enforces the pinned NVIDIA recipe's zero generation retries
at the gateway as well as in the workbench. The effective one-attempt limit and
corresponding budget reservation are persisted with the operation and dynamic
dispatch snapshot. Idempotent replay returns that operation; it cannot turn a
failed or uncertain attempt into another generation. A canonical-source match
also covers independently named Apps backed by this Transfer runtime. Other
models retain their existing retry policies. Previously admitted operations keep
their historical policy rather than having their evidence rewritten.

The correction is locally tested before deployment; current end-to-end release
evidence must identify the exact control-plane image implementing it. Recovery
of the interrupted R5 preview retained its original operation and did not invoke
generation again, but does not qualify the corrected release.

## Historical canary findings (not the current App contract)

The sections below retain the original onboarding sequence, including early simplified probes. They are historical evidence only. The authoritative original PAIDF contract is `nvidia-paidf-original-transfer/v1`: motion threshold **0.6**, original one-frame attribute verification, unchanged 153-frame 1920×1080 reference source. The earlier 0.682/five-frame probe below does **not** replace or qualify that contract.

Status as of 2026-09-20 04:57 UTC: **private H100 canary deployed and Ready; one real full-video generation passed structure, motion and sampled-weather checks; not published as a platform App**. The existing isolated video workbench and shared backend are unchanged by this task. Container-adapter, public HTTP/MCP, chat preview and approval/batch acceptance remain separate gates.

The first successful generation completed at 04:51:24Z in 501.59 seconds,
preserving the source's 1280×720 geometry, all 153 frames, 30 FPS and 5.1-second
duration. Full-frame PAIDF motion score was 0.952509 against the unchanged 0.682
threshold; five-frame weather verification accepted `overcast`. See
[the retained direct-generation and quality evidence](direct-generation-20260920.json).
Automated checks are not proof of physical fidelity or annotation validity.
The generated video is retained for human review. A prior request returned
HTTP422 before generation because upstream cookbook YAML used integer
`resolution`; the real NIM schema requires a string. That failure is retained
separately rather than counted as a successful request.

The target cluster now has operator-provisioned `wan2-ngc-api` and
`wan2-ngc-pull` Secrets (created03:54 UTC). The isolated registry probe references
the API Secret directly; no credential value is copied into the source or
printed. It passed at04:06:02 UTC, resolving the1.1 image index to
`sha256:970b21c10b2efd38c362b1fa2560cb94bed3eaa8065273d36ce885ee6e8d1cae`
and the linux/amd64 manifest to
`sha256:1891a2421b57cd5f2249f0b44a2720bbca24804e8e579af297a90c876d62659f`.
This proves image-manifest access, not model-download entitlement or inference.
See [the registry receipt](registry-access-20260920.json). The credential-free
image inspection runs on existing H100-node capacity without requesting a GPU.

## Deployed canary and resolved startup failure

The reviewed [canary manifest](canary-20260920.yaml) runs
`fs2-models/cosmos-transfer25-canary-20260920-r2` on the existing
`computeinstance-e00jqs4xxntre6eycf` H100 node. Pod UID:
`0cafd90b-835b-4c08-a575-ce4d31abdd17`; container started
2026-09-20T04:26:33Z. It requests one H100, creates no new node or PVC, has a
two-hour deadline, and has no public Service or gateway route.

The pinned [selected profile](selected-profile-20260920.json) is H100 BF16
diffusion with the vendor-packaged FP8 text encoder, throughput profile
`e74ebba119c8a196dca12cac66aa1b5323291048a855fe02ceb6b664f334c672`.
NIM image version is 1.1.0; the actual bundled model manifest reports release
1.0.0 and SHA-256
`67e0b910a86b7cdd1d91d9dae2058a12e88bc3f4b154073f176df2377bdbca17`.
The manifest's 194 exact file/checksum references are retained, not inferred
from the image tag. Driver observed: 580.173.02; GPU: NVIDIA H100 80GB HBM3.

The first runtime attempt failed with `ManifestDownloadError: builder error`.
An isolated, zero-GPU environment-shape check proved that the injected Secret
contains a trailing LF, not internal whitespace. The registry probe had trimmed
it, while the unmodified NIM launcher had not. The canary launcher now trims
only its process environment, rejects invalid internal/control characters, and
executes the exact upstream Entrypoint/Cmd. It does not change or copy the shared
Secret. See [the retained failure](startup-failure-20260920.json).

The corrected run downloaded the model and safety assets, materialized all 194
profile files, advanced to NIM inference-server initialization at 04:34:30Z,
and became Ready at 04:39:17Z with zero restarts.
Guardrails remain enabled. Successful model downloads prove that the earlier
client-construction error was not an entitlement denial. GPU generation and
source/output alignment plus automated motion/weather checks now passed once;
hosted App/client flows remain separate tests.

Completed diagnostic Pods were removed with UID-bound deletion after retaining
their relevant receipts and selected profile. The running canary was not
deleted. See [cleanup and image-launch evidence](diagnostic-cleanup-20260920.json).

Local checks: 34 tests plus 5 subtests passed for registry auth/digest handling,
credential normalization, the bounded private manifest, the direct-NIM test
runner, and the bounded CPU adapter. These are offline tests, not GPU
qualification. The merged current
backend also passed 75 tests (20 PostgreSQL-dependent skips), followed by 21
passing delegation tests against an isolated local PostgreSQL instance.

## Intended implementation

Prefer NVIDIA's maintained NIM `nvcr.io/nim/nvidia/cosmos-transfer2.5-2b:1.1`, resolving the actual linux/amd64 digest and exact model/profile manifest after authorized access. NVIDIA's [NIM support matrix](https://docs.nvidia.com/nim/cosmos/3.0.0/support-matrix.html) lists a one-H100 80-GB configuration. That is upstream support, **not a local qualification**. Select an explicit BF16 profile if available and establish a full-quality baseline before exploring other precisions or offload.

NVIDIA PAIDF's [pinned Transfer 2.5 walkthrough](https://github.com/NVIDIA/paidf-augmentation/blob/bc5719362492a1e3b40bd7d33b43c46dd89efad5/configs/cookbook/video-data-augmentation/config_video_transfer_CT25_nim.yaml) uses `/v1/infer`, raw base64 input video, nested edge controls and a `b64_video` result. The platform wrapper will use authenticated artifacts, bounded requests and asynchronous operations; it must verify the actual NIM schema/output before publishing those capabilities. Existing Token Factory caption/prompt providers can be reused.

The official source-runtime fallback is `nvidia-cosmos/cosmos-transfer2.5`, commit `2ff49d0717af02057ae79bc75c00fbff9da1b4e7`, with model repository `nvidia/Cosmos-Transfer2.5-2B`, revision `ce8440327c632d8313c3bde69db13b627ba5cae1`. The source repository now reports limited maintenance and recommends Cosmos3; it must not be represented as the same runtime or support policy as the NIM. Source code is Apache-2.0; model weights have NVIDIA Open Model License terms and require authorized access. Auxiliary model/guardrail access must also be checked before using this fallback.

## Initial access checks (historical preflight, 03:02 UTC)

See [the machine-readable preflight](access-preflight-20260920.json).

- Local NGC registry root and Transfer image manifest requests returned HTTP403 without a registry auth challenge. A public CUDA manifest also returned403, so this is not evidence that Transfer alone is unavailable.
- The same Transfer manifest request from an existing Stockholm control-plane Pod returned HTTP401 **with** a registry auth challenge. The target cluster can reach the registry; an authorized in-cluster pull/mirror is a viable next check.
- The platform's approved NGC secrets `fs2-models/fs2-ngc-pull` and `fs2-models/fs2-ngc-runtime` are absent. No NGC environment credential or local Docker NGC auth entry was configured.
- The existing Hugging Face token can read the model README, but a HEAD request for the pinned edge weights returned HTTP403 `GatedRepo`: the account is not on the authorized list. No weights were downloaded and no gate/terms were accepted on the user's behalf.
- The shared backend still uses control-plane digest `sha256:e4319840c1d9378c2942f2e389785917e045af4b5733f93da3b535c3f33e3dad`. No cloud resource, budget, Secret, App grant, runtime or backend release was changed during this preflight.

### Registry-path diagnosis, rechecked 2026-09-20 03:58 UTC

The local403 is **not evidence of an invalid API key or missing Cosmos Transfer
entitlement**. Anonymous HEAD requests to both `/v2/` and the exact Transfer1.1
manifest returned403 from `awselb/2.0` locally, but401 with
`WWW-Authenticate` from `istio-envoy` in an existing Ready Stockholm gateway Pod.
No credential was sent in this recheck and no new workload was created.

This matches the August6 diagnostic in Task Deck record
`archvteams-2370_3deuv9`: confirmed-correct keys and anonymous requests both
failed before registry authentication on the development host. The older
`fs2-nims-baseline-sweep` record documents a successful August20 target-cluster
kubelet pull with `imagePullPolicy: Always`, followed by real NIM baseline
pulls. These are historical observations, not proof that Transfer2.5 is
currently entitled or that the old credentials are reusable.

Continue the NGC preflight **from the authorized Stockholm environment** once
approved credentials are delivered, then resolve and mirror the exact image
digest if permitted. Do not retry the development-host registry path as a key
validity test. The evidence distinguishes a pre-authentication path failure;
it does not establish NVIDIA's precise source-IP/geographic/WAF rule. Image
pull access and NIM model-artifact download access must still be checked
separately. The Hugging Face fallback's gated-account denial is also a separate
finding.

## Credential delivery for activation

The earlier absence of NGC Secrets below is retained as the initial preflight
finding, not the current cluster state. Current canary checks use the existing
operator-provisioned Secret references noted above. Broad platform activation
still requires the platform credential contract and model-download evidence.

Preferred NIM path: supply a **fresh, appropriately entitled NGC API key through the approved secret-delivery process**, not in chat, Git, command-line arguments or Helm values. The existing platform contract requires:

- `fs2-models/fs2-ngc-pull`, type `kubernetes.io/dockerconfigjson`, key `.dockerconfigjson` for `nvcr.io`.
- `fs2-models/fs2-ngc-runtime`, type `Opaque`, key `NGC_API_KEY` for model artifact download.

Follow `catalog/runtime/contracts/runtime-prerequisites.json`: legacy secret copies and historical plaintext keys are not valid rotation sources. Do not import a credential from an old handoff to make this pass. Resolve license/entitlement for the intended hosted service before promotion.

Alternative: request access at [the official Hugging Face model page](https://huggingface.co/nvidia/Cosmos-Transfer2.5-2B) for the account used by the configured token, and verify gated-repository read permission. That enables evaluation of the separate open-source runtime; it does not grant NGC entitlement.

## Resume sequence

1. Recheck access, pin the image/model/profile and all auxiliary artifacts, and inspect the actual `/v1/manifest`, `/v1/metadata`, license and inference contract.
2. Prepare a reproducible bounded wrapper and candidate catalog/projections. Keep the App unqualified and unpublished while testing. Keep conventional startup and snapshot/fast-start claims off.
3. Run a bounded, isolated single-H100 reference canary with guardrails intact. Record hardware, driver, memory, exact output shape/FPS/frame count, timings and motion/weather checks. Do not lower the existing video workflow's quality thresholds to pass this backend.
4. After semantic success, integrate the current backend declaration and typed MCP/artifact contract, publish reviewed website metadata, grant only the isolated canary user, and connect the existing video workflow through attempt-scoped child delegation.
5. Exercise the actual chat upload/preview, human approval and frozen bucket-prefix batch, then the required two unchanged customer-shaped cohorts. Preserve rejected outputs and failures. A direct NIM generation is not evidence that those integrated paths work.

User authorization covers onboarding and the separate sandbox canary. The
original access pause is resolved for the selected NIM image/profile; do not
ask for that same deployment authorization again.

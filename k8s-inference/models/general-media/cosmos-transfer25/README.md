# Cosmos Transfer 2.5 onboarding — access blocked

Requested App identity: `cosmos-transfer2.5-2b`. This is a new, independent video-transfer App, not a rename or replacement of `cosmos3-nano`.

Status as of 2026-09-20: **preflight completed; runtime/model access blocked; not deployed or qualified**. No catalog entry, runtime image digest, GPU qualification, App grant or active route has been fabricated. The existing isolated video workbench and shared backend are unchanged.

## Intended implementation

Prefer NVIDIA's maintained NIM `nvcr.io/nim/nvidia/cosmos-transfer2.5-2b:1.1`, resolving the actual linux/amd64 digest and exact model/profile manifest after authorized access. NVIDIA's [NIM support matrix](https://docs.nvidia.com/nim/cosmos/3.0.0/support-matrix.html) lists a one-H100 80-GB configuration. That is upstream support, **not a local qualification**. Select an explicit BF16 profile if available and establish a full-quality baseline before exploring other precisions or offload.

NVIDIA PAIDF's [pinned Transfer 2.5 walkthrough](https://github.com/NVIDIA/paidf-augmentation/blob/bc5719362492a1e3b40bd7d33b43c46dd89efad5/configs/cookbook/video-data-augmentation/config_video_transfer_CT25_nim.yaml) uses `/v1/infer`, raw base64 input video, nested edge controls and a `b64_video` result. The platform wrapper will use authenticated artifacts, bounded requests and asynchronous operations; it must verify the actual NIM schema/output before publishing those capabilities. Existing Token Factory caption/prompt providers can be reused.

The official source-runtime fallback is `nvidia-cosmos/cosmos-transfer2.5`, commit `2ff49d0717af02057ae79bc75c00fbff9da1b4e7`, with model repository `nvidia/Cosmos-Transfer2.5-2B`, revision `ce8440327c632d8313c3bde69db13b627ba5cae1`. The source repository now reports limited maintenance and recommends Cosmos3; it must not be represented as the same runtime or support policy as the NIM. Source code is Apache-2.0; model weights have NVIDIA Open Model License terms and require authorized access. Auxiliary model/guardrail access must also be checked before using this fallback.

## Access checks actually performed

See [the machine-readable preflight](access-preflight-20260920.json).

- Local NGC registry root and Transfer image manifest requests returned HTTP403 without a registry auth challenge. A public CUDA manifest also returned403, so this is not evidence that Transfer alone is unavailable.
- The same Transfer manifest request from an existing Stockholm control-plane Pod returned HTTP401 **with** a registry auth challenge. The target cluster can reach the registry; an authorized in-cluster pull/mirror is a viable next check.
- The platform's approved NGC secrets `fs2-models/fs2-ngc-pull` and `fs2-models/fs2-ngc-runtime` are absent. No NGC environment credential or local Docker NGC auth entry was configured.
- The existing Hugging Face token can read the model README, but a HEAD request for the pinned edge weights returned HTTP403 `GatedRepo`: the account is not on the authorized list. No weights were downloaded and no gate/terms were accepted on the user's behalf.
- The shared backend still uses control-plane digest `sha256:e4319840c1d9378c2942f2e389785917e045af4b5733f93da3b535c3f33e3dad`. No cloud resource, budget, Secret, App grant, runtime or backend release was changed during this preflight.

## Required operator action

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

User authorization covers onboarding and the separate sandbox canary. The pause is an external access prerequisite, not a request to reauthorize that same work.

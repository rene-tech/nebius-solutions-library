# Cosmos Transfer 2.5 onboarding — isolated canary in progress

Requested App identity: `cosmos-transfer2.5-2b`. This is a new, independent video-transfer App, not a rename or replacement of `cosmos3-nano`.

Status as of 2026-09-20 04:08 UTC: **authenticated image access passed; image inspection and isolated runtime canary in progress; not qualified or published**. No catalog entry, GPU qualification, App grant or active route has been fabricated. The existing isolated video workbench and shared backend are unchanged by this task.

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

User authorization covers onboarding and the separate sandbox canary. The pause is an external access prerequisite, not a request to reauthorize that same work.

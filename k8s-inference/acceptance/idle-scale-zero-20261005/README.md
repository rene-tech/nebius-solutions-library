# Idle App lifecycle, 2026-10-05

Work in progress: **not all legacy Apps are qualified yet**. The user authorized
removing idle model hot floors, while retaining publication, caches, on-demand
execution and active customer work. No provider capacity or customer quotas are
changed by this task.

## Completed changes

- Supported admin desired-state preview/apply removed 19 existing minimum-one
  floors. Maxima, cooldown, cache configuration, publication and runtime identity
  were retained. Active/queued operations were checked before each mutation.
- Existing static scVI/scANVI, Cellpose, SAM2, ACE-Step and both Wan2 deployments
  were adopted by the existing ModelDeployment/KEDA owner, with minimum zero and
  maximum one. Existing runtime images, Service names and cached PVCs survive.
  Old static objects were backed up and removed with exact UID/resourceVersion
  preconditions; generated Deployments are never manually scaled.
- Both full H100 nodes were released for the parallel GROMACS performance work:
  `computeinstance-e00m0hsph76ajt9sdb` and
  `computeinstance-e00p3acr87k9k4mckj`. This does not mean they remain unused while
  that benchmark runs.
- The two **already existing** `wan2-h200-1x` nodes were registered with the
  scheduling envelope and Kueue. Only their additional ResourceFlavor/quota was
  appended; all pre-existing quota entries remain unchanged. No H200 node group,
  instance, reservation, provider limit or Terraform state was modified.
- The redundant legacy lean-route overrides were retired only for adopted Apps,
  retaining the exact selected deployment-runtime records. The API continues to
  publish the original MCP tool names.

## Public cold request evidence

Existing `system/qa` identity only; its concurrency limit stays **2**. Each result
was downloaded and domain-validated, not inferred from readiness. Idempotency
replay returned the same operation. Durations below are **single cold
end-to-end observations**, not measured sustainable capacity or snapshot times.

| App | Public typed MCP operation | End-to-end seconds | Verification |
| --- | --- | ---: | --- |
| scVI | `3ff549ca-ded4-40f9-b120-4dcd1f921d1d` | 53.070 | AnnData result archive, latent output, manifest and input digest; natural return to zero |
| SAM2 | `d1b45c94-6280-4b5a-9187-690549f4128f` | 38.472 | Prompted-image mask, overlay, manifest and exact checkpoint |
| Cellpose | `8175aa3e-07ff-4bcf-9473-1c9da103bc4d` | 42.507 | Mask, overlay and object-count result |
| ACE-Step | `0f3ba8a0-6576-4268-b245-c235a1786f71` | 623.363 | Downloaded WAV, channels/sample rate/duration, idempotency and natural return to zero |
| Wan2 T2V | `e80a7325-669a-4bb9-9583-8154aeaf3999` | 1219.992 | Downloaded MP4, 832x480/61 frames, 50 inference steps and idempotency replay |

ACE-Step completed at 15:52:32 UTC. Its original runtime spent most startup time
loading DiT and its 4B LM, then compiling TorchInductor kernels; no Pod restart.
scVI, Cellpose, SAM2 and ACE-Step were all subsequently observed Cold with zero
Pods. Their ordinary cooldown remained 300 seconds.
Wan2 T2V read 72.6 GB from its existing PVC while NIM materialized the workspace
(approximately 715 seconds), then spent 223.29 seconds initializing the pipeline.
The actual requested generation took 195.26 seconds; the response reported
201.25 seconds including encoding. The observed cold path is usable but slow;
this is not snapshot acceleration. Its natural drain remains to be observed.
Wan2 I2V, remaining workflow variants and MindGuard lifecycle are not yet claimed.

MindGuard now enters the ordinary durable operation lane once actual managed
desired state exists. Merely including its catalog entry does not take over the
old direct preview during reader-first deployment. The updated existing
MindEval worker polls 202, preserves terminal failures/cancellation/timeouts and
validates large result digests without sending API credentials to signed URLs.
It is deployed as `sha256:ee6eea4a54749a75510b4c9362b271c3261c08793dbf0c8f11a813ddd5f09553`;
both replicas and authenticated public workshop catalog passed after rollout.
Its local full suite passed 63 tests with one optional real-Silero fixture
skipped; the production build separately loaded that pinned model. Removing
the final two static MindGuard hot replicas still awaits the coordinated
API successor and public inference qualification.

## Reproducibility and rollback

Scripts in this directory implement the narrow changes. They require the exact
cluster context and explicit private evidence directory; none contains tokens.
`remove_hot_floors.py` previews before apply. `prepare_managed.py` only renders
additive bundles/envelopes. `roll_registration.py` replaces fresh, verified
ConfigMap references without overwriting the image or unrelated environment.
`retire_static_routes.py` removes only explicitly listed redundant overlays.

`register_existing_pool.py` validates the declared existing nodes and allocatable
resources before appending a ResourceFlavor and its exact two-node quota. The
declaration is `models/general-media/wan2-h200-existing-pool.json`; the adjacent
tfvars example records the portable pool shape for the infrastructure owner.
It is a merge fragment, not a standalone deployment or permission to duplicate
the existing node group.

Private before/after objects, admin preview receipts, admission probes and
downloaded results are retained under
`/home/tux/secure-handoff/fs2-idle-scale-zero-20261005/`. These backups contain
operator configuration and are intentionally excluded from Git. Restore a floor
through the admin desired-state API, preserving the current revision; do not
restore an old generated Deployment over the controller's ownership.

The existing QA key temporarily received ten explicitly named model grants for
this cohort. Its original seven grants, owner, limits and non-expiring lifetime
were preserved. `qa_grants.py --restore` removes only this task's additions after
the corresponding requests finish. The tenth grant is `mindeval`, used only to
verify the already-existing caller. No customer API key is used.

## Public website metadata follow-through

The two Sword model metadata entries were added to the exact live public-site
source, not the dirty canonical checkout. Runtime commit `c45eeab` and evidence
commit `cd97b5b` are in the website repository's existing
`scientific-ai-medical-catalog-20260928` worktree. Image
`sha256:af757125d4da9361f0510e9249642f6caf4e227d310e1b0e2261ea0e3ec8ee9b`
replaced only the website image; two replicas, routes, runtime configuration and
prior medical speech metadata were preserved. Both Sword models use the
existing General-purpose AI category, source links and no NVIDIA attribution.

Website tests: 112 unit/integration, 62 actual-MCP-config desktop/mobile tests
(two opposite-configuration tests skipped), and the opt-in live catalog check
passed. The public metadata verifier covered all 47 expected IDs. Live
desktop/mobile inspection retained 45 currently published cards, 26 NVIDIA
marks and zero Other-category sections or horizontal overflow. One **pre-patch**
catalog semantic failure occurred among 978 observed public requests: HTTP 200
after 5.1 seconds did not satisfy fresh catalog semantics. Subsequent checks
passed; the receipt retains that finding rather than claiming uninterrupted
catalog freshness. No unsolicited lead-form email was sent in this scoped
metadata-only release.

## Outstanding acceptance

- Complete both Wan2 public wake/result/drain requests.
- Complete the existing-operation integration for MindGuard; validate both
  typed MCP and direct assess callers, including 202 polling and failures, then
  remove its two idle hot replicas through the same owner.
- Observe natural idle drain for all adopted Apps; record exact deployed image
  and final additive ConfigMap references.
- Restore the temporary QA grants, retain tests and task-card receipts, and
  report untested variants explicitly. No GPU snapshot or cache acceleration
  qualification is inferred from this lifecycle change.

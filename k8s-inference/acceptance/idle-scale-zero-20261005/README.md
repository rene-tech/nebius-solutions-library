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

ACE-Step completed at 15:52:32 UTC. Its original runtime spent most startup time
loading DiT and its 4B LM, then compiling TorchInductor kernels; no Pod restart.
scVI, Cellpose, SAM2 and ACE-Step were all subsequently observed Cold with zero
Pods. Their ordinary cooldown remained300seconds.
Wan2 requests, remaining workflow variants and MindGuard lifecycle are not yet
claimed. MindGuard's old direct endpoint bypasses admission; it must enter the
normal durable operation lane before removing those two static hot replicas.

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

The existing QA key temporarily received nine explicitly named model grants for
this cohort. Its original seven grants, owner, limits and non-expiring lifetime
were preserved. `qa_grants.py --restore` removes only this task's additions after
the corresponding requests finish. No customer API key is used.

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

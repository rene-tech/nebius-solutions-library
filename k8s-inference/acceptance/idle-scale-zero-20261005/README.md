# Idle App lifecycle, 2026-10-05

The user authorized
removing idle model hot floors, while retaining publication, caches, on-demand
execution and active customer work. No provider capacity or customer quotas are
changed by this task.

## Completed changes

- Supported admin desired-state preview/apply removed 19 existing minimum-one
  floors. Maxima, cooldown, cache configuration, publication and runtime identity
  were retained. Active/queued operations were checked before each mutation.
- Existing static scVI/scANVI, Cellpose, SAM2, ACE-Step, both Wan2 and both
  MindGuard deployments
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
  publish the original MCP tool names. All 31 ModelDeployment records now have
  minimum zero: 30 enabled Apps and one unchanged disabled Qwen preview.
- Exact source-backed registration and redeploy ownership are retained in
  [REDEPLOY.md](REDEPLOY.md). The optional root Terraform
  `dynamic_models.retained_registration_file` now preserves all eight exact
  registrations/runtime records, requires their existing declared pools, and
  creates neither bootstrap writes nor static owners. Fifteen mock-plan cases
  and 23 focused Python/Helm tests, plus five root facade/ownership cases, pass. Authoritative pinned-overlay activation
  is still the release owner's responsibility; no Terraform apply is claimed.

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
| Wan2 I2V | `991c58be-164d-434a-9e24-c177f89457fa` | 1246.259 | Downloaded MP4, 832x480/65 frames, 50 steps; test-only frame-count correction and same-result revalidation, described below |

ACE-Step completed at 15:52:32 UTC. Its original runtime spent most startup time
loading DiT and its 4B LM, then compiling TorchInductor kernels; no Pod restart.
scVI, Cellpose, SAM2 and ACE-Step were all subsequently observed Cold with zero
Pods. Their ordinary cooldown remained 300 seconds.
Wan2 T2V read 72.6 GB from its existing PVC while NIM materialized the workspace
(approximately 715 seconds), then spent 223.29 seconds initializing the pipeline.
The actual requested generation took 195.26 seconds; the response reported
201.25 seconds including encoding. The observed cold path is usable but slow;
this is not snapshot acceleration. It returned naturally to Cold at16:35 and its
Pod was absent at16:36. Remaining workflow variants are not qualified by these
single-request tests.

MindGuard now enters the ordinary durable operation lane once actual managed
desired state exists. Merely including its catalog entry does not take over the
old direct preview during reader-first deployment. The updated existing
MindEval worker polls 202, preserves terminal failures/cancellation/timeouts and
validates large result digests without sending API credentials to signed URLs.
It is deployed as `sha256:ee6eea4a54749a75510b4c9362b271c3261c08793dbf0c8f11a813ddd5f09553`;
both replicas and authenticated public workshop catalog passed after rollout.
Its local full suite passed 63 tests with one optional real-Silero fixture
skipped; the production build separately loaded that pinned model. Removing
the final two static MindGuard hot replicas completed after the coordinated
reader-first successor and metadata publication gates. Their first actual cold
request exposed a public polling problem; see below rather than treating model
readiness as acceptance.

### MindGuard cold/warm acceptance and retained failed attempt

Operation `92c2a118-5f8e-457f-ab92-d661a27afc9b` woke MindGuard4B on the existing
L40S1x pool and completed at16:58:51 UTC, 140.237 seconds after admission. Its
reported cold-start component was111.101 seconds. The original typed-MCP polling
client failed once with `MCPError: Server returned an error response` during
concurrent large-job activity. No definitive error cause is inferred from that
timing. The retained partial receipt and separate recovery receipt must remain
distinct: this was **not a clean end-to-end pass**.

A new MCP session fetched the same completed operation and validated the pinned
4B revision, both user-prefix assessments, complete context coverage and
observational output. No inference was resubmitted for recovery. The model then
returned naturally to Cold/zero replicas under its original300-second cooldown.
After the parent verified the exact three-replica successor
`sha256:ea48e96625af79e91a2ebf8fda2403207c637ba3cb35cc0328c1d08a0bdfa622`,
fresh pairs passed during the parallel internal large GROMACS continuation:

| App/path | Operation | Admission-to-verified-result seconds |
| --- | --- | ---: |
| MindGuard4B, cold REST assess + MCP poll | `b41cd860-7753-4b4e-9906-977e96a16c39` | 106.012 |
| MindGuard4B, warm typed MCP | `0b5a364a-07e8-442a-a556-74a1f6703fac` | 21.609 |
| MindGuard8B, cold REST assess + MCP poll | `37314c83-05d5-4b7e-99bf-84cb5aaac2ce` | 138.165 |
| MindGuard8B, warm typed MCP | `707eb0b8-cd17-48c4-a169-d0d3132ea914` | 41.164 |

Both direct cold admissions returned 202; each idempotent retry retained the
same operation. Every response matched the pinned revision and covered both
user-prefix contexts. The synthetic risk transcript flagged the final user turn
and the benign transcript did not; these two fixtures are **not clinical
accuracy qualification**. No client error occurred in these four successor
attempts. Durations include shared-platform queuing, idempotency/result calls
and 5-second polling granularity; they are not pure model latency or p95.
Errors still fail the attempt instead of being silently retried away.

### Wan I2V result and fixture correction

Wan I2V `991c58be-164d-434a-9e24-c177f89457fa` ran on the existing H200 pool.
It was admitted at17:13:51.920 and completed at17:34:21.660 UTC: 1229.740 seconds
server-side. Public discovery, admission, idempotency, polling and checksum-bound
artifact download succeeded. The original client took1246.259 seconds through
download/inspection, then failed a **test-only** hardcoded61-frame assertion
copied from the T2V fixture. That failed receipt remains retained.

The requested four-second I2V output is a valid VP9 MP4, 832x480, 65frames at16fps,
4.0625seconds and899,774bytes. SHA256:
`ca2e8d0fb636dd02cc213730f6d840a1085354223550ff27408dbbb15fb937a5`.
The corrected validator checks requested dimensions/duration and consistent
frame-count/frame-rate, with regressions for both61- and65-frame outputs and
rejection of mismatched size, duration or frames. Read-only recovery of the
**same operation/artifact** passed in3.620 seconds; no second inference was
submitted. That recovery duration is not a cold-start measurement. Neither the
failed assertion nor this lifecycle test is evidence of perceptual video
quality or snapshot acceleration. No full H100 node was used by these final
probes.

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
verify the already-existing caller. All ten additions were removed after the
last test completed; the restored receipt retains the original seven model
grants, maximum concurrency2 and no expiry. No customer API key was used.

## Public website metadata follow-through

The two Sword model metadata entries were added to the exact live public-site
source, not the dirty canonical checkout. Runtime commit `c45eeab` and evidence
commits `cd97b5b` and `40b304d` are in the website repository's existing
`scientific-ai-medical-catalog-20260928` worktree. Image
`sha256:af757125d4da9361f0510e9249642f6caf4e227d310e1b0e2261ea0e3ec8ee9b`
replaced only the website image; two replicas, routes, runtime configuration and
prior medical speech metadata were preserved. Both Sword models use the
existing General-purpose AI category, source links and no NVIDIA attribution.

Website tests: 112 unit/integration, 62 actual-MCP-config desktop/mobile tests
(two opposite-configuration tests skipped), and the opt-in live catalog check
passed. The public metadata verifier covered all 47 expected IDs. Live
desktop/mobile inspection after adoption retained 47 published cards, both Sword
names/source links, 26 NVIDIA marks and zero Other-category sections or horizontal
overflow. One **pre-patch**
catalog semantic failure occurred among 978 observed public requests: HTTP 200
after 5.1 seconds did not satisfy fresh catalog semantics. Subsequent checks
passed; the receipt retains that finding rather than claiming uninterrupted
catalog freshness. No unsolicited lead-form email was sent in this scoped
metadata-only release.

## Final lifecycle and acceptance boundary

At **17:42:04 UTC**, the final read-only receipt confirmed all31 records had
minimum0, observed Cold state, generated Deployment replicas0 and **no App Pods**.
Thirty Apps remain enabled and the pre-existing disabled Qwen preview remains
disabled. Each enabled App has the existing ModelDeployment/ScaledObject owner;
none has a second static writer. Wan was already Cold/replicas0 at17:40:31 but
its old Pod still existed, so GPU release was not called complete at that point.
The final check waited for actual Pod absence. No manual scaling was used.

The active Lynx GROMACS Pod retained UID
`28cba283-f42d-4fe7-aac7-010ba2222f1e` and both containers had zero restarts.
Its customer operation remained running. Temporary QA grants were restored to
the original seven models, maximum concurrency2 and no expiry. The exact
cluster, ownership, model status, image/map bindings and protected Pod receipt
is `lifecycle-final-all-cold-20261005.json` in the private evidence directory.

- These are selected public cold/warm lifecycle fixtures, not a rerun of every
  workflow variant, a sustained concurrency benchmark, clinical validation or
  qualification of existing GPU snapshots/cache acceleration.
- Ordinary root Terraform redeploy retention is source-tested but its new input
  has not been activated in authoritative private tfvars or applied to the live
  deployment. See the exact integration prerequisite in REDEPLOY.md.

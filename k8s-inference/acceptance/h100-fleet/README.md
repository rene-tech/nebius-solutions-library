# Complete H100 fleet restoration

This campaign restores the earlier model fleet alongside the scientific batch
profiles. Its fixed denominator is [expected-models.json](expected-models.json):
**24 public model/profile IDs**, excluding GLM. These are not 24 distinct model
families: ESMFold2/Fast are profiles, and OpenFold3/OpenBind are separately named
runtime implementations. RFdiffusion is fulfilled by its existing batch route.

The earlier B300 cluster served 15 models. Its Terraform replacement initially
deployed only GLM on B300 and Qwen on H100. Cosmos and ten scientific profiles
were added later, but the remaining original models were not migrated. Earlier
H100 twelve-model acceptance and benchmarks are valid for that narrower set,
not evidence of full-fleet completion.

## Work and acceptance

- BioNeMo/structure: Boltz2, GenMol, MolMIM, MSA Search, DiffDock, OpenFold2,
  OpenFold3 and ProteinMPNN.
- Medical/media: Evo2-40B, NV-Reason-CXR-3B, NV-Segment-CT and SDXL.
- Existing services/profiles: preserve Qwen, Cosmos and all ten scientific
  profiles; verify their public routes again after integration.
- Snapshot options: reuse the CUDA+CRIU framework. Test fresh-pod restore with
  new inputs and matched startup measurements. Keep ordinary loading available;
  report unsupported, experimental, qualified and slower-than-normal paths
  separately. Do not claim snapshots from a cached image or model weights.

Each model needs two distinct valid public requests and three measured startup
trials. Record provisioning, image pull, artifact loading, compilation and
restore separately. Scaling to zero is allowed only after verification and must
not remove the model from discovery. Scientific profiles use queued Jobs and
do not require permanent GPU workers.

The retained startup measurements are split by runtime cohort, not lost with
temporary Pods:

- [Ten scientific profiles plus Qwen/Cosmos](../scientific-startup/current/current-h100-20260907.md).
- [Medical/media and Evo2](medical-media/README.md).
- [Bio/structure per-model measurements](bionemo-structure/fragments/), with
  exact clocks, repetitions and separately identified acquisition cases.
- [Standalone OpenFold3 Preview2](openfold3-standalone/README.md).
- Optional snapshot comparisons live in `snapshots/`; installed matching
  evidence is also exposed in the admin inventory API and UI.

Root serializes shared Terraform/control-plane changes; parallel workers own
disjoint runtime directories. No quota/limit increases, B300 changes, host
driver/MIG changes or unrequested hardening are part of this work. The current
cluster and its existing models remain available during the rollout.

## Live acceptance — 7 September 2026, 11:35 UTC

The Terraform rollout now configures **all 24 in-scope model/profile IDs**.
Authenticated `/v1/models` exposes all 14 serving models and
`/v1/scientific-models` exposes all ten scientific profiles. The admin inventory
retains the excluded GLM entry as not deployed rather than hiding it.

- Admin inventory: `https://89.169.99.188/admin/model-inventory`.
- Inventory API: `GET /admin/api/v1/model-inventory`, using an admin session.
- Public inference: `https://89.169.99.188/v1`; MCP: `https://89.169.99.188/mcp`.
- `verify_inventory.py --credential-bundle /path/to/private/access.json
  --output /path/to/new/receipt.json` checks the fixed denominator against both
  public discovery endpoints and the admin API. This is a configuration check,
  not a substitute for inference validation.

All ten scientific profiles passed fresh public requests after the full-fleet
rollout; see [the retained regression](scientific-regression-20260907/README.md).
The serving cohorts retain separate semantic HTTP/MCP acceptance receipts in
[Bio/structure](bionemo-structure/public-h100-20260907.md),
[medical/media](medical-media/README.md), and
[standalone OpenFold3](openfold3-standalone/README.md). The latter's original
request recovered after fixing a missing runtime Pod label; that repair-window
wait must not be reported as model startup. Its separate clean follow-up passed
all four original HTTP/MCP outputs on first attempt.

Qwen, Cosmos and Protenix have selectable shared-filesystem CUDA+CRIU bundles,
three matched native/restore trials and actual production restore plus new-input
proof. [Production receipts](snapshots/production-options-h100-20260907.json)
bind the successful operations to the restored Pod/GPU. Cached container-start
medians are respectively **99.80→51.49 s**, **59.02→28.08 s**, and
**66.25→3.76 s**; they exclude node provisioning/image acquisition and are not
guarantees for arbitrary requests or other GPU/driver combinations.

Serving startup choices are under **Model deployments → model → Model startup
path**. Scientific choices are under **Scientific runs → Scientific model
dispatch policy → Edit policy → Startup for stage**. Normal loading remains
available. Changing a serving snapshot needs the supported drain/zero-replica
cutover; restore the intended enabled state and hot floor afterward. Qwen kept
its original floor of one; Cosmos kept its original floor of zero. No other
model has been permanently scaled down during this campaign.

The expanded snapshot rollout is tracked separately below. Mosaic's current
JAX runtime has a tested CUDA restore incompatibility and retains normal
loading. Release `01a71fee` also fixes the admin state attribution for retained
failed qualification Pods; the public inventory is 24/24 healthy or
intentionally cold, without treating those historical task Pods as serving
replicas.

## Expanded snapshot acceptance — rollout in progress

Main `1c0898a20` adds production renderer/UI support and RFdiffusion's observed
startup metadata. These additional **isolated qualifications are not yet
production-option acceptance**. Each has three normal/fresh-restore pairs and
two original valid inputs per trial:

| Runtime | Native → restore, seconds | Evidence |
| --- | ---: | --- |
| GenMol | 12.198 → 8.654 | [Matched trials](snapshots/genmol-h100-20260907.json) |
| DiffDock | 24.514 → 8.890 | [Matched trials](snapshots/diffdock-h100-20260907.json) |
| NV-Reason-CXR-3B | 83.307 → 55.350 | [Matched trials](medical-media/cxr-snapshot-qualification.json) |
| NV-Segment-CT | 12.782 → 8.498 | [Matched trials](medical-media/segment-snapshot-qualification.json) |
| OpenFold3 Preview2 (standalone) | 37.451 → 9.817 | [Matched trials](openfold3-standalone/snapshot-qualification.json) |
| RFdiffusion | 8.507 → 3.070 | [Matched trials](snapshots/rfdiffusion-h100-20260907.json) |
| ESMFold2 | 20.611 → 13.317 | [Corrected-identity trials](snapshots/esmfold2-h100-20260907.json) |
| ESMFold2-Fast | 19.104 → 11.754 | [Corrected-identity trials](snapshots/esmfold2-fast-h100-20260907.json) |

These are median **container-start-to-application-ready** clocks with shared
files retained and no deliberate cache eviction. They exclude node acquisition
and image pulls, but do not guarantee identical node page-cache residency. One
ESMFold2 native trial ran on a newly autoscaled node: its separate image pull
took 69.252 seconds and its container-ready time was 60.364 seconds. The report
retains that trial and its acquisition events; this is not a controlled
image-cold comparison. The standalone OpenFold3 result does not qualify the
separate OpenBind profile.

Initial public ESMFold2/Fast snapshot attempts restored CUDA and produced valid
structures, but the production confidence collector correctly rejected their
captured weights identity. Corrected captures now preserve the image-locked
weights revision separately from the catalog source revision. Both replacement
qualifications pass three fresh restores and both original inputs through the
production confidence collector, using the original five-capability restore
environment. Normal public policies were restored; earlier isolated results
are preserved separately and are not proof of a successful production option.
The corrected bundles still need the final Terraform rollout and public smoke
tests.

Proteina and BoltzGen remain normal-load runtimes: the
[exact-source assessment](snapshots/proteina-boltzgen-model-only-assessment-20260907.md)
shows that safe reusable snapshots require stage-specific persistent workers
which separate model state from each request. Fast native loaders and paths
whose measured restore is slower should not be presented as acceleration.

## Initial state — 7 September 2026 (historical)

The public admin API reports two serving models (Qwen hot, Cosmos cold) and ten
qualified scientific profiles. The twelve other required standalone services
are **not deployed**, not merely scaled to zero. No completion is claimed by
this plan. Cohort directories will retain actual tests and deployment evidence.

Task Deck parent: `fs2-full-h100-fleet-restore-snapshots-r20260907`.

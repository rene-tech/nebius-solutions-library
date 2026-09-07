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

ESMFold2/Fast, GenMol, CXR and RFdiffusion snapshot work remains in progress;
it is not described as a production option until fresh-Pod restore and new-input
validation pass. Mosaic's current JAX runtime has a tested CUDA restore
incompatibility and retains normal loading. The admin state fix for retained
failed qualification Pods is awaiting the next release; current successful
inference receipts are not invalidated by those historical task Pods.

## Initial state — 7 September 2026 (historical)

The public admin API reports two serving models (Qwen hot, Cosmos cold) and ten
qualified scientific profiles. The twelve other required standalone services
are **not deployed**, not merely scaled to zero. No completion is claimed by
this plan. Cohort directories will retain actual tests and deployment evidence.

Task Deck parent: `fs2-full-h100-fleet-restore-snapshots-r20260907`.

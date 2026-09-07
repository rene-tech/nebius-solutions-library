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

Root serializes shared Terraform/control-plane changes; parallel workers own
disjoint runtime directories. No quota/limit increases, B300 changes, host
driver/MIG changes or unrequested hardening are part of this work. The current
cluster and its existing models remain available during the rollout.

## Initial state — 7 September 2026

The public admin API reports two serving models (Qwen hot, Cosmos cold) and ten
qualified scientific profiles. The twelve other required standalone services
are **not deployed**, not merely scaled to zero. No completion is claimed by
this plan. Cohort directories will retain actual tests and deployment evidence.

Task Deck parent: `fs2-full-h100-fleet-restore-snapshots-r20260907`.

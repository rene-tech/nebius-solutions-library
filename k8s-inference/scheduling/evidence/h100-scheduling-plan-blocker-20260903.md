# Live H100 scheduling plan: reproduction of the blocker

Plan only. No apply, no cluster mutation, no B300. The one live interaction was
a read-only `kubectl get nodes`.

## Why this exists

The workloads apply that would create the `fs2-serve-artifact-store` Secret and
the scientific scheduling ConfigMaps cannot run, so `scientific_batch` cannot be
switched on. This records exactly why, measured rather than assumed, so the fix
is a decision rather than an investigation.

## Setup

| Fact | Value |
| --- | --- |
| Code | `origin/main` `97f9491a` |
| Project / region | `project-e00rene` / `eu-north1` |
| Cluster | `k8s-inference-h100`, `run_id` `r927c465c6d` |
| Workloads state | copy of the retained run state, serial 468, 73 resources |
| Inputs | the retained generated `workloads.tfvars.json` and the live `terraform.tfvars` |
| Command | `terraform plan -input=false -lock=false -refresh=false` |

State was copied to scratch first; the retained files were read, never used as a
plan backend.

## The general CPU pool itself is clean

Against the retained infrastructure state, adding the pool is
**34 no-op, 1 create, 0 destroy, 0 replace** — only
`nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"]`. With the
`32vcpu-128gb` reference-pool resize it is 1 create plus 1 **in-place** update
(`replace_paths` null). Neither of those is what blocks the platform.

## What actually fails

The workloads stage fails three preconditions with the retained generated
tfvars:

1. `stages/workloads/queue.tf:354`, `terraform_data.academic_lane_ownership` —
   `core_pool_capacity` is empty while the reference-data plane is enabled.
2. `modules/kueue-scheduling/main.tf:1218` — the retained `service_classes` do
   not match the current contract (every class must be `restartable`, carry a
   `default_local_queue` that exists, and a bounded description).
3. `var.scientific_batch.execution_map.models` is an empty tuple under schema
   `fs2-serve.nebius.ai/scientific-execution-map/v3`.

All three are **stale generated input**, not code. `workloads.tfvars.json` is
written by the root facade, and the retained copy predates the current contract.

## Regenerating it hits the real gate

Planning the root with the live `terraform.tfvars` fails at
`main.tf:255`: the reference-data plane is enabled, so
`deployment.scheduling.budget_core_resources` must be on, because Kueue drops
cpu and memory before admission otherwise.

Turning it on then fails at `main.tf:304`: core admission requires a **measured**
per-node cpu and memory capacity for every selected accelerator pool, and the
value must carry `evidence` (`pool_id`, `source`, `captured_at`,
`payload_sha256`). That requirement is deliberate — it is what stops an invented
number from becoming a quota.

Both selected pools are reported missing: `h100-1x`, `h100-reserved-8x`.

## Measured capacity, and the one value that cannot be measured

Read from the live cluster on 2026-09-03, read-only:

| Pool | Nodes | Allocatable CPU | Allocatable memory | memory_mib |
| --- | --- | --- | --- | --- |
| `h100-reserved-8x` | 2 Ready capacity-block nodes | `127900m` | `1610494004Ki` / `1610494024Ki` | 1572748 |

Node identifiers are deliberately omitted: the public export forbids opaque
Nebius resource IDs. The digest below is taken over the captured payload, which
does include them, so the measurement stays checkable against the cluster
without publishing private identifiers.

`payload_sha256` of the captured allocatable payload:
`945b717df461dae8d21326ee6e5f5bd435ed9bce8cbae3e325b19e33d19069a9`. The same
pair appears independently in `reference-data/placement-contract.json`, which
corroborates it.

`h100-1x` is `min_nodes = 0` and preemptible, and no node of that pool is
running, so it has **no allocatable to measure**. There is no recorded
measurement for it anywhere in the run state or the repository. Supplying a
number for it would be exactly the invented quota the evidence requirement
exists to prevent.

## The decision this needs

One of:

1. Briefly run one preemptible `h100-1x` node to capture its allocatable, then
   record both pools with evidence. This is a live mutation and needs explicit
   approval.
2. Drop `h100-1x` from the deployment's selected pools, leaving
   `h100-reserved-8x` as the only accelerator pool, which is already measured.
3. Leave the reference-data plane disabled, which is not viable for AlphaFold 3.

Once one is chosen, the root regenerates `workloads.tfvars.json` and the two
remaining failures (service classes, execution map) are follow-on input updates
surfaced by that regeneration.

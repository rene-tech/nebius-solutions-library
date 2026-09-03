# Live H100 scheduling plan: blocker, measurement and fix

Plan only. No `terraform apply`, no merge, no B300. The only live mutations were
one preemptible node scaled from zero for a capacity measurement and returned
to zero, both authorized.

## Why this exists

The workloads apply that creates the `fs2-serve-artifact-store` Secret and the
scientific scheduling ConfigMap could not run, so `scientific_batch` could not
be switched on. This records what actually blocked it, what was measured rather
than assumed, and what changed.

## Setup

| Fact | Value |
| --- | --- |
| Code | `origin/main` `97f9491a` plus this branch |
| Project / region | `project-e00rene` / `eu-north1` |
| Cluster | `k8s-inference-h100`, `run_id` `r927c465c6d` |
| Workloads state | copy of the retained run state, serial 468, 73 resources |
| Command | `terraform plan -input=false -lock=false -refresh=false` |

State was copied to scratch; the retained files were read, never used as a plan
backend.

## The defect that blocked the plan

The root emitted `core_pool_capacity` as a **sibling** of the workloads
`scheduling` object, but the stage declares it **inside** that object and reads
`var.scheduling.core_pool_capacity`. Terraform does not fail on an undeclared
variable in a var-file; it warns and drops the value. The stage therefore saw an
empty map while the facade believed it had supplied the capacity, and
`queue.tf` refused to admit core-requesting work:

```
Warning: Value for undeclared variable
The root module does not declare a variable named "core_pool_capacity"
```

Fixed by emitting it inside `scheduling`, where the contract declares it. It is
silent when wrong, so it is covered by
`test_core_pool_capacity_reaches_the_variable_the_stage_reads`.

## Measured accelerator capacity

Core admission requires a measured per-node capacity, with evidence, for every
selected accelerator pool. Both were read from the live cluster with
`kubectl get nodes -o json`, `.status.allocatable`:

| Pool | Nodes read | CPU | memory_mib | `payload_sha256` |
| --- | --- | --- | --- | --- |
| `h100-reserved-8x` | 2 Ready capacity-block nodes | `127900m` | 1572748 | `945b717df461dae8d21326ee6e5f5bd435ed9bce8cbae3e325b19e33d19069a9` |
| `h100-1x` | 1 node, scaled from zero for this measurement | `15900m` | 190072 | `854bbf9643dbf38f4978b3e98ea7a1f02ea6efce798a39e564e9b556472cc777` |

Captured `2026-09-03T12:02:45Z` and `2026-09-03T12:02:16Z`. Node identifiers are
omitted deliberately: the public export forbids opaque Nebius resource IDs, and
the digests are taken over the payloads that do contain them, so each
measurement stays checkable against the cluster. The `h100-reserved-8x` pair is
independently corroborated by `reference-data/placement-contract.json`.

`h100-1x` had no node to read, being `min_nodes = 0` and preemptible. Rather
than infer it from the preset, one node was scaled up by scheduling a single
`pause` Pod that requested `nvidia.com/gpu: 1` with the pool's selector and
toleration — the pool's own scale-from-zero path, which also exercised it. The
autoscaler reported `pod triggered scale-up: 0->1 (max: 2)`, the node reached
Ready, allocatable was read, and the Pod was deleted so the pool returns to
zero. No quota or limit was changed and B300 was untouched.

Keeping `h100-1x` matters beyond this measurement: it is the scale-from-zero
pool that BoltzGen and Proteina-Complexa both list in `compatible_pool_ids`, and
the handover requires both hot capacity and scale-from-zero preemptibles to
work.

## Input updates

Applied to the live customer tfvars (backed up alongside it):

* `scheduling.budget_core_resources = true`, required once the reference-data
  plane is on, because Kueue drops cpu and memory before admission otherwise.
* `scheduling.accelerator_schedulable_capacity` for both pools, with the
  evidence above.
* `dynamic_models.handoff_receipt` refreshed to the recomputed
  `sha256:867a1343…35ba`; the retained value predated the current catalog.

The stale generated `workloads.tfvars.json` needed no hand editing beyond this:
regenerating it from the root supplied the current `service_classes` shape by
itself.

## Plan results

Infrastructure, general CPU pool added to the retained state:
**34 no-op, 1 create, 0 destroy, 0 replace** — only the node group. With the
`32vcpu-128gb` reference-pool resize, 1 create plus 1 **in-place** update
(`replace_paths` null).

Workloads, regenerated inputs against the retained state, with
`scientific_batch.enabled = false`: **0 errors**, 105 resources,
**68 no-op, 12 create, 19 update, 3 replace, 0 destroy-only**. The creates
include exactly what the platform is waiting on:

```
kubernetes_secret_v1.scientific_artifact_store[0]
kubernetes_config_map_v1.scientific_scheduling_contract
module.reference_data[0].kubernetes_config_map_v1.placement
```

The three replacements are immutable objects being rewritten in place
(`model_controller_*` ConfigMaps, the bootstrap Job, the reference-data tools
ConfigMap and pipeline manifest). Nothing is deleted without being recreated.

## What still gates `scientific_batch.enabled = true`

One thing, and it is not in this branch:

```
scientific_batch.enabled requires a non-empty schema-v3 execution map
```

A schema-v3 entry needs a real `execution_identity_sha256`, `runtime_artifacts`
with content digests, and a `localization_receipt_digest` per artifact. Those
come from the runtime-image, artifact-localization and adapter workstreams.
Inventing them would be the same failure the capacity evidence requirement
exists to prevent, so the map is left empty and the gate left closed.

The consequence is that the platform unblocks in two steps rather than one:
apply now with `scientific_batch.enabled = false` to create the artifact-store
Secret and the scheduling ConfigMap, then flip the batch gate once a real
execution map exists. The Secret is gated on `scientific_artifacts.enabled`,
not on the batch flag, so step one delivers it.

## Sandbox note

The workloads plan also requires `nvcrio_dockerconfigjson`, which the operator
supplies from `FS2_..._NVCR_DOCKERCONFIGJSON` at apply time. A throwaway
`{"auths":{}}` was used for the plan only; it is not committed and not applied.

# General CPU batch pool and Kueue lane

An elastic, entirely tfvars-driven CPU pool for scientific preprocessing and
aggregation that does not need the dedicated reference-data nodes, plus the
Kueue lane that admits work onto it.

The lane exists so scientific aggregation never waits behind a GPU queue and
never lands on the storage-attached nodes that AlphaFold 3 raw preprocessing
depends on. BindCraft reaches this backing through an academic namespace-local
queue; keeping both general classes separate from the reference-data plane is
enforced rather than merely documented.

## Configuring it

```hcl
deployment = {
  cpu_pools = {
    "general-cpu-8x" = {
      platform      = "cpu-d3"
      preset        = "8vcpu-32gb"
      capacity_type = "preemptible"
      autoscaling   = { min_nodes = 0, max_nodes = 4 }
      # Measured capacity of one node after the Kubernetes and DaemonSet
      # reserve. There is no preset-derived default: the lane's quota is
      # exactly this multiplied by the node ceiling, so a guess here would be
      # a quota that does not match the hardware.
      schedulable_capacity = {
        cpu_millicores        = 7000
        memory_mib            = 28672
        ephemeral_storage_mib = 114688
      }
    }
  }

  scheduling = {
    general_cpu = {
      cluster_queue = "general-cpu"
      local_queue   = "general-cpu"
      # The ordinary class defaults to fs2-models. When academic execution is
      # enabled, Terraform derives academic-cpu and academic-general-cpu in the
      # claim namespace over this exact ClusterQueue/flavor/pool.
      namespace = "fs2-models"
    }
  }
}
```

`fixed_nodes = N` replaces `autoscaling` for a pinned pool. Exactly one of the
two is required: a pool that declared both, or neither, would have no single
answer for how many nodes the lane may count on.

Custom `node_labels` are checked against the Kubernetes qualified-name grammar
rather than a single loose pattern — at most one `/`, a DNS-subdomain prefix of
at most 253 characters, a name and value of at most 63 each, and the reserved
`accelerator.`, `capacity.`, `lifecycle.`, `storage.` and `workload.`
`fs2.nebius/` prefixes refused. A label the API would reject must fail the plan,
not the node group's first registration.

## In-queue preemption

The lane's ClusterQueue sets `withinClusterQueue: LowerPriority` (and
`reclaimWithinCohort: Never`, since it joins no cohort). Without it Kueue
defaults to `Never`, and a presentation or interactive CPU stage would wait
behind admitted bulk work on the only lane that can run it, whatever
`WorkloadPriorityClass` it carries. Outside a cohort, in-queue displacement is
the only mechanism this queue has.

## What v1 deliberately does not do

Two limits are real constraints of the consumers, not simplifications:

* **One execution namespace per class.** The assembled scheduling contract maps
  each class to one uniquely named LocalQueue, and the controller freezes every
  stage of a run into one namespace. Reusing a physical lane in another
  namespace therefore requires a second class and LocalQueue; `academic-cpu`
  is that explicit projection for BindCraft.
* **One pool per class.** Kueue reports the ResourceFlavor it admitted through.
  A flavor whose selector spanned several pools could not tell a consumer which
  node group actually ran a stage, so the flavor selector pins exactly one pool
  and carries its `capacity.fs2.nebius/pool-id` label.

## Ownership

| Object | Owner |
| --- | --- |
| `general-cpu` ResourceFlavor, ClusterQueue, and `fs2-models` LocalQueue | this lane |
| `academic-general-cpu` LocalQueue in `fs2-academic-poc` | the scheduling workstream |
| `reference-data-cpu` flavor and queue, `reference-data` LocalQueue | the reference-data plane |
| the assembled scheduling contract and its ConfigMap | the scheduling workstream |

This lane contributes one canonical `general-cpu` entry under
`cpu_classes_schema` `fs2-serve.nebius.ai/cpu-stage-classes/v1`, its digest in
`cpu_class_digests`, and the ownership facts describing the queues it created.
`scheduling/integration/general-cpu-class-entry.fixture.json` is the exact entry
the assembler merges; the `.invalid-capacity.` fixture beside it is an entry the
canonical gate must reject.

The scheduling assembler derives `academic-cpu` by changing only `namespace`
and `local_queue` from that canonical entry. Its ClusterQueue, ResourceFlavor,
pool resolution, selectors, tolerations, and measured capacity remain byte-for-
byte equivalent. The derived LocalQueue carries no model or tenant routes; the
catalog stage selects it only through `placement.class = "academic-cpu"`.

## Global core admission

Kueue's manager configuration excludes `cpu` and `memory` from admission by
default. A ClusterQueue that declares cpu or memory quota while they are
excluded is **silently inert**: the quota is filtered out and admission ignores
it. `deployment.scheduling.budget_core_resources` turns core admission on, and
enabling it requires measured per-node capacity, with evidence, for every
selected accelerator pool — because once cpu and memory count, an accelerator
queue without core capacity stops admitting GPU work.

## Rebinding the LocalQueue

`LocalQueue.spec.clusterQueue` is immutable in Kueue 0.17.8. The binding
identity is held in Terraform state, so changing
`scheduling.general_cpu.cluster_queue` plans a **replacement** rather than an
in-place update the API server rejects. Replacement briefly removes the queue:
stop submitting into `general-cpu`, let admitted Workloads finish, then apply.

## Reference pool sizing

The reference-data pool is a separate owner and a separate size. An
`8vcpu-32gb` node can stage, decompress and hash the databases, but one
AlphaFold 3 raw-input pod needs 16 CPU and 64 GiB
(`reference-data/model-requirements.json`,
`models.alphafold3.preprocessing_capacity`) and a pod is never split across
nodes. The shipped examples therefore use `32vcpu-128gb` with a matching
`queue.nominal_cpu`/`nominal_memory`, while the bulk stager stays at 6 CPU /
24 GiB.

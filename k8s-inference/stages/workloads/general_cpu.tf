# General CPU admission lane, workload side.
#
# Ownership, stated once so it stays true:
#   * this file owns the general-cpu ResourceFlavor, ClusterQueue and the
#     LocalQueue of its single execution namespace, and nothing else;
#   * the reference-data plane owns its own flavor, queue and LocalQueue, and is
#     read here only to prove the general lane reuses none of them;
#   * the scheduling workstream is the sole producer of the reference-data
#     class, the sole assembler of the scheduling contract, and the sole owner
#     of the ConfigMap that publishes it.
#
# What this file contributes is one canonical general-cpu class entry, its
# digest, and the ownership facts describing the queues it created. The
# assembler merges that entry as-is; nothing here writes a document, a
# ConfigMap, a digest of someone else's bytes, or a chart value that would
# claim a contract exists before its owner has published one.

locals {

  general_cpu_pool_contract = var.general_cpu_pools == null ? {
    schema        = "fs2-serve.nebius.ai/general-cpu-pools/v1"
    node_selector = {}
    taint         = { key = "", value = "", effect = "NoSchedule" }
    pools         = {}
    } : {
    schema        = var.general_cpu_pools.schema
    node_selector = var.general_cpu_pools.node_selector
    taint         = var.general_cpu_pools.taint
    pools         = var.general_cpu_pools.pools
  }

  general_cpu_lane_input = {
    enabled             = var.general_cpu_lane.enabled && var.general_cpu_pools != null
    cluster_queue       = var.general_cpu_lane.cluster_queue
    local_queue         = var.general_cpu_lane.local_queue
    resource_flavor     = var.general_cpu_lane.resource_flavor
    queueing_strategy   = var.general_cpu_lane.queueing_strategy
    fair_sharing_weight = var.general_cpu_lane.fair_sharing_weight
    namespace           = var.general_cpu_lane.namespace
  }

  # Identities only. The reference-data class itself is produced and assembled
  # by its own owner; this lane reads these to prove it reuses none of them.
  general_cpu_reference_lane = !var.reference_data.enabled ? null : {
    resource_flavor = var.reference_data.queue.resource_flavor
    cluster_queue   = var.reference_data.queue.cluster_queue
    local_queue     = var.reference_data.queue.local_queue
    namespace       = var.reference_data.namespace
  }

  # Every accelerator ClusterQueue's covered resources, read from the queue
  # renderer this stage already instantiates. Once core budgeting is on, Kueue
  # requires a queue to cover every resource its workloads request, so an
  # accelerator queue that budgets only GPUs would stop admitting.
  accelerator_queue_covered_resources = {
    for queue_name, manifest in module.kueue_scheduling.contract.cluster_queues :
    queue_name => toset(flatten([
      for group in manifest.spec.resourceGroups : group.coveredResources
    ]))
  }
  accelerator_queues_missing_core = sort([
    for queue_name, covered in local.accelerator_queue_covered_resources : queue_name
    if !contains(covered, "cpu") || !contains(covered, "memory")
  ])

  general_cpu_contract           = module.general_cpu_scheduling.contract
  general_cpu_enabled            = local.general_cpu_contract.enabled
  general_cpu_class_entry_sha256 = try(local.general_cpu_contract.cpu_class_digests["general-cpu"], null)

}

module "general_cpu_scheduling" {
  source = "../../modules/general-cpu-scheduling"

  pool_contract       = local.general_cpu_pool_contract
  lane                = local.general_cpu_lane_input
  reference_data_lane = local.general_cpu_reference_lane
  labels              = local.common_labels
  annotations = {
    # The shape of the class entries this producer contributes, which is the
    # class schema, not the workload-requirements document beside it.
    "fs2-serve.nebius.ai/cpu-stage-classes-schema" = "fs2-serve.nebius.ai/cpu-stage-classes/v1"
  }
}

resource "kubernetes_manifest" "general_cpu_flavor" {
  for_each = local.general_cpu_enabled ? {
    (var.general_cpu_lane.resource_flavor) = local.general_cpu_contract.manifests.resource_flavor
  } : {}

  manifest = each.value

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-general-cpu"
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "general_cpu_cluster_queue" {
  for_each = local.general_cpu_enabled ? {
    (var.general_cpu_lane.cluster_queue) = local.general_cpu_contract.manifests.cluster_queue
  } : {}

  manifest = each.value

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-general-cpu"
  }

  depends_on = [kubernetes_manifest.general_cpu_flavor]
}

# LocalQueue.spec.clusterQueue is immutable in Kueue 0.17.8. Keeping the binding
# identity in Terraform state makes a changed binding plan a replacement instead
# of an in-place API update the server rejects. Replacement briefly removes the
# queue, so an operator drains it first: stop submitting into general-cpu, let
# admitted Workloads finish, then apply.
resource "terraform_data" "general_cpu_local_queue_binding" {
  for_each = local.general_cpu_contract.manifests.local_queues

  input = {
    namespace     = each.value.metadata.namespace
    cluster_queue = each.value.spec.clusterQueue
  }

  triggers_replace = [
    each.value.metadata.namespace,
    each.value.spec.clusterQueue,
  ]
}

# The LocalQueue of the single execution namespace. A Job is admitted through
# the queue in its own namespace, so a tenant cannot submit into another lane.
resource "kubernetes_manifest" "general_cpu_local_queue" {
  for_each = local.general_cpu_contract.manifests.local_queues

  manifest = each.value

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-general-cpu"
  }

  lifecycle {
    replace_triggered_by = [terraform_data.general_cpu_local_queue_binding[each.key]]
  }

  depends_on = [kubernetes_manifest.general_cpu_cluster_queue]
}

# The pool contract arrives as a staged output from a previous apply. A stale
# or cross-cluster file would name node groups in another project or region,
# and every Kueue object rendered from it would select nodes that do not
# exist here. Checked before any resource is created, against the target this
# stage was invoked for.
resource "terraform_data" "general_cpu_pool_target_binding" {
  input = var.general_cpu_pools == null ? null : {
    project_id = var.general_cpu_pools.project_id
    region     = var.general_cpu_pools.region
    schema     = var.general_cpu_pools.schema
  }

  lifecycle {
    precondition {
      condition = var.general_cpu_pools == null || (
        var.general_cpu_pools.project_id == var.target_contract.project_id &&
        var.general_cpu_pools.region == var.target_contract.region
      )
      error_message = "The general CPU pool contract was produced for project ${try(var.general_cpu_pools.project_id, "none")} in ${try(var.general_cpu_pools.region, "none")}, but this stage targets ${var.target_contract.project_id} in ${var.target_contract.region}. A staged output from another cluster would render Kueue objects for node groups that do not exist here."
    }
  }
}

resource "terraform_data" "general_cpu_contract" {
  input = {
    enabled            = local.general_cpu_enabled
    cpu_classes_schema = local.general_cpu_contract.cpu_classes_schema
    cpu_classes        = local.general_cpu_contract.cpu_classes
    # The rendered Kueue objects, so their policy is reviewable and assertable
    # without standing up the stage.
    manifests = local.general_cpu_contract.manifests
    capacity  = local.general_cpu_contract.capacity
    pool_ids  = local.general_cpu_contract.pool_ids
    namespace = local.general_cpu_contract.execution_namespace
    elasticity = {
      elastic         = local.general_cpu_contract.elastic
      scale_from_zero = local.general_cpu_contract.scale_from_zero
    }
    # What this producer contributes to the scheduling workstream: one
    # canonical class entry, its digest, and the ownership facts for the queues
    # created here. The assembler merges the entry as-is and describes these
    # queues without becoming their owner.
    scheduling_contribution = {
      cpu_classes_schema  = local.general_cpu_contract.cpu_classes_schema
      cpu_classes         = local.general_cpu_contract.cpu_classes
      cpu_class_digests   = local.general_cpu_contract.cpu_class_digests
      external_lane_facts = local.general_cpu_contract.external_lane_facts
      assembled_by        = "scheduling workstream (sole ConfigMap owner)"
      rendered_by         = "modules/general-cpu-scheduling"
      consumer_verification = join(" ", [
        "The controller hashes the raw bytes it mounted from the scheduling",
        "ConfigMap and compares them with the digest its owner injected before",
        "enabling writes; an absent class or a digest mismatch is a refusal.",
      ])
    }
    cohort = null
  }

  depends_on = [terraform_data.general_cpu_pool_target_binding]

  lifecycle {
    # This producer contributes exactly one class and owns exactly the queues
    # that class names. It never contributes another owner's class.
    precondition {
      condition = (
        length(local.general_cpu_contract.cpu_classes) == (local.general_cpu_enabled ? 1 : 0) &&
        !contains(keys(local.general_cpu_contract.cpu_classes), "reference-data")
      )
      error_message = "The general CPU producer contributes only the general-cpu class; the reference-data class belongs to its own owner."
    }

    # The assembler published this entry unaltered. The producer computes the
    # digest of what it handed over; the assembler computes the digest of what
    # it emitted. If they differ, the class in the ConfigMap is not the class
    # this module rendered, and a consumer freezing it would freeze something
    # nobody produced.
    precondition {
      condition = !local.general_cpu_enabled || (
        try(module.kueue_scheduling.contract.cpu_class_digests["general-cpu"], null) ==
        local.general_cpu_class_entry_sha256 &&
        module.kueue_scheduling.contract.cpu_classes_schema ==
        local.general_cpu_contract.cpu_classes_schema
      )
      error_message = "The assembled scheduling contract must carry this producer's general-cpu entry unaltered, under the same class contract version."
    }

    # BindCraft aggregation is the bound consumer of this class; it must resolve
    # to a complete, exact placement or not resolve at all.
    precondition {
      condition = !local.general_cpu_enabled || (
        length(local.general_cpu_contract.pool_ids) > 0 &&
        length(local.general_cpu_contract.cpu_classes["general-cpu"].node_selector) > 0 &&
        length(local.general_cpu_contract.cpu_classes["general-cpu"].tolerations) > 0 &&
        local.general_cpu_contract.cpu_classes["general-cpu"].namespace != null &&
        local.general_cpu_contract.cpu_classes["general-cpu"].schedulable_capacity.cpu_millicores > 0
      )
      error_message = "An enabled general-cpu class must carry eligible pools plus a complete node selector, toleration set, execution namespace and per-node capacity so resolution is exact and fail-closed."
    }

    precondition {
      condition = !local.general_cpu_enabled || !var.reference_data.enabled || (
        local.general_cpu_contract.cpu_classes["general-cpu"].cluster_queue != var.reference_data.queue.cluster_queue &&
        local.general_cpu_contract.cpu_classes["general-cpu"].resource_flavor != var.reference_data.queue.resource_flavor &&
        local.general_cpu_contract.cpu_classes["general-cpu"].namespace != var.reference_data.namespace
      )
      error_message = "The general CPU lane must stay disjoint from the reference-data lane in flavor, ClusterQueue and namespace."
    }
  }
}

# Global core admission is a separate, deliberately blocking gate.
#
# Kueue filters cpu and memory out of admission by default, so this lane's
# quotas would be decoration unless core budgeting is on. Turning it on makes
# core resources part of every ClusterQueue's arithmetic, so an accelerator
# queue without core capacity would stop admitting GPU work. Both conditions are
# checked here, before any infrastructure is mutated, and neither is inferred.
resource "terraform_data" "general_cpu_core_admission" {
  input = {
    required                        = local.general_cpu_enabled
    budget_core_resources           = var.budget_core_resources
    accelerator_queues_missing_core = local.accelerator_queues_missing_core
    quota_enforced                  = local.general_cpu_enabled && var.budget_core_resources
  }

  lifecycle {
    # Kueue drops excluded resources from admission arithmetic, so a cpu/memory
    # quota published while core budgeting is off is not a smaller quota, it is
    # no quota at all. Refuse to render it rather than ship decoration.
    precondition {
      condition     = !local.general_cpu_enabled || var.budget_core_resources
      error_message = "The general CPU lane budgets cpu and memory, which Kueue ignores while core resources are globally excluded. The facade enables foundation core budgeting wherever a general CPU pool exists; apply the foundation stage before the workloads stage."
    }

    # Turning core budgeting on makes cpu and memory part of every queue's
    # arithmetic, so an accelerator queue without core capacity would stop
    # admitting GPU work. Fail closed instead of breaking a live lane.
    precondition {
      condition     = !var.budget_core_resources || length(local.accelerator_queues_missing_core) == 0
      error_message = "Global core budgeting is required by the general CPU lane, but these accelerator ClusterQueues declare no cpu/memory capacity and would stop admitting once it is on: ${join(", ", local.accelerator_queues_missing_core)}. Give them exact core capacity in one combined scheduling contract before enabling a general CPU pool."
    }
  }
}

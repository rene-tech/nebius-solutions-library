variables {
  pools = {
    burst = {
      flavor_name   = "example-burst"
      resource_name = "example.com/accelerator"
      capacity      = 4
    }
    reserved = {
      flavor_name   = "example-reserved"
      resource_name = "example.com/accelerator"
      capacity      = 8
    }
    partitioned = {
      flavor_name   = "example-partitioned"
      resource_name = "example.com/accelerator-slice"
      capacity      = 2
    }
  }

  default_queue = {
    cluster_queue_name = "inference-accelerators"
    local_queue_name   = "inference-models"
    namespace          = "fs2-models"
    queueing_strategy  = "BestEffortFIFO"
  }

  scheduling = {
    cohort = {
      enabled             = true
      name                = "inference-shared"
      fair_sharing_weight = 1
    }
    cluster_queues = {
      inference-accelerators = {
        namespace                          = "fs2-models"
        queueing_strategy                  = "BestEffortFIFO"
        fair_sharing_weight                = 1
        admission_fair_sharing             = true
        fair_share_precedence_acknowledged = true
        flavor_order                       = ["reserved", "burst", "partitioned"]
        pool_quotas = {
          reserved    = { nominal_quota = 2, lending_limit = 0 }
          burst       = { nominal_quota = 0 }
          partitioned = { nominal_quota = 0 }
        }
        preemption = {
          reclaim_within_cohort = "LowerPriority"
          within_cluster_queue  = "LowerPriority"
        }
      }
      customer-batch = {
        namespace                          = "fs2-models"
        queueing_strategy                  = "BestEffortFIFO"
        fair_sharing_weight                = 4
        admission_fair_sharing             = true
        fair_share_precedence_acknowledged = true
        flavor_order                       = ["reserved", "burst", "partitioned"]
        flavor_fungibility = {
          when_can_borrow  = "TryNextFlavor"
          when_can_preempt = "TryNextFlavor"
          preference       = "PreemptionOverBorrowing"
        }
        pool_quotas = {
          reserved    = { nominal_quota = 2, borrowing_limit = 4 }
          burst       = { nominal_quota = 1, borrowing_limit = 3 }
          partitioned = { nominal_quota = 0 }
        }
        preemption = {
          reclaim_within_cohort = "LowerPriority"
          within_cluster_queue  = "LowerPriority"
        }
      }
    }
    local_queues = {
      cancer-primary = {
        namespace           = "fs2-models"
        cluster_queue       = "customer-batch"
        fair_sharing_weight = 4
        model_ids           = ["example-primary-model"]
        service_classes     = ["customer-batch"]
      }
      cancer-secondary = {
        namespace           = "fs2-models"
        cluster_queue       = "customer-batch"
        fair_sharing_weight = 1
        model_ids           = ["example-secondary-model"]
        service_classes     = ["bulk-backfill"]
      }
      academic-scientific-cpu = {
        namespace           = "fs2-academic-poc"
        cluster_queue       = "reference-data-cpu"
        fair_sharing_weight = 1
        model_ids           = []
        tenant_ids          = []
        service_classes     = []
      }
    }
    service_classes = {
      platform-critical = {
        workload_priority_class = "platform-critical"
        priority                = 10000
        preemption_mode         = "restartable"
        pool_preference         = ["reserved", "burst", "partitioned"]
      }
      presentation = {
        workload_priority_class = "presentation"
        priority                = 1000
        preemption_mode         = "restartable"
        pool_preference         = ["reserved", "burst", "partitioned"]
      }
      interactive = {
        workload_priority_class = "interactive"
        priority                = 100
        preemption_mode         = "restartable"
        pool_preference         = ["reserved", "burst", "partitioned"]
      }
      customer-batch = {
        workload_priority_class = "standard"
        priority                = 0
        default_local_queue     = "cancer-primary"
        preemption_mode         = "restartable"
        pool_preference         = ["reserved", "burst", "partitioned"]
        max_queue_seconds       = 3600
        max_execution_seconds   = 21600
        description             = "Deterministic test batch policy."
      }
      bulk-backfill = {
        workload_priority_class = "batch"
        priority                = -100
        default_local_queue     = "cancer-secondary"
        preemption_mode         = "restartable"
        pool_preference         = ["reserved", "burst", "partitioned"]
      }
    }
  }

  base_priority_classes = {
    interactive = 100
    standard    = 0
    batch       = -100
  }

  required_namespaces = {
    inference-accelerators = ["fs2-academic-poc"]
    reference-data-cpu     = ["fs2-academic-poc"]
  }

  # The GPU lane's object belongs to modules/academic-assets.
  external_local_queues = {
    academic-scientific = {
      namespace           = "fs2-academic-poc"
      cluster_queue       = "inference-accelerators"
      fair_sharing_weight = 1
      model_ids           = ["example-licensed-model"]
      tenant_ids          = ["tenant-academic"]
      service_classes = [
        "platform-critical",
        "presentation",
        "interactive",
        "customer-batch",
        "bulk-backfill",
      ]
    }
  }

  # The reference-data CPU ClusterQueue belongs to the reference-data plane.
  external_cluster_queues = {
    reference-data-cpu = {
      namespaces = ["fs2-academic-poc", "fs2-reference-data"]
    }
  }

  namespace_bound_models = {
    example-licensed-model = "fs2-academic-poc"
  }

  cpu_classes = {
    reference-data = {
      local_queue   = "academic-scientific-cpu"
      cluster_queue = "reference-data-cpu"
      namespace     = "fs2-academic-poc"
      pool_id       = "reference-cpu"
      node_selector = { "workload.fs2.nebius/reference-data" = "true" }
      tolerations = [{
        key      = "workload.fs2.nebius/reference-data"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }]
      schedulable_capacity = {
        cpu_millicores        = 16000
        memory_mib            = 65536
        ephemeral_storage_mib = 51200
      }
    }
  }
}

run "renders_weighted_shared_accelerator_queues" {
  command = plan

  assert {
    condition = output.contract.shared_pool_quota == {
      burst       = 3
      reserved    = 4
      partitioned = 2
    }
    error_message = "The cohort must receive exactly the residual physical capacity after queue floors."
  }

  assert {
    condition     = output.contract.cluster_queues["inference-accelerators"].metadata.name == "inference-accelerators"
    error_message = "The stable ClusterQueue identity must be retained."
  }

  assert {
    condition = (
      output.contract.cluster_queues["inference-accelerators"].spec.resourceGroups[0].flavors[0].resources[0].nominalQuota == "2" &&
      output.contract.cluster_queues["inference-accelerators"].spec.preemption.withinClusterQueue == "LowerPriority"
    )
    error_message = "The presentation-capable stable queue must retain an explicit nominal floor and same-queue displacement; cross-queue numerical priority alone is not an absolute fair-sharing guarantee."
  }

  assert {
    condition     = output.contract.local_queues["inference-models"].metadata.name == "inference-models"
    error_message = "The stable LocalQueue identity must be retained."
  }

  assert {
    condition     = !contains(keys(output.contract.cluster_queues["inference-accelerators"].spec.flavorFungibility), "preference")
    error_message = "The default MayStopSearch/TryNextFlavor strategy must omit the CRD-forbidden preference field."
  }

  assert {
    condition     = output.contract.cluster_queues["customer-batch"].spec.fairSharing.weight == "4"
    error_message = "ClusterQueue fair-sharing weight was not rendered."
  }

  assert {
    condition     = !contains(keys(output.contract.cluster_queues["customer-batch"].spec.preemption), "borrowWithinCohort")
    error_message = "Fair Sharing must not render the mutually exclusive classical borrowWithinCohort policy."
  }

  assert {
    condition     = output.contract.local_queues["cancer-primary"].spec.fairSharing.weight == "4"
    error_message = "LocalQueue admission-fair-sharing weight was not rendered."
  }

  assert {
    condition     = output.contract.service_classes["customer-batch"].default_local_queue == "cancer-primary"
    error_message = "The customer service class must resolve to its configured lane."
  }

  assert {
    condition = (
      output.contract.service_classes["customer-batch"].max_queue_seconds == 3600 &&
      output.contract.service_classes["customer-batch"].max_execution_seconds == 21600 &&
      output.contract.service_classes["customer-batch"].caller_selectable &&
      !output.contract.service_classes["platform-critical"].caller_selectable
    )
    error_message = "The controller-facing service-class SLA and caller boundary were not rendered."
  }

  assert {
    condition = (
      output.contract.local_queue_routes["cancer-primary"].namespace == "fs2-models" &&
      output.contract.local_queue_routes["cancer-primary"].cluster_queue == "customer-batch" &&
      length(output.contract.local_queue_routes["cancer-primary"].model_ids) == 1 &&
      output.contract.local_queue_routes["cancer-primary"].model_ids[0] == "example-primary-model" &&
      length(output.contract.local_queue_routes["cancer-primary"].tenant_ids) == 0 &&
      join(",", output.contract.local_queue_routes["cancer-primary"].service_classes) == "customer-batch"
    )
    error_message = "The deterministic model/tenant LocalQueue route was not exported."
  }

  assert {
    condition = output.contract.pools["burst"] == {
      resource_flavor           = "example-burst"
      accelerator_resource_name = "example.com/accelerator"
      capacity                  = 4
    }
    error_message = "The controller must be able to resolve pool IDs to exact flavors and accelerator resources."
  }

  assert {
    condition = (
      output.contract.cluster_queues["customer-batch"].spec.flavorFungibility.whenCanBorrow == "TryNextFlavor" &&
      output.contract.cluster_queues["customer-batch"].spec.flavorFungibility.whenCanPreempt == "TryNextFlavor" &&
      output.contract.cluster_queues["customer-batch"].spec.flavorFungibility.preference == "PreemptionOverBorrowing"
    )
    error_message = "Flavor borrowing/preemption selection must be explicit."
  }

  assert {
    condition     = !contains(keys(output.contract.cluster_queues["customer-batch"].spec), "admissionChecksStrategy")
    error_message = "The deployable default must not reference an AdmissionCheck/controller that this module does not install."
  }

  assert {
    condition     = output.contract_sha256 == sha256(jsonencode(output.contract))
    error_message = "The exported scheduling revision must cover the complete contract."
  }

  assert {
    condition     = output.contract.workload_priority_classes["presentation"].value == 1000
    error_message = "The presentation WorkloadPriorityClass must be rendered."
  }

  assert {
    condition = [
      for group in output.contract.cluster_queues["customer-batch"].spec.resourceGroups :
      group.coveredResources[0]
    ] == ["example.com/accelerator", "example.com/accelerator-slice"]
    error_message = "Pools must be grouped by Kubernetes extended resource name."
  }
}

run "rejects_nominal_floors_above_pool_capacity" {
  command = plan

  variables {
    scheduling = {
      cohort = {
        enabled             = true
        name                = "inference-shared"
        fair_sharing_weight = 1
      }
      cluster_queues = {
        inference-accelerators = {
          namespace              = "fs2-models"
          queueing_strategy      = "BestEffortFIFO"
          fair_sharing_weight    = 1
          admission_fair_sharing = true
          flavor_order           = ["burst", "reserved", "partitioned"]
          pool_quotas = {
            burst       = { nominal_quota = 5 }
            reserved    = { nominal_quota = 8 }
            partitioned = { nominal_quota = 2 }
          }
          preemption = {
            reclaim_within_cohort = "Never"
            within_cluster_queue  = "Never"
          }
        }
      }
      local_queues    = {}
      service_classes = {}
    }
  }

  expect_failures = [terraform_data.contract]
}

run "admits_the_licensed_asset_namespace_from_one_cluster_queue" {
  command = plan

  assert {
    condition = (
      join(",", output.contract.cluster_queue_namespaces["inference-accelerators"]) ==
      "fs2-academic-poc,fs2-models" &&
      one(output.contract.cluster_queues["inference-accelerators"].spec.namespaceSelector.matchExpressions).key ==
      "kubernetes.io/metadata.name" &&
      one(output.contract.cluster_queues["inference-accelerators"].spec.namespaceSelector.matchExpressions).operator ==
      "In" &&
      join(",", one(output.contract.cluster_queues["inference-accelerators"].spec.namespaceSelector.matchExpressions).values) ==
      "fs2-academic-poc,fs2-models"
    )
    error_message = "The stable ClusterQueue must explicitly admit both the model namespace and the licensed-asset namespace."
  }

  assert {
    condition = (
      output.contract.local_queues["academic-scientific"].metadata.namespace == "fs2-academic-poc" &&
      contains(output.contract.cluster_queue_namespaces["reference-data-cpu"], "fs2-academic-poc") &&
      join(",", output.contract.local_queue_routes["academic-scientific"].tenant_ids) == "tenant-academic" &&
      length(output.contract.local_queue_routes["academic-scientific"].service_classes) == 5 &&
      output.contract.namespace_bound_models["example-licensed-model"] == "fs2-academic-poc"
    )
    error_message = "The licensed lane must be an exact tenant/model route in its own namespace for every service class."
  }

  assert {
    condition = (
      output.contract.pool_node_label_key == "accelerator.fs2.nebius/pool-id" &&
      output.contract.resource_flavor_pool_ids["example-burst"] == "burst"
    )
    error_message = "A consumer must be able to map an admitted ResourceFlavor back to its pool through the contract."
  }

  assert {
    condition = (
      output.contract.cpu_classes["reference-data"].node_selector["workload.fs2.nebius/reference-data"] == "true" &&
      one(output.contract.cpu_classes["reference-data"].tolerations).effect == "NoSchedule" &&
      output.contract.cpu_classes["reference-data"].schedulable_capacity.cpu_millicores == 16000 &&
      output.contract.cpu_classes["reference-data"].local_queue == "academic-scientific-cpu" &&
      output.contract.cpu_classes["reference-data"].cluster_queue == "reference-data-cpu" &&
      length(output.contract.cpu_classes) == 1
    )
    error_message = "Each CPU stage class needs its own queue, node routing, toleration, and advertised per-node capacity; one global placement cannot serve both a tainted reference pool and general CPU work."
  }

  assert {
    condition = (
      join(",", output.contract.external_local_queue_names) == "academic-scientific" &&
      output.contract.local_queues["academic-scientific-cpu"].spec.clusterQueue == "reference-data-cpu" &&
      output.contract.local_queues["academic-scientific-cpu"].metadata.namespace == "fs2-academic-poc" &&
      output.contract.local_queue_routes["academic-scientific"].namespace == "fs2-academic-poc"
    )
    error_message = "The licensed namespace needs two lanes, the GPU one owned elsewhere and the CPU one on the reference ClusterQueue, so a consumer can freeze a different queue per stage."
  }

  assert {
    condition = (
      output.contract.priority_precedence["inference-accelerators"] == "localqueue-fair-share-then-priority" &&
      output.contract.priority_precedence["customer-batch"] == "localqueue-fair-share-then-priority"
    )
    error_message = "The contract must state the ordering Kueue actually applies, not an unconditional priority guarantee."
  }

  assert {
    condition     = join(",", output.local_queue_namespaces) == "fs2-academic-poc,fs2-models"
    error_message = "A caller must be able to order the apply behind every namespace this module renders queues in."
  }
}

run "rejects_unacknowledged_multi_lane_fair_share_ordering" {
  command = plan

  variables {
    required_namespaces    = {}
    namespace_bound_models = {}
    scheduling = {
      cohort = {
        enabled             = true
        name                = "inference-shared"
        fair_sharing_weight = 1
      }
      cluster_queues = {
        inference-accelerators = {
          namespace              = "fs2-models"
          queueing_strategy      = "BestEffortFIFO"
          fair_sharing_weight    = 1
          admission_fair_sharing = true
          flavor_order           = ["reserved", "burst", "partitioned"]
          pool_quotas = {
            reserved    = { nominal_quota = 0 }
            burst       = { nominal_quota = 0 }
            partitioned = { nominal_quota = 0 }
          }
          preemption = {
            reclaim_within_cohort = "LowerPriority"
            within_cluster_queue  = "LowerPriority"
          }
        }
      }
      local_queues = {
        second-lane = {
          namespace           = "fs2-models"
          cluster_queue       = "inference-accelerators"
          fair_sharing_weight = 1
          model_ids           = ["example-primary-model"]
          service_classes     = ["customer-batch"]
        }
      }
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
      }
    }
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_stranded_quota_when_the_cohort_is_disabled" {
  command = plan

  variables {
    required_namespaces    = {}
    namespace_bound_models = {}
    scheduling = {
      cohort = {
        enabled             = false
        name                = "inference-shared"
        fair_sharing_weight = 1
      }
      cluster_queues = {
        inference-accelerators = {
          namespace              = "fs2-models"
          queueing_strategy      = "BestEffortFIFO"
          fair_sharing_weight    = 1
          admission_fair_sharing = true
          flavor_order           = ["reserved", "burst", "partitioned"]
          pool_quotas = {
            reserved    = { nominal_quota = 8 }
            burst       = { nominal_quota = 1 }
            partitioned = { nominal_quota = 2 }
          }
          preemption = {
            reclaim_within_cohort = "LowerPriority"
            within_cluster_queue  = "LowerPriority"
          }
        }
      }
      local_queues    = {}
      service_classes = {}
    }
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_a_licensed_model_without_a_lane_for_every_caller_class" {
  command = plan

  variables {
    namespace_bound_models = {
      example-primary-model = "fs2-models"
    }
  }

  expect_failures = [terraform_data.contract]
}

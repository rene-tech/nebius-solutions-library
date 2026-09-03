variables {
  pools = {
    burst = {
      flavor_name   = "example-burst"
      resource_name = "example.com/accelerator"
      capacity      = 4
      preemptible   = true
      min_nodes     = 0
    }
    reserved = {
      flavor_name   = "example-reserved"
      resource_name = "example.com/accelerator"
      capacity      = 8
      # All three fixture pools are preemptible. This one has a node floor, so
      # it is the warmest available tier and must be searched first.
      preemptible = true
      min_nodes   = 1
    }
    partitioned = {
      flavor_name = "example-partitioned"
      # One accelerator resource across the pools, which is what pool-coupled
      # core admission requires: cpu and memory share the accelerator
      # resourceGroup, and a resource belongs to exactly one group.
      resource_name = "example.com/accelerator"
      capacity      = 2
      preemptible   = true
      min_nodes     = 0
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
        # A zero floor throughout, so every accelerator this queue runs on is
        # borrowed from the Cohort and the protected lanes on the stable queue
        # can reclaim it. A nominal floor here would be unreclaimable, and the
        # renderer refuses that beside protected work.
        pool_quotas = {
          reserved    = { nominal_quota = 0, borrowing_limit = 4 }
          burst       = { nominal_quota = 0, borrowing_limit = 3 }
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
      # The CPU queue its own owner renders, with the cpu and memory floor it
      # declares there. It is outside the accelerator Cohort and cannot
      # borrow, so its floor is the only capacity it has.
      core_quota = {
        cpu_millicores = 24000
        memory_mib     = 98304
      }
    }
  }

  namespace_bound_models = {
    example-licensed-model = "fs2-academic-poc"
  }

  # A model qualified for a warm pool and a burst pool, so it can start on one
  # and burst onto the other, and a model qualified for neither of those.
  model_eligible_pool_ids = {
    example-licensed-model = ["reserved", "burst"]
    # reserved and burst share one accelerator resource, so they are a valid
    # fallback set; partitioned advertises a different one.
    example-primary-model   = ["reserved", "burst"]
    example-secondary-model = ["partitioned"]
  }

  # A CPU stage requests cpu and memory, which Kueue drops before admission
  # unless core admission is on, so the fixture that declares a CPU class must
  # also budget core resources.
  # Measured schedulable cpu and memory per accelerator pool, at that pool's
  # maximum node count. Core admission is pool-coupled, so these ride on each
  # pool's own ResourceFlavor rather than on a shared core flavor.
  core_capacity = {
    reserved    = { cpu_millicores = 96000, memory_mib = 786432 }
    burst       = { cpu_millicores = 48000, memory_mib = 393216 }
    partitioned = { cpu_millicores = 24000, memory_mib = 196608 }
  }

  cpu_classes = {
    reference-data = {
      local_queue       = "academic-scientific-cpu"
      cluster_queue     = "reference-data-cpu"
      namespace         = "fs2-academic-poc"
      resource_flavor   = "reference-data-cpu"
      eligible_pool_ids = ["reference-cpu"]
      pool_resolution = {
        mode    = "per-pool-flavor"
        pool_id = "reference-cpu"
      }
      node_selector = { "workload.fs2.nebius/reference-data" = "true" }
      tolerations = [{
        key      = "workload.fs2.nebius/reference-data"
        operator = "Equal"
        value    = "true"
        effect   = "NoSchedule"
      }]
      schedulable_capacity = {
        cpu               = "16000m"
        memory            = "65536Mi"
        ephemeral_storage = "51200Mi"

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
    # The bulk queue reserves nothing, so its former floors return to the
    # Cohort where the protected lanes can reclaim them.
    condition = output.contract.shared_pool_quota == {
      burst       = 4
      reserved    = 6
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
      # Core admission is on, so cpu and memory ride in the accelerator group
      # rather than forming one of their own. That is what ties a Workload's
      # cpu and memory to the pool whose accelerators it reserved.
    ] == ["example.com/accelerator"]
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
      join(",", output.contract.model_eligible_pool_ids["example-licensed-model"]) ==
      "reserved,burst" &&
      join(",", output.contract.model_eligible_pool_ids["example-secondary-model"]) ==
      "partitioned"
    )
    error_message = "Eligibility is a set, so a model can be qualified for a warm pool and a burst pool, and another model for neither."
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
      output.contract.cpu_classes["reference-data"].resource_flavor == "reference-data-cpu" &&
      !contains(keys(output.contract.resource_flavor_pool_ids), "reference-data-cpu")
    )
    error_message = "A CPU class must publish the ResourceFlavor its admission reports, because the accelerator reverse map does not cover a core flavor."
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

run "rejects_core_admission_across_two_accelerator_resources" {
  command = plan

  variables {
    pools = {
      p00 = {
        flavor_name   = "example-p00"
        resource_name = "example.com/accelerator-00"
        capacity      = 1
      }
      p01 = {
        flavor_name   = "example-p01"
        resource_name = "example.com/accelerator-01"
        capacity      = 1
      }
      p02 = {
        flavor_name   = "example-p02"
        resource_name = "example.com/accelerator-02"
        capacity      = 1
      }
      p03 = {
        flavor_name   = "example-p03"
        resource_name = "example.com/accelerator-03"
        capacity      = 1
      }
      p04 = {
        flavor_name   = "example-p04"
        resource_name = "example.com/accelerator-04"
        capacity      = 1
      }
      p05 = {
        flavor_name   = "example-p05"
        resource_name = "example.com/accelerator-05"
        capacity      = 1
      }
      p06 = {
        flavor_name   = "example-p06"
        resource_name = "example.com/accelerator-06"
        capacity      = 1
      }
      p07 = {
        flavor_name   = "example-p07"
        resource_name = "example.com/accelerator-07"
        capacity      = 1
      }
      p08 = {
        flavor_name   = "example-p08"
        resource_name = "example.com/accelerator-08"
        capacity      = 1
      }
      p09 = {
        flavor_name   = "example-p09"
        resource_name = "example.com/accelerator-09"
        capacity      = 1
      }
      p10 = {
        flavor_name   = "example-p10"
        resource_name = "example.com/accelerator-10"
        capacity      = 1
      }
      p11 = {
        flavor_name   = "example-p11"
        resource_name = "example.com/accelerator-11"
        capacity      = 1
      }
      p12 = {
        flavor_name   = "example-p12"
        resource_name = "example.com/accelerator-12"
        capacity      = 1
      }
      p13 = {
        flavor_name   = "example-p13"
        resource_name = "example.com/accelerator-13"
        capacity      = 1
      }
      p14 = {
        flavor_name   = "example-p14"
        resource_name = "example.com/accelerator-14"
        capacity      = 1
      }
    }
    scheduling = {
      cohort = {
        enabled             = true
        name                = "inference-shared"
        fair_sharing_weight = 1
      }
      cluster_queues = {}
      local_queues   = {}
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14"]
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14"]
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14"]
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14"]
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14"]
        }
      }
    }
    required_namespaces     = {}
    namespace_bound_models  = {}
    cpu_classes             = {}
    cpu_stage_requests      = {}
    external_local_queues   = {}
    model_eligible_pool_ids = {}
    # Core admission couples cpu and memory to the accelerator resourceGroup,
    # and a resource belongs to exactly one group, so fifteen accelerator
    # resources cannot all carry them. Decoupling would let a Workload hold
    # accelerators in one pool against cpu and memory measured on another and
    # then fit no node, so the contract refuses the deployment instead.
    core_capacity = {
      p00 = { cpu_millicores = 16000, memory_mib = 65536 }
      p01 = { cpu_millicores = 16000, memory_mib = 65536 }
      p02 = { cpu_millicores = 16000, memory_mib = 65536 }
      p03 = { cpu_millicores = 16000, memory_mib = 65536 }
      p04 = { cpu_millicores = 16000, memory_mib = 65536 }
      p05 = { cpu_millicores = 16000, memory_mib = 65536 }
      p06 = { cpu_millicores = 16000, memory_mib = 65536 }
      p07 = { cpu_millicores = 16000, memory_mib = 65536 }
      p08 = { cpu_millicores = 16000, memory_mib = 65536 }
      p09 = { cpu_millicores = 16000, memory_mib = 65536 }
      p10 = { cpu_millicores = 16000, memory_mib = 65536 }
      p11 = { cpu_millicores = 16000, memory_mib = 65536 }
      p12 = { cpu_millicores = 16000, memory_mib = 65536 }
      p13 = { cpu_millicores = 16000, memory_mib = 65536 }
      p14 = { cpu_millicores = 16000, memory_mib = 65536 }
    }
  }

  expect_failures = [terraform_data.contract]
}

run "seventeen_accelerator_resources_exceed_the_crd_cap" {
  command = plan

  variables {
    pools = {
      p00 = {
        flavor_name   = "example-p00"
        resource_name = "example.com/accelerator-00"
        capacity      = 1
      }
      p01 = {
        flavor_name   = "example-p01"
        resource_name = "example.com/accelerator-01"
        capacity      = 1
      }
      p02 = {
        flavor_name   = "example-p02"
        resource_name = "example.com/accelerator-02"
        capacity      = 1
      }
      p03 = {
        flavor_name   = "example-p03"
        resource_name = "example.com/accelerator-03"
        capacity      = 1
      }
      p04 = {
        flavor_name   = "example-p04"
        resource_name = "example.com/accelerator-04"
        capacity      = 1
      }
      p05 = {
        flavor_name   = "example-p05"
        resource_name = "example.com/accelerator-05"
        capacity      = 1
      }
      p06 = {
        flavor_name   = "example-p06"
        resource_name = "example.com/accelerator-06"
        capacity      = 1
      }
      p07 = {
        flavor_name   = "example-p07"
        resource_name = "example.com/accelerator-07"
        capacity      = 1
      }
      p08 = {
        flavor_name   = "example-p08"
        resource_name = "example.com/accelerator-08"
        capacity      = 1
      }
      p09 = {
        flavor_name   = "example-p09"
        resource_name = "example.com/accelerator-09"
        capacity      = 1
      }
      p10 = {
        flavor_name   = "example-p10"
        resource_name = "example.com/accelerator-10"
        capacity      = 1
      }
      p11 = {
        flavor_name   = "example-p11"
        resource_name = "example.com/accelerator-11"
        capacity      = 1
      }
      p12 = {
        flavor_name   = "example-p12"
        resource_name = "example.com/accelerator-12"
        capacity      = 1
      }
      p13 = {
        flavor_name   = "example-p13"
        resource_name = "example.com/accelerator-13"
        capacity      = 1
      }
      p14 = {
        flavor_name   = "example-p14"
        resource_name = "example.com/accelerator-14"
        capacity      = 1
      }
      p15 = {
        flavor_name   = "example-p15"
        resource_name = "example.com/accelerator-15"
        capacity      = 1
      }
      p16 = {
        flavor_name   = "example-p16"
        resource_name = "example.com/accelerator-16"
        capacity      = 1
      }
    }
    scheduling = {
      cohort = {
        enabled             = true
        name                = "inference-shared"
        fair_sharing_weight = 1
      }
      cluster_queues = {}
      local_queues   = {}
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14", "p15", "p16"]
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14", "p15", "p16"]
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14", "p15", "p16"]
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14", "p15", "p16"]
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          pool_preference         = ["p00", "p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "p09", "p10", "p11", "p12", "p13", "p14", "p15", "p16"]
        }
      }
    }
    required_namespaces     = {}
    namespace_bound_models  = {}
    cpu_classes             = {}
    cpu_stage_requests      = {}
    external_local_queues   = {}
    model_eligible_pool_ids = {}
    # No core admission here: sixteen accelerator resources are already one
    # resourceGroup past what a ClusterQueue may declare.
    core_capacity = {}
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_an_eligible_set_spanning_two_accelerator_resources" {
  command = plan

  variables {
    # Two accelerator resources, so core admission is off here; this run is
    # about the eligible set, not about core coupling.
    pools = {
      reserved = {
        flavor_name   = "example-reserved"
        resource_name = "example.com/accelerator"
        capacity      = 8
        preemptible   = false
        min_nodes     = 1
      }
      burst = {
        flavor_name   = "example-burst"
        resource_name = "example.com/accelerator"
        capacity      = 4
        preemptible   = true
      }
      partitioned = {
        flavor_name   = "example-partitioned"
        resource_name = "example.com/accelerator-slice"
        capacity      = 2
      }
    }
    core_capacity = {}
    cpu_classes   = {}
    # burst and partitioned advertise different resource names, and one
    # Workload requests exactly one of them, so the second entry is a flavor
    # Kueue can never fall back to.
    model_eligible_pool_ids = {
      example-licensed-model = ["burst", "partitioned"]
    }
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_a_merged_namespace_list_that_is_not_label_safe" {
  command = plan

  variables {
    required_namespaces = {
      inference-accelerators = ["fs2-academic-poc", "Not_A_Namespace"]
      reference-data-cpu     = ["fs2-academic-poc"]
    }
  }

  expect_failures = [terraform_data.contract]
}

run "an_empty_order_is_derived_warm_first_when_every_pool_is_preemptible" {
  command = plan

  variables {
    # No operator-declared ClusterQueues, so the stable queue this module
    # renders is the one under test. Every pool is preemptible, but reserved
    # has a node floor while the other two scale from zero. Empty inputs must
    # derive that warm-first order rather than use alphabetical pool IDs.
    default_queue = {
      cluster_queue_name                 = "inference-accelerators"
      local_queue_name                   = "inference-models"
      namespace                          = "fs2-models"
      queueing_strategy                  = "BestEffortFIFO"
      flavor_order                       = []
      fair_share_precedence_acknowledged = true
    }
    scheduling = {
      cohort = { enabled = true, name = "inference-shared", fair_sharing_weight = 1 }
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          pool_preference         = []
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          pool_preference         = []
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          pool_preference         = []
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          pool_preference         = []
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          pool_preference         = []
        }
      }
      cluster_queues = {}
      local_queues   = {}
    }
    external_local_queues   = {}
    external_cluster_queues = {}
    required_namespaces     = {}
    namespace_bound_models  = {}
    model_eligible_pool_ids = {}
    cpu_classes             = {}
    cpu_stage_requests      = {}
  }

  assert {
    condition = (
      join(",", output.contract.cluster_queue_pool_order["inference-accelerators"]) ==
      "reserved,burst,partitioned" &&
      join(",", output.contract.cluster_queue_pool_order["inference-accelerators"]) !=
      join(",", sort(output.contract.cluster_queue_pool_order["inference-accelerators"])) &&
      output.contract.cluster_queues["inference-accelerators"].metadata.annotations["fs2-serve.nebius.ai/accelerator-pool-ids"] ==
      "reserved,burst,partitioned" &&
      [
        for flavor in output.contract.cluster_queues["inference-accelerators"].spec.resourceGroups[0].flavors :
        flavor.name
      ] == ["example-reserved", "example-burst", "example-partitioned"]
    )
    error_message = "An empty order must derive the warm preemptible pool before preemptible pools that scale from zero, not sort pool IDs alphabetically."
  }
}

run "rejects_a_stable_queue_pool_order_that_is_not_every_pool" {
  command = plan

  variables {
    default_queue = {
      cluster_queue_name                 = "inference-accelerators"
      local_queue_name                   = "inference-models"
      namespace                          = "fs2-models"
      queueing_strategy                  = "BestEffortFIFO"
      flavor_order                       = ["reserved", "burst"]
      fair_share_precedence_acknowledged = true
    }
    scheduling = {
      cohort = { enabled = true, name = "inference-shared", fair_sharing_weight = 1 }
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst"]
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst"]
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst"]
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst"]
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          pool_preference         = ["reserved", "burst"]
        }
      }
      cluster_queues = {}
      local_queues   = {}
    }
    external_local_queues   = {}
    external_cluster_queues = {}
    required_namespaces     = {}
    namespace_bound_models  = {}
    model_eligible_pool_ids = {}
    cpu_classes             = {}
    cpu_stage_requests      = {}
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_a_cpu_class_toleration_that_cannot_match_a_taint" {
  command = plan

  variables {
    # Exists never carries a value; Equal always does. A toleration that mixes
    # them matches nothing, so the stage would sit unschedulable on the
    # tainted pool rather than being refused here.
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "workload.fs2.nebius/reference-data" = "true" }
        tolerations = [{
          key      = "workload.fs2.nebius/reference-data"
          operator = "Exists"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_a_cpu_class_with_no_node_routing" {
  command = plan

  variables {
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        # No selector: the Pod would land anywhere the taint allows.
        node_selector = {}
        tolerations = [{
          key      = "workload.fs2.nebius/reference-data"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_a_cpu_class_capacity_quantity_that_contradicts_its_integer" {
  command = plan

  variables {
    # The quantity and the integer are the same number in two spellings. A
    # grammar-only check would let these disagree and the contract would be
    # lying about one of them, whichever a consumer happened to read.
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "workload.fs2.nebius/reference-data" = "true" }
        tolerations = [{
          key      = "workload.fs2.nebius/reference-data"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "1m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_a_contributed_cpu_class_memory_that_contradicts_its_integer" {
  command = plan

  variables {
    # The same rule applies to a class contributed by another owner, which is
    # where a unit mistake is most likely to arrive unnoticed.
    cpu_classes = {
      general-cpu = {
        local_queue       = "general-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "general-cpu"
        eligible_pool_ids = ["general-small", "general-large"]
        pool_resolution = {
          mode           = "node-label-observation"
          node_label_key = "accelerator.fs2.nebius/pool-id"
        }
        node_selector = { "workload.fs2.nebius/general-cpu" = "true" }
        tolerations   = []
        schedulable_capacity = {
          cpu               = "15500m"
          memory            = "60Gi"
          ephemeral_storage = "0Mi"

          cpu_millicores        = 15500
          memory_mib            = 61440
          ephemeral_storage_mib = 0
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "accepts_a_toleration_key_at_the_qualified_name_boundary" {
  command = plan

  variables {
    # 253-character prefix, slash, 63-character name: exactly 317, the largest
    # a Kubernetes qualified name can be. The variable validation, this
    # module's contract precondition, and the published class schema must all
    # accept it, or a class that one layer allows another silently rejects.
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" = "true" }
        tolerations = [{
          key      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  assert {
    condition = (
      one(output.contract.cpu_classes["reference-data"].tolerations).key == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" &&
      length(one(output.contract.cpu_classes["reference-data"].tolerations).key) == 317
    )
    error_message = "A 317-character qualified toleration key must survive every layer unchanged."
  }
}

run "rejects_a_toleration_key_one_character_past_the_boundary" {
  command = plan

  variables {
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "workload.fs2.nebius/reference-data" = "true" }
        tolerations = [{
          key      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbc"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_a_qualified_key_whose_prefix_is_too_long" {
  command = plan

  variables {
    # 254 before the slash and 62 after: 317 in total, so a total-length rule
    # accepts it while the API rejects the prefix.
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" = "true" }
        tolerations = [{
          key      = "workload.fs2.nebius/reference-data"
          operator = "Equal"
          value    = "true"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_an_equal_toleration_value_the_api_would_reject" {
  command = plan

  variables {
    # A label value has a grammar, so a space is invalid however short it is.
    cpu_classes = {
      reference-data = {
        local_queue       = "academic-scientific-cpu"
        cluster_queue     = "reference-data-cpu"
        namespace         = "fs2-academic-poc"
        resource_flavor   = "reference-data-cpu"
        eligible_pool_ids = ["reference-cpu"]
        pool_resolution = {
          mode    = "per-pool-flavor"
          pool_id = "reference-cpu"
        }
        node_selector = { "workload.fs2.nebius/reference-data" = "true" }
        tolerations = [{
          key      = "workload.fs2.nebius/reference-data"
          operator = "Equal"
          value    = "fs2 reference data"
          effect   = "NoSchedule"
        }]
        schedulable_capacity = {
          cpu               = "16000m"
          memory            = "65536Mi"
          ephemeral_storage = "51200Mi"

          cpu_millicores        = 16000
          memory_mib            = 65536
          ephemeral_storage_mib = 51200
        }
      }
    }
  }

  expect_failures = [var.cpu_classes]
}

run "rejects_unacknowledged_bulk_floor_a_protected_lane_cannot_reclaim" {
  command = plan

  variables {
    # Kueue's reclaimWithinCohort preempts only what another ClusterQueue
    # borrowed above its nominal quota, so the floor customer-batch holds is
    # unreachable for the presentation and interactive lanes on the stable
    # queue. Priority does not change that, and the contract refuses to imply
    # it does.
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
            reserved    = { nominal_quota = 2 }
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
          fair_sharing_weight                = 1
          admission_fair_sharing             = true
          fair_share_precedence_acknowledged = true
          flavor_order                       = ["reserved", "burst", "partitioned"]
          # A real floor, so anything running inside it is not borrowed and
          # cannot be reclaimed by the protected lanes elsewhere.
          pool_quotas = {
            reserved    = { nominal_quota = 1 }
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
        inference-models = {
          namespace           = "fs2-models"
          cluster_queue       = "inference-accelerators"
          fair_sharing_weight = 1
          model_ids           = toset(["example-primary-model"])
          tenant_ids          = toset([])
          service_classes     = toset(["presentation", "interactive"])
        }
        customer-bulk = {
          namespace           = "fs2-models"
          cluster_queue       = "customer-batch"
          fair_sharing_weight = 1
          model_ids           = toset(["example-primary-model"])
          tenant_ids          = toset([])
          service_classes     = toset(["bulk-backfill"])
        }
      }
      service_classes = {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
          default_local_queue     = "inference-models"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
          default_local_queue     = "inference-models"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
          default_local_queue     = "inference-models"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
          default_local_queue     = "inference-models"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
          default_local_queue     = "customer-bulk"
          pool_preference         = ["reserved", "burst", "partitioned"]
        }
      }
    }
    default_queue = {
      cluster_queue_name                 = "inference-accelerators"
      local_queue_name                   = "inference-models"
      namespace                          = "fs2-models"
      queueing_strategy                  = "BestEffortFIFO"
      flavor_order                       = ["reserved", "burst", "partitioned"]
      fair_share_precedence_acknowledged = true
    }
    cpu_classes             = {}
    cpu_stage_requests      = {}
    external_local_queues   = {}
    external_cluster_queues = {}
    required_namespaces     = {}
    namespace_bound_models  = {}
    model_eligible_pool_ids = {}
  }

  expect_failures = [terraform_data.contract]
}

run "rejects_core_admission_with_a_pool_that_has_no_measured_capacity" {
  command = plan

  variables {
    # A pool absent from the map has no budget, and the queues would still
    # list its flavor. Kueue would then admit accelerator work there against
    # cpu and memory measured somewhere else, which is the cross-pool leak
    # this coupling exists to prevent, so the contract fails closed.
    core_capacity = {
      reserved = { cpu_millicores = 96000, memory_mib = 786432 }
      burst    = { cpu_millicores = 48000, memory_mib = 393216 }
    }
  }

  expect_failures = [terraform_data.contract]
}

run "core_quota_follows_the_accelerator_share_of_the_same_pool" {
  command = plan

  assert {
    condition = (
      # customer-batch reserves no accelerators, so it reserves no cores and
      # borrows both from the Cohort together.
      output.contract.core_queue_quotas["customer-batch"]["reserved"].cpu_millicores == 0 &&
      # The stable queue holds a quarter of the reserved pool's accelerators
      # (2 of 8), so it holds a quarter of that pool's cores.
      output.contract.core_queue_quotas["inference-accelerators"]["reserved"].cpu_millicores == 24000 &&
      output.contract.core_queue_quotas["inference-accelerators"]["reserved"].memory_mib == 196608 &&
      # And the Cohort holds exactly the rest of that pool, never a total
      # pooled across pools.
      output.contract.core_shared_quota["reserved"].cpu_millicores == 72000 &&
      output.contract.core_shared_quota["reserved"].memory_mib == 589824
    )
    error_message = "A queue's core floor must be the same share of a pool that its accelerator floor is, and the Cohort must hold exactly that pool's remainder."
  }

  assert {
    condition = (
      # cpu and memory ride in the accelerator group, so Kueue's one flavor
      # assignment per group grants all three from the same pool.
      one(output.contract.cluster_queues["inference-accelerators"].spec.resourceGroups).coveredResources ==
      ["example.com/accelerator", "cpu", "memory"] &&
      [
        for resource in one(output.contract.cluster_queues["inference-accelerators"].spec.resourceGroups).flavors[0].resources :
        resource.name
      ] == ["example.com/accelerator", "cpu", "memory"]
    )
    error_message = "cpu and memory must share the accelerator resourceGroup, or Kueue could grant them from a different pool than the accelerators."
  }
}

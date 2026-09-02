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
        namespace              = "fs2-models"
        queueing_strategy      = "BestEffortFIFO"
        fair_sharing_weight    = 1
        admission_fair_sharing = true
        flavor_order           = ["reserved", "burst", "partitioned"]
        pool_quotas = {
          reserved    = { nominal_quota = 2, lending_limit = 0 }
          burst       = { nominal_quota = 0 }
          partitioned = { nominal_quota = 0 }
        }
        preemption = {
          reclaim_within_cohort = "Never"
          within_cluster_queue  = "Never"
        }
      }
      customer-batch = {
        namespace              = "fs2-models"
        queueing_strategy      = "BestEffortFIFO"
        fair_sharing_weight    = 4
        admission_fair_sharing = true
        flavor_order           = ["burst", "reserved", "partitioned"]
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
      }
      cancer-secondary = {
        namespace           = "fs2-models"
        cluster_queue       = "customer-batch"
        fair_sharing_weight = 1
        model_ids           = ["example-secondary-model"]
      }
    }
    service_classes = {
      platform-critical = {
        workload_priority_class = "platform-critical"
        priority                = 10000
        preemption_mode         = "non-preemptible"
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
        pool_preference         = ["burst", "reserved", "partitioned"]
      }
      customer-batch = {
        workload_priority_class = "standard"
        priority                = 0
        default_local_queue     = "cancer-primary"
        preemption_mode         = "restartable"
        pool_preference         = ["burst", "reserved", "partitioned"]
      }
      bulk-backfill = {
        workload_priority_class = "batch"
        priority                = -100
        default_local_queue     = "cancer-secondary"
        preemption_mode         = "checkpointable"
        pool_preference         = ["burst", "reserved", "partitioned"]
      }
    }
  }

  base_priority_classes = {
    interactive = 100
    standard    = 0
    batch       = -100
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
    condition     = output.contract.local_queues["inference-models"].metadata.name == "inference-models"
    error_message = "The stable LocalQueue identity must be retained."
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

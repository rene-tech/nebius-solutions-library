# Kueue queue, priority, and fair-capacity policy on customer-specified H100
# capacity. This example covers the scheduling policy only: it deliberately
# leaves the scientific batch controller gate off, so it does not install
# JobSet and makes no claim about the scientific execution path.
#
# One warm preemptible pool plus one scale-from-zero preemptible pool. Nothing
# here claims a reservation or a capacity block.
# Two customer lanes share a weighted ClusterQueue that borrows unreserved
# capacity from the shared Cohort. Nothing here is GPU-model specific: the pool
# IDs, ResourceFlavor names, and extended-resource name all come from the
# accelerator pools below. Run it from k8s-inference with ./inference-stack.

deployment = {
  schema_version = 1
  name           = "inference-h100-lanes"

  target = {
    project_id   = "project-yourprojectid"
    project_name = "my-inference-project"
    # H100 capacity in this repository's catalog and in the shared cluster is
    # eu-north1. us-north1 is B300-preemptible only.
    region = "eu-north1"
    network = {
      network_name        = "default-network"
      subnet_name         = "default-subnet"
      private_subnet_cidr = "10.0.0.0/16"
    }
    system_update_strategy = { max_surge = 1, max_unavailable = 0 }
  }

  # accelerator_pools below makes this deployment customer-specified, so the
  # accelerators profile is only the catalog fallback name.
  profiles = {
    capacity     = "minimal"
    accelerators = "minimal"
    models       = "minimal"
  }

  accelerator_pools = {
    # Both pools are preemptible: this example claims no reservation, and a
    # regular H100 pool needs capacity-block reservation IDs this file does not
    # represent. The first pool simply keeps a warm floor.
    "h100-warm" = {
      platform          = "gpu-h100-sxm"
      preset            = "8gpu-128vcpu-1600gb"
      accelerator_class = "nvidia-h100-sxm5-80gb"
      gpus_per_node     = 8
      gpu_memory_gb     = 80
      capacity_type     = "preemptible"
      min_nodes         = 1
      max_nodes         = 1
      driver            = { mode = "managed", preset = "cuda12.4" }
    }
    "h100-preemptible" = {
      platform          = "gpu-h100-sxm"
      preset            = "8gpu-128vcpu-1600gb"
      accelerator_class = "nvidia-h100-sxm5-80gb"
      gpus_per_node     = 8
      gpu_memory_gb     = 80
      capacity_type     = "preemptible"
      min_nodes         = 0
      max_nodes         = 2
      driver            = { mode = "managed", preset = "cuda12.4" }
    }
  }

  models = {
    selection = "profile"
    scaling = {
      mode = "keda"
      hot  = []
    }
    # The minimal profile selects proteinmpnn, whose catalog placement targets
    # B300 pools. This deployment has no B300, so the model is placed on an
    # H100 pool explicitly. That is a placement choice, not a qualification
    # claim: the runtime still has to be qualified for this accelerator before
    # anything is served.
    pool_overrides = {
      proteinmpnn = "h100-warm"
    }
  }

  # Two scientific lanes on one weighted ClusterQueue. The always-on pool is
  # listed first so interactive and presentation work prefers hot capacity and
  # only falls through to scale-from-zero preemptible nodes.
  scheduling = {
    cohort = { enabled = true, name = "inference-shared" }
    # Both lanes live on one ClusterQueue with usage-based admission fair
    # sharing, so pending work is ordered by decayed LocalQueue usage before
    # WorkloadPriorityClass. Accepting that is explicit.
    fair_share_precedence_acknowledged = true
    cluster_queues = {
      scientific-batch = {
        fair_sharing_weight = 4
        flavor_order        = ["h100-warm", "h100-preemptible"]
        # This queue serves two lanes, so Kueue orders their pending work by
        # decayed LocalQueue usage before WorkloadPriorityClass.
        fair_share_precedence_acknowledged = true
        pool_quotas = {
          h100-warm        = { nominal_quota = 8, lending_limit = 4 }
          h100-preemptible = { nominal_quota = 0 }
        }
        preemption = {
          reclaim_within_cohort = "LowerPriority"
          within_cluster_queue  = "LowerPriority"
        }
      }
    }
    local_queues = {
      scientific-primary = {
        cluster_queue       = "scientific-batch"
        fair_sharing_weight = 4
        model_ids           = ["proteinmpnn"]
        service_classes     = ["presentation", "interactive", "customer-batch"]
      }
      # Unrestricted, because bulk-backfill defaults here: a tenant-restricted
      # lane must never be a service class default.
      scientific-bulk = {
        cluster_queue       = "scientific-batch"
        fair_sharing_weight = 1
        model_ids           = ["proteinmpnn"]
        service_classes     = ["bulk-backfill"]
      }
    }
    service_classes = {
      platform-critical = {
        workload_priority_class = "platform-critical"
        priority                = 10000
        preemption_mode         = "restartable"
      }
      presentation = {
        workload_priority_class = "presentation"
        priority                = 1000
        default_local_queue     = "scientific-primary"
        preemption_mode         = "restartable"
        pool_preference         = ["h100-warm", "h100-preemptible"]
        max_queue_seconds       = 120
        max_execution_seconds   = 1800
      }
      interactive = {
        workload_priority_class = "interactive"
        priority                = 100
        default_local_queue     = "scientific-primary"
        preemption_mode         = "restartable"
        pool_preference         = ["h100-warm", "h100-preemptible"]
      }
      customer-batch = {
        workload_priority_class = "standard"
        priority                = 0
        default_local_queue     = "scientific-primary"
        preemption_mode         = "restartable"
        pool_preference         = ["h100-warm", "h100-preemptible"]
        max_queue_seconds       = 3600
        max_execution_seconds   = 21600
      }
      bulk-backfill = {
        workload_priority_class = "batch"
        priority                = -100
        default_local_queue     = "scientific-bulk"
        preemption_mode         = "restartable"
        pool_preference         = ["h100-warm", "h100-preemptible"]
      }
    }
  }

  edge = {
    mode = "internal-only"
  }

  applications = {
    control_plane = {
      repository             = "registry.example.invalid/k8s-inference/control-plane"
      digest                 = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      catalog_rollout_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
    admin_console = {
      repository = "registry.example.invalid/k8s-inference/admin-console"
      digest     = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      provenance = {
        source_commit = "1111111111111111111111111111111111111111"
        source_tree   = "2222222222222222222222222222222222222222"
        sbom_sha256   = "3333333333333333333333333333333333333333333333333333333333333333"
        sbom_format   = "cyclonedx-json"
      }
    }
  }
}

# Raw AlphaFold 3 execution: the licensed claim namespace, its own GPU and CPU
# lanes, and real cpu/memory admission.
#
# The raw data stage is CPU-only, reads the shared reference databases, and
# needs 16 CPU and 64 GiB in one Pod. Three inputs make that admissible and
# Terraform refuses the plan without them: raw mode is turned on explicitly,
# the reference CPU pool advertises measured schedulable capacity at least that
# large, and core-resource admission is configured so Kueue counts cpu and
# memory at all. The pool is a 32 vCPU / 128 GB class node because Kubernetes
# allocatable is lower than a preset's nominal size, so a nominal 16/64 node
# cannot hold this Pod.
#
# Run it from k8s-inference with ./inference-stack.

deployment = {
  schema_version = 1
  name           = "inference-academic-raw-af3"

  target = {
    project_id   = "project-yourprojectid"
    project_name = "my-inference-project"
    region       = "eu-north1"
    network = {
      network_name        = "default-network"
      subnet_name         = "default-subnet"
      private_subnet_cidr = "10.0.0.0/16"
    }
    system_update_strategy = { max_surge = 1, max_unavailable = 0 }
  }

  profiles = {
    capacity     = "minimal"
    accelerators = "minimal"
    models       = "none"
  }

  cluster = {
    # Inside the Kueue and JobSet upstream-tested intersection, which enabling
    # scientific batch requires.
    kubernetes_version = "1.34"
  }

  accelerator_pools = {
    "h100-warm" = {
      platform          = "gpu-h100-sxm"
      preset            = "8gpu-128vcpu-1600gb"
      accelerator_class = "nvidia-h100-sxm5-80gb"
      gpus_per_node     = 8
      gpu_memory_gb     = 80
      # Regular capacity that stays up, so admitted work is not reclaimed.
      capacity_type = "regular"
      min_nodes     = 1
      max_nodes     = 1
      driver        = { mode = "managed", preset = "cuda12.4" }
      # Illustrative plan fixture below the 128 vCPU / 1600 GB preset. The
      # fixture source names the exact UTF-8 bytes hashed by payload_sha256 and
      # makes no live-measurement claim. Replace this whole record with the
      # target pool's observed allocatable and provenance before deployment.
      schedulable_capacity = {
        cpu_millicores = 124000
        memory_mib     = 1540096
        evidence = {
          pool_id        = "h100-warm"
          source         = "fixture:utf8:h100-warm"
          captured_at    = "2026-09-03T06:00:00Z"
          payload_sha256 = "8ec94b7bc5006d1cf2ceb136f49e16653911b9544d5f93dd8a908afc4605daaf"
        }
      }
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
      schedulable_capacity = {
        cpu_millicores = 124000
        memory_mib     = 1540096
        evidence = {
          pool_id        = "h100-preemptible"
          source         = "fixture:utf8:h100-preemptible"
          captured_at    = "2026-09-03T06:00:00Z"
          payload_sha256 = "11260e53045dd4593a457ddb7ce1b36ba2dd02d09ece49813287bb7af0bd1a2e"
        }
      }
    }
  }

  models = {
    selection = "profile"
  }

  # Enabling batch execution requires the scientific artifact store, which
  # holds every result the controller commits, and installs the pinned JobSet
  # API for true-gang stages.
  scientific_batch = {
    enabled = true
  }

  scheduling = {
    cohort = { enabled = true, name = "inference-shared" }
    # The stable ClusterQueue serves the model lane and the licensed GPU lane,
    # so Kueue orders them by decayed fair-share usage before priority.
    fair_share_precedence_acknowledged = true
    # Licensed models run their own raw CPU data stages here.
    academic_raw_data_stages = true
    # AlphaFold 3 is a scientific-only model, so it has no serving placement to
    # derive eligibility from. The operator states which pools it is qualified
    # for, and both are listed so its GPU stage can start on the warm pool and
    # burst onto preemptible capacity. Terraform checks every ID against the
    # pools this deployment actually declares; it is a declaration of
    # qualification, not a guess from a pool name.
    model_eligible_pool_ids = {
      alphafold3 = ["h100-warm", "h100-preemptible"]
    }
    # Alphabetical pool order would try h100-preemptible before h100-warm, so
    # every Workload would land on capacity that can be reclaimed while a warm
    # node sits idle. One setting fixes it: the stable ClusterQueue searches
    # warm capacity first, and every service class inherits that order because
    # none of them overrides it. A class that does set pool_preference must
    # name the same order as the queue it routes to, and Terraform refuses the
    # plan when they disagree.
    default_queue_pool_order = ["h100-warm", "h100-preemptible"]
    # Count cpu and memory in Kueue admission. This removes them from Kueue's
    # exclusions, so every cpu/memory quota in the cluster starts being
    # enforced instead of dropped before admission, and it couples core
    # capacity to each accelerator pool: cpu and memory join that pool's own
    # resourceGroup, so a Workload reserves cores on the pool that granted its
    # accelerators. Each pool's budget is derived from the measured per-node
    # capacity declared above times its maximum node count, so there is no
    # aggregate to keep in step. The reference-data CPU pool is not part of
    # it: its ClusterQueue is external, with its own flavor and quota.
    budget_core_resources = true
  }

  storage = {
    # Batch execution commits every result here, so the store is enabled with
    # the exact object-storage addresses the control plane may reach. The
    # default media types already cover the scientific formats.
    scientific_artifacts = {
      enabled      = true
      egress_cidrs = ["203.0.113.10/32"]
    }
    reference_data = {
      enabled = true
      cpu_pool = {
        # 32 vCPU / 128 GB class. The declared capacity is conservative
        # measured allocatable, not the preset's nominal size, and it must hold
        # one 16 CPU / 64 GiB stage Pod.
        platform   = "cpu-d3"
        preset     = "32vcpu-128gb"
        node_count = 1
        schedulable_capacity = {
          cpu_millicores        = 30000
          memory_mib            = 122880
          ephemeral_storage_mib = 114688
        }
      }
      filesystem = {
        size_gib = 2048
      }
      object_storage = {
        max_size_gib = 2048
      }
      queue = {
        # At least one whole raw stage Pod, so the stage can be admitted rather
        # than queued forever behind a quota smaller than itself.
        nominal_cpu    = "24"
        nominal_memory = "96Gi"
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

# The licensed assets and their execution identity. The claim can only be
# mounted from its own namespace, so both lanes live there.
academic_assets = {
  enabled   = true
  tenant_id = "tenant-academic"
  namespace = "fs2-academic-poc"
  execution = { enabled = true }
  assets = {
    alphafold3-parameters = {
      model_id      = "alphafold3"
      relative_path = "alphafold3/af3.bin.zst"
      runtime_binding = {
        artifact_id     = "alphafold3-parameters"
        source_sub_path = "alphafold3/af3.bin.zst"
        consumer_path   = "/models/af3.bin.zst"
        mechanism       = "subpath-file-mount"
      }
    }
  }
}

# The general CPU lane must reach Kueue and the chart exactly, and must fail
# closed when the scheduling contract producer is absent.
#
# The lane is only useful if a stage can actually be admitted through it, so
# these runs assert a real CPU admission tuple rather than the presence of a
# block: the queue a Job names, the namespace it runs in, the flavor it admits
# through, the pool it may land on, and the cpu/memory a Pod may request.

mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "random" {}

# Reuse the stage-wide inputs the existing stage tests already pin.
variables {
  run_root        = "/tmp/fs2-modelexpress-test"
  kubeconfig_path = "/tmp/fs2-modelexpress-test/kubeconfig"
  run_id          = "mxtest01"
  cluster_id      = "mk8scluster-modelexpresstest"
  cluster_name    = "fs2-modelexpress-test"
  kube_context    = "fs2-modelexpress-test"
  kube_system_uid = "00000000-0000-0000-0000-000000000001"
  project_id      = "project-modelexpresstest"

  target_contract = {
    project_id                 = "project-modelexpresstest"
    project_name               = "modelexpress-test"
    region                     = "us-north1"
    network_name               = "modelexpress-test-network"
    subnet_name                = "modelexpress-test-subnet"
    private_subnet_cidr        = "10.104.0.0/13"
    source_registry_project_id = "project-modelexpresstest"
    system_update_strategy = {
      max_surge       = 1
      max_unavailable = 0
    }
    tenant_id = "tenant-modelexpresstest"
    source_registry = {
      id         = "registry-modelexpresstest"
      project_id = "project-modelexpresstest"
      fqdn       = "cr.us-north1.nebius.cloud"
    }
  }

  accelerator_pool_contract = {
    schema        = "fs2-serve.nebius.ai/terraform-accelerator-pools/v2"
    source_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    profile       = "custom"
    floor_profile = "zero"
    target_region = "us-north1"
    capacity_ownership = {
      owner_root                 = "infra-disposable"
      override_mode              = "capacity-only-patch"
      override_fields            = ["min_nodes", "max_nodes"]
      requested_overrides        = {}
      requested_overrides_sha256 = sha256(jsonencode({}))
    }
    artifact_source = {
      deprecated = true
      registry = {
        id           = "registry-modelexpresstest"
        project_id   = "project-modelexpresstest"
        project_name = "modelexpress-test"
        region       = "us-north1"
        fqdn         = "cr.us-north1.nebius.cloud"
      }
      closure_schema             = "fs2-serve.nebius.ai/source-registry-closure/v1"
      closure_sha256             = "8c612fc8246e9f937156dda6dcb5a19dd778db2fe4589d07b44f6af93740d43d"
      cross_region_pull_required = false
    }
    pools = {
      nebius-b300-preemptible-1x = {
        id                = "nebius-b300-preemptible-1x"
        accelerator_class = "nvidia-b300-sxm6-288gb"
        resource_api      = { mode = "extended-resource", resource_name = "nvidia.com/gpu" }
        provider = {
          name                   = "nebius"
          platform               = "gpu-b300-sxm"
          preset                 = "1gpu-24vcpu-346gb"
          node_group_name_suffix = "b300-1x"
          node_group_label       = "gpu-b300-1x"
          os                     = "ubuntu24.04"
          driver                 = { owner = "provider-managed", preset = "cuda13.0" }
          reservation_policy     = "FORBID"
          reservation_ids        = []
        }
        node = {
          gpus_per_node         = 1
          gpu_memory_gb_nominal = 288
          vcpu_count            = 24
          memory_gib            = 346
          host_architectures    = ["amd64"]
          topology              = "single-gpu"
          boot_disk             = { size_gib = 320, type = "NETWORK_SSD" }
          drain_timeout         = "15m"
        }
        capacity = {
          type            = "preemptible"
          min_nodes       = 0
          max_nodes       = 1
          source          = "customer-tfvars"
          profile_bounds  = { min_nodes = 0, max_nodes = 1 }
          scale_from_zero = true
        }
        scheduling = {
          stable_node_labels = {
            "workload.fs2.nebius/gpu"        = "true"
            "accelerator.fs2.nebius/class"   = "nvidia-b300-sxm6-288gb"
            "accelerator.fs2.nebius/pool-id" = "nebius-b300-preemptible-1x"
            "capacity.fs2.nebius/type"       = "preemptible"
            "capacity.fs2.nebius/pool"       = "burst"
            "capacity.fs2.nebius/preset"     = "b300-1x"
            "capacity.fs2.nebius/gpu-count"  = "1"
            "topology.fs2.nebius/scope"      = "single-gpu"
            "storage.fs2.nebius/tier"        = "sfs-conventional"
            "snapshot.fs2.nebius/eligible"   = "false"
            "local-nvme.fs2.nebius/eligible" = "false"
          }
          resource_flavor_name = "fs2-b300-1x"
          taints = [{
            key = "dedicated", value = "fs2-inference", effect = "NO_SCHEDULE"
          }]
          tolerations = [{
            key = "dedicated", operator = "Equal", value = "fs2-inference", effect = "NoSchedule"
          }]
          forbidden_scale_zero_selectors = ["kubernetes.io/arch"]
        }
        features = {
          mig               = { mode = "disabled", resource_strategy = "single" }
          local_storage     = { mode = "none", provider_config = "none" }
          shared_filesystem = true
          local_cache       = "shared-filesystem"
          gpu_snapshot      = "ineligible"
        }
        region_availability = [{
          region = "us-north1", state = "live-preflight-required", capacity_modes = ["preemptible"]
        }]
        state = "customer-specified"
        evidence = {
          hardware_state = "live-preflight-required"
          reference      = "synthetic non-live Terraform test"
        }
      }
    }
  }

  public_edge_contract = {
    schema                  = "fs2-serve.nebius.ai/public-edge/v1"
    mode                    = "internal-only"
    transport               = "kubectl-port-forward"
    public_origin           = null
    allocation_project_id   = null
    allocation_id           = null
    public_ipv4_address     = null
    external_traffic_policy = "Cluster"
    service_ports = {
      http  = { listener_port = 80, target_port = 10080, node_port = 30080 }
      https = { listener_port = 443, target_port = 10443, node_port = 30443 }
    }
    port_forward = {
      enabled                  = true
      bind_address             = "127.0.0.1"
      application_origin       = "http://localhost:18082"
      operator_endpoint        = "http://127.0.0.1:18082"
      operator_proxy_port      = 18082
      control_plane_service    = "fs2-serve-control-plane"
      control_plane_port       = 8080
      control_plane_local_port = 18080
      admin_console_service    = "fs2-serve-control-plane-admin-console"
      admin_console_port       = 8080
      admin_console_local_port = 18081
    }
    security_group_destination_ports = []
  }

  deployment_profile = "full_catalog"
  enabled_model_ids  = ["qwen3-8b"]
  model_image_overrides = {
    qwen3-8b = "registry.example.test/fs2/qwen3-8b@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
  }
  model_pool_overrides = { qwen3-8b = "nebius-b300-preemptible-1x" }
  model_scaling_mode   = "keda"
  model_controller = {
    enabled             = true
    writes_enabled      = true
    workload_owner      = "controller"
    bootstrap_model_ids = ["qwen3-8b"]
    fresh_install       = true
    handoff_receipt     = null
    priority_classes    = { interactive = 100, standard = 0, batch = -100 }
  }
  model_express = {
    enabled          = true
    deployment_mode  = "managed"
    endpoint         = "fs2-modelexpress.fs2-modelexpress.svc.cluster.local:8001"
    metadata_backend = "kubernetes"
    namespace        = "fs2-modelexpress"
    server_image = {
      repository = "nvcr.io/nvidia/ai-dynamo/modelexpress-server"
      digest     = "sha256:9999999999999999999999999999999999999999999999999999999999999999"
    }
    # storage_class is deliberately omitted: this is the production default
    # that previously failed when coalesce(null, "") was evaluated.
    cache = { enabled = true, size_gib = 100 }
    models = {
      qwen3-8b = {
        runtime_adapter        = "vllm"
        client_package_version = "0.5.1"
      }
    }
  }
  nvcrio_dockerconfigjson = "{\"auths\":{}}"
  # The lane budgets cpu and memory, so core admission is on. These are one
  # truth at the facade: budget_core_resources is exactly
  # scheduling.core_capacity != null, and the root refuses an enabled general
  # CPU lane without it.
  budget_core_resources = true

  scheduling = {
    cohort                             = { enabled = true, name = "inference-shared" }
    fair_share_precedence_acknowledged = true
    # Pool-coupled: each pool's budget is its measured per-node schedulable
    # capacity times its maximum node count, so cpu and memory ride on that
    # pool's own ResourceFlavor.
    core_pool_capacity = {
      nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
    }
  }

  general_cpu_lane = {
    enabled             = true
    cluster_queue       = "general-cpu"
    local_queue         = "general-cpu"
    resource_flavor     = "general-cpu"
    queueing_strategy   = "BestEffortFIFO"
    fair_sharing_weight = 1
    namespace           = "fs2-academic-poc"
  }

  general_cpu_pools = {
    schema     = "fs2-serve.nebius.ai/general-cpu-pools/v1"
    project_id = "project-modelexpresstest"
    region     = "us-north1"
    node_selector = {
      "workload.fs2.nebius/general-cpu" = "true"
      "capacity.fs2.nebius/pool"        = "general-cpu"
    }
    taint = {
      key    = "workload.fs2.nebius/general-cpu"
      value  = "true"
      effect = "NoSchedule"
    }
    pools = {
      general-cpu-8x = {
        id              = "mk8snodegroup-generalcputest"
        name            = "fs2-general-cpu-8x"
        platform        = "cpu-d3"
        preset          = "8vcpu-32gb"
        capacity_type   = "preemptible"
        elastic         = true
        min_nodes       = 0
        max_nodes       = 4
        scale_from_zero = true
        schedulable_capacity = {
          cpu_millicores        = 7000
          memory_mib            = 28672
          ephemeral_storage_mib = 114688
        }
        shared_filesystem = false
        node_labels = {
          "workload.fs2.nebius/general-cpu" = "true"
          "capacity.fs2.nebius/type"        = "preemptible"
          "capacity.fs2.nebius/pool"        = "general-cpu"
          "capacity.fs2.nebius/pool-id"     = "general-cpu-8x"
          "storage.fs2.nebius/shared-cache" = "false"
        }
      }
    }
    reference_data_filesystem = false
  }
}


run "without_a_general_pool_the_lane_is_absent" {
  command = plan

  variables {
    general_cpu_pools = null
  }

  plan_options {
    target = [terraform_data.general_cpu_contract]
  }

  assert {
    condition     = terraform_data.general_cpu_contract.input.enabled == false
    error_message = "With no general CPU pool contract the lane must stay absent."
  }

  assert {
    condition     = length(terraform_data.general_cpu_contract.input.cpu_classes) == 0
    error_message = "An absent lane must publish no general-cpu class."
  }
}


run "a_general_pool_yields_a_real_cpu_admission_tuple" {
  command = plan

  plan_options {
    target = [terraform_data.general_cpu_contract]
  }

  assert {
    condition     = terraform_data.general_cpu_contract.input.enabled
    error_message = "A declared general CPU pool must enable the lane."
  }

  # The exact tuple a controller resolves and persists for a CPU stage.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].cluster_queue == "general-cpu" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].local_queue == "general-cpu" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].namespace == "fs2-academic-poc" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].resource_flavor == "general-cpu" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].eligible_pool_ids == ["general-cpu-8x"] &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].pool_resolution.mode == "per-pool-flavor" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].pool_resolution.pool_id == "general-cpu-8x" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].schedulable_capacity.cpu == "7000m" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].schedulable_capacity.memory == "28672Mi" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].schedulable_capacity.ephemeral_storage == "114688Mi"
    )
    error_message = "The general-cpu class must resolve an exact queue, namespace, flavor, pool and cpu/memory quantity tuple."
  }

  # Placement must be complete: a CPU stage inherits no accelerator flavor, so
  # without these it would never schedule onto the tainted pool.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].node_selector["capacity.fs2.nebius/pool"] == "general-cpu" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].node_selector["capacity.fs2.nebius/pool-id"] == "general-cpu-8x" &&
      length(terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].tolerations) == 1 &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].tolerations[0].key == "workload.fs2.nebius/general-cpu" &&
      terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"].tolerations[0].effect == "NoSchedule"
    )
    error_message = "The class must carry the complete general-CPU selector and toleration."
  }

  # Quota is measured capacity times the authorized ceiling.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.capacity.nominal_cpu == "28000m" &&
      terraform_data.general_cpu_contract.input.capacity.nominal_memory == "114688Mi"
    )
    error_message = "The lane quota must be measured per-node capacity times the maximum node count."
  }

  assert {
    condition     = terraform_data.general_cpu_contract.input.elasticity.scale_from_zero
    error_message = "A zero-floor elastic pool must be reported as scale-from-zero."
  }

  # No cohort, so this lane can neither borrow reference-database capacity nor
  # lend its own to accelerator work.
  assert {
    condition     = terraform_data.general_cpu_contract.input.cohort == null
    error_message = "The general CPU lane must join no cohort."
  }
}

run "the_cpu_cluster_queue_preempts_lower_priority_work_in_queue" {
  command = plan

  plan_options {
    # The lane contract carries the same rendered manifests, and targeting it
    # keeps this run on the dependency set the other runs already resolve.
    target = [terraform_data.general_cpu_contract]
  }

  # Kueue defaults withinClusterQueue to Never. Without this an interactive or
  # presentation CPU stage waits behind admitted bulk work on the only lane
  # that can run it, whatever WorkloadPriorityClass it carries, and because
  # this queue joins no cohort in-queue displacement is its only mechanism.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.manifests.cluster_queue.spec.preemption.withinClusterQueue == "LowerPriority" &&
      terraform_data.general_cpu_contract.input.manifests.cluster_queue.spec.preemption.reclaimWithinCohort == "Never"
    )
    error_message = "The general CPU ClusterQueue must preempt lower-priority work within the queue and never reclaim across a cohort it does not join."
  }
}

run "only_the_general_cpu_class_is_contributed" {
  command = plan

  plan_options {
    target = [terraform_data.general_cpu_contract]
  }

  # This producer contributes its own class and nothing else: the reference-data
  # class belongs to its owner, and merging it here would fork the assembly.
  assert {
    condition = (
      length(terraform_data.general_cpu_contract.input.scheduling_contribution.cpu_classes) == 1 &&
      contains(keys(terraform_data.general_cpu_contract.input.scheduling_contribution.cpu_classes), "general-cpu") &&
      terraform_data.general_cpu_contract.input.scheduling_contribution.cpu_classes_schema == "fs2-serve.nebius.ai/cpu-stage-classes/v1"
    )
    error_message = "The producer must contribute exactly the canonical general-cpu class entry."
  }

  # The entry digest is what lets a consumer prove the assembled contract
  # carries this exact revision rather than merely mentioning the class name.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.scheduling_contribution.cpu_class_digests["general-cpu"] ==
      sha256(jsonencode(terraform_data.general_cpu_contract.input.cpu_classes["general-cpu"]))
    )
    error_message = "The published class digest must be the digest of the exact entry contributed."
  }

  # Ownership facts describe the queues this producer created, so the assembler
  # can reference them without becoming their owner.
  assert {
    condition = (
      terraform_data.general_cpu_contract.input.scheduling_contribution.external_lane_facts.cluster_queue == "general-cpu" &&
      terraform_data.general_cpu_contract.input.scheduling_contribution.external_lane_facts.local_queue == "general-cpu" &&
      terraform_data.general_cpu_contract.input.scheduling_contribution.external_lane_facts.namespace == "fs2-academic-poc" &&
      terraform_data.general_cpu_contract.input.scheduling_contribution.external_lane_facts.nominal_cpu == "28000m"
    )
    error_message = "The contribution must carry the exact ownership facts of the queues it created."
  }
}

run "core_exclusion_blocks_the_lane_before_any_infrastructure_changes" {
  command = plan

  plan_options {
    target = [terraform_data.general_cpu_core_admission]
  }

  variables {
    # Kueue's default manager configuration filters cpu and memory out of
    # admission, so the lane's quotas would exist but never be enforced.
    budget_core_resources = false
  }

  expect_failures = [terraform_data.general_cpu_core_admission]
}

run "core_budgeting_gives_every_accelerator_queue_core_capacity" {
  command = plan

  plan_options {
    target = [terraform_data.general_cpu_core_admission]
  }

  # Turning core budgeting on makes cpu and memory part of every queue's
  # arithmetic, so an accelerator ClusterQueue that budgeted only GPUs would
  # stop admitting. The assembler renders a core resourceGroup on every
  # ClusterQueue it owns from the same core_capacity, so the condition this
  # gate exists to catch cannot arise while both come from one contract.
  assert {
    condition     = length(local.accelerator_queues_missing_core) == 0
    error_message = "With core admission on, every accelerator ClusterQueue must cover cpu and memory or it stops admitting GPU work."
  }

  assert {
    condition     = terraform_data.general_cpu_core_admission.input.quota_enforced
    error_message = "An enabled general CPU lane with core budgeting on must report its quota as enforced."
  }
}

run "rejects_a_pool_contract_produced_for_another_project_or_region" {
  command = plan

  plan_options {
    target = [terraform_data.general_cpu_pool_target_binding]
  }

  variables {
    # A staged output copied from another cluster. Every node group ID in it
    # belongs to that cluster, so a ResourceFlavor rendered from it would
    # select nodes that do not exist here.
    general_cpu_pools = {
      schema     = "fs2-serve.nebius.ai/general-cpu-pools/v1"
      project_id = "project-someoneelse"
      region     = "us-north1"
      node_selector = {
        "workload.fs2.nebius/general-cpu" = "true"
        "capacity.fs2.nebius/pool"        = "general-cpu"
      }
      taint = {
        key    = "workload.fs2.nebius/general-cpu"
        value  = "true"
        effect = "NoSchedule"
      }
      reference_data_filesystem = false
      pools = {
        general-cpu-8x = {
          id              = "mk8snodegroup-generalcpu8x"
          name            = "fs2-mxtest-general-cpu-8x"
          platform        = "cpu-d3"
          preset          = "8vcpu-32gb"
          capacity_type   = "preemptible"
          elastic         = true
          min_nodes       = 0
          max_nodes       = 4
          scale_from_zero = true
          schedulable_capacity = {
            cpu_millicores        = 7000
            memory_mib            = 28672
            ephemeral_storage_mib = 114688
          }
          shared_filesystem = false
          node_labels = {
            "workload.fs2.nebius/general-cpu" = "true"
            "capacity.fs2.nebius/pool"        = "general-cpu"
            "capacity.fs2.nebius/pool-id"     = "general-cpu-8x"
          }
        }
      }
    }
  }

  expect_failures = [terraform_data.general_cpu_pool_target_binding]
}

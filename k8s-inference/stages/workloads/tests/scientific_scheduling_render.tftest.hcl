# The rendered scheduling contract, from the workloads stage itself.
#
# The root facade only checks its own preflight, so a facade plan proves
# nothing about what this stage hands the controller. These runs instantiate
# the real stage with academic execution, raw CPU data stages, and the
# reference-data plane switched on, and assert the exact tuples a consumer
# resolves: the licensed GPU lane, the licensed CPU lane on the externally
# owned reference ClusterQueue, the CPU class placement, and AlphaFold 3's
# declared two-pool eligibility.

mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "random" {}

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

  # The reference-data plane, whose CPU ClusterQueue the licensed CPU lane
  # points at. Its pool is a 32 vCPU / 128 GB class node with conservative
  # measured capacity, so one 16 CPU / 64 GiB stage Pod fits.
  reference_data = {
    enabled   = true
    namespace = "fs2-reference-data"
    queue = {
      resource_flavor = "reference-data-cpu"
      cluster_queue   = "reference-data-cpu"
      local_queue     = "reference-data"
      nominal_cpu     = "24"
      nominal_memory  = "96Gi"
    }
    network = {
      allow_public_source_staging = false
      allow_public_msa_opt_in     = false
    }
    status = {
      enabled                 = false
      replicas                = 1
      service_monitor_enabled = false
    }
    pipeline = {
      enabled                 = false
      bundle_id               = "alphafold3-public-databases-v3.0"
      generation              = 1
      cpu                     = "6"
      memory                  = "24Gi"
      ephemeral_storage       = "2Gi"
      active_deadline_seconds = 604800
      backoff_limit           = 6
    }
    storage_contract = {
      schema     = "fs2-serve.nebius.ai/reference-data-storage/v1"
      project_id = "project-modelexpresstest"
      region     = "us-north1"
      cpu_pool = {
        id         = "mk8snodegroup-reference-mxtest"
        name       = "reference-data-cpu"
        platform   = "cpu-d3"
        preset     = "32vcpu-128gb"
        node_count = 1
        capacity   = "regular"
        schedulable_capacity = {
          cpu_millicores        = 30000
          memory_mib            = 122880
          ephemeral_storage_mib = 114688
        }
        node_labels = {
          "workload.fs2.nebius/reference-data" = "true"
          "capacity.fs2.nebius/pool"           = "reference-data"
          "storage.fs2.nebius/reference-data"  = "true"
        }
        taint = {
          key    = "workload.fs2.nebius/reference-data"
          value  = "true"
          effect = "NoSchedule"
        }
      }
      filesystem = {
        id               = "computefilesystem-referenceschedtest"
        size_gib         = 2048
        type             = "NETWORK_SSD"
        block_size_bytes = 4096
        forbid_deletion  = false
        node_mount_path  = "reference-data"
        host_path        = "/mnt/reference-data"
        uri              = "filesystem://computefilesystem-referenceschedtest"
      }
      object_storage = {
        id                = "bucket-referenceschedtest"
        name              = "fs2-reference-data-mxtest"
        endpoint          = "https://storage.us-north1.nebius.cloud"
        max_size_gib      = 2048
        versioning_policy = "ENABLED"
        object_prefix     = "reference-data"
      }
      layout = {
        blobs                 = "blobs"
        manifests             = "manifests"
        filesystem_datasets   = "datasets"
        preprocessing_inputs  = "inputs"
        preprocessing_outputs = "outputs"
      }
      sizing = {
        official_alphafold3_expanded_bytes = 676457349939
        required_headroom_bytes            = 1099511627776
        minimum_size_gib                   = 1611
      }
      public_msa_default = false
      lifecycle = {
        retention_mode     = "disposable"
        destroy_status     = "eligible-only-while-bucket-empty"
        destroy_completion = "full-only-when-versioned-bucket-empty"
        adoption_status    = "not-required"
      }
    }
    object_storage_access = {
      access_key_id       = "accesskey-schedtest"
      secret_reference_id = "secret-schedtest"
      revision            = 1
    }
  }

  academic_assets = {
    enabled        = true
    project_id     = "project-schedtest"
    region         = "eu-north1"
    tenant_id      = "tenant-academic"
    institution_id = null
    namespace      = "fs2-academic-poc"
    runtime_claim = {
      name          = "academic-assets-runtime-rwx"
      storage_gib   = 128
      storage_class = "csi-mounted-fs-path-sc"
      access_mode   = "ReadWriteMany"
      lifecycle     = "disposable"
    }
    legacy_quarantine_claim = {
      enabled     = false
      namespace   = "fs2-models"
      name        = "cancer-immunotherapy-academic-assets-rwx-v1"
      storage_gib = 128
      retain      = false
    }
    delivery = {
      mode                    = "tenant-private-volume"
      mount_root              = "/opt/fs2/academic"
      asset_gid               = 65532
      consumer_access         = "supplemental-group"
      world_readable          = false
      embed_licensed_bytes    = false
      general_shared_cache    = false
      deny_egress_on_validate = true
    }
    assets = {
      alphafold3-parameters = {
        model_id      = "alphafold3"
        relative_path = "alphafold3/af3.bin.zst"
      }
    }
    readiness_manifest_sha256 = "2b5a21f8eca6d8e465f29c508a6717915b84e73cb351d24811223a70228a3e36"
  }
  scheduling = {
    cohort                             = { enabled = true, name = "inference-shared" }
    fair_share_precedence_acknowledged = true
    academic_raw_data_stages           = true
    # Pool-coupled: each pool's budget is its measured per-node schedulable
    # capacity times its maximum node count, so cpu and memory ride on that
    # pool's own ResourceFlavor.
    core_pool_capacity = {
      nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
    }
    # AlphaFold 3 is scientific-only, so its qualification is declared rather
    # than derived from a serving placement. This fixture deploys one pool;
    # the warm-plus-burst set is proved where two pools exist, in
    # modules/kueue-scheduling and in the acceptance renderer.
    model_eligible_pool_ids = {
      alphafold3 = ["nebius-b300-preemptible-1x"]
    }
  }
}

run "academic_chart_receives_both_stage_queues" {
  command = plan

  plan_options {
    target = [terraform_data.academic_assets_contract]
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.helm_values.execution.localQueue == "academic-scientific" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.clusterQueue == "inference-accelerators" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.referenceDataLocalQueue == "academic-scientific-cpu" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.referenceDataClusterQueue == "reference-data-cpu"
    )
    error_message = "The Helm handoff must identify both the academic accelerator lane and the namespace-local reference-data CPU lane."
  }
}

run "licensed_lanes_and_cpu_class_are_rendered_by_the_stage" {
  command = plan

  plan_options {
    target = [module.kueue_scheduling.terraform_data.contract]
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.local_queues["academic-scientific"].metadata.namespace ==
      "fs2-academic-poc" &&
      module.kueue_scheduling.contract.local_queues["academic-scientific"].spec.clusterQueue ==
      "inference-accelerators" &&
      module.kueue_scheduling.contract.local_queue_routes["academic-scientific"].namespace ==
      "fs2-academic-poc" &&
      join(",", module.kueue_scheduling.contract.local_queue_routes["academic-scientific"].tenant_ids) ==
      "tenant-academic" &&
      join(",", module.kueue_scheduling.contract.local_queue_routes["academic-scientific"].model_ids) ==
      "alphafold3" &&
      length(module.kueue_scheduling.contract.local_queue_routes["academic-scientific"].service_classes) == 5
    )
    error_message = "The licensed GPU lane must be an exact tenant/model route in the claim namespace, for every service class."
  }

  assert {
    condition = (
      contains(
        module.kueue_scheduling.contract.cluster_queue_namespaces["inference-accelerators"],
        "fs2-academic-poc",
      ) &&
      contains(
        module.kueue_scheduling.contract.cluster_queue_namespaces["reference-data-cpu"],
        "fs2-academic-poc",
      )
    )
    error_message = "Both the accelerator ClusterQueue and the externally owned reference CPU ClusterQueue must admit the claim namespace."
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.local_queues["academic-scientific-cpu"].metadata.namespace ==
      "fs2-academic-poc" &&
      module.kueue_scheduling.contract.local_queues["academic-scientific-cpu"].spec.clusterQueue ==
      "reference-data-cpu" &&
      length(module.kueue_scheduling.contract.local_queue_routes["academic-scientific-cpu"].model_ids) == 0 &&
      length(module.kueue_scheduling.contract.local_queue_routes["academic-scientific-cpu"].service_classes) == 0
    )
    error_message = "The CPU data-stage lane must be route-less in the claim namespace on the reference ClusterQueue, because a tenant/model/class tuple cannot say whether a stage is CPU or GPU."
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.cpu_classes["reference-data"].local_queue ==
      "academic-scientific-cpu" &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].cluster_queue ==
      "reference-data-cpu" &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].namespace ==
      "fs2-academic-poc" &&
      one(module.kueue_scheduling.contract.cpu_classes["reference-data"].tolerations).effect ==
      "NoSchedule" &&
      length(module.kueue_scheduling.contract.cpu_classes["reference-data"].node_selector) >= 1 &&
      module.kueue_scheduling.contract.cpu_stage_execution == "available"
    )
    error_message = "The reference-data CPU class must carry its queue, namespace, selector, and toleration, or a CPU stage cannot be scheduled on the tainted pool."
  }

  # Producer validation of the class the stage actually renders, against every
  # requirement the published class contract states. A schema file that no
  # producer is checked against is a proposal, not evidence.
  assert {
    condition = (
      module.kueue_scheduling.contract.cpu_classes_schema ==
      "fs2-serve.nebius.ai/cpu-stage-classes/v1" &&
      # Expected pools, and how the actual one becomes knowable. One pool
      # behind one flavor here, so the admission itself answers it.
      join(",", module.kueue_scheduling.contract.cpu_classes["reference-data"].eligible_pool_ids) ==
      module.kueue_scheduling.contract.cpu_classes["reference-data"].pool_resolution.pool_id &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].pool_resolution.mode ==
      "per-pool-flavor" &&
      # The key a mode does not use is absent, not null: the published class
      # schema forbids its presence, and Terraform's typed object would have
      # filled it with null.
      !contains(keys(module.kueue_scheduling.contract.cpu_classes["reference-data"].pool_resolution), "node_label_key") &&
      # Raw quantities beside the normalized integers, from the same measured
      # capacity, so neither can drift from the other.
      module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.cpu ==
      "${module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.cpu_millicores}m" &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.memory ==
      "${module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.memory_mib}Mi" &&
      # One digest per entry, so a consumer can tell which class changed.
      can(regex("^[0-9a-f]{64}$", module.kueue_scheduling.contract.cpu_class_digests["reference-data"])) &&
      # The exact key set the published class schema defines, and nothing
      # else. These are the bytes that go into the ConfigMap, so an extra key
      # or a typed null would be published and would fail validation there.
      join(",", sort(keys(module.kueue_scheduling.contract.cpu_classes["reference-data"]))) ==
      join(",", sort([
        "local_queue", "cluster_queue", "namespace", "resource_flavor",
        "eligible_pool_ids", "pool_resolution", "node_selector", "tolerations",
        "schedulable_capacity",
      ])) &&
      join(",", sort(keys(module.kueue_scheduling.contract.cpu_classes["reference-data"].pool_resolution))) ==
      "mode,pool_id" &&
      join(",", sort(keys(module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity))) ==
      join(",", sort([
        "cpu", "memory", "ephemeral_storage",
        "cpu_millicores", "memory_mib", "ephemeral_storage_mib",
      ])) &&
      length(module.kueue_scheduling.contract.cpu_classes["reference-data"].resource_flavor) >= 1 &&
      length(module.kueue_scheduling.contract.cpu_classes["reference-data"].node_selector) >= 1 &&
      length(module.kueue_scheduling.contract.cpu_classes["reference-data"].node_selector) <= 16 &&
      length(module.kueue_scheduling.contract.cpu_classes["reference-data"].tolerations) <= 8 &&
      alltrue([
        for toleration in module.kueue_scheduling.contract.cpu_classes["reference-data"].tolerations :
        contains(["Equal", "Exists"], toleration.operator) &&
        contains(["NoSchedule", "PreferNoSchedule", "NoExecute"], toleration.effect) &&
        (
          toleration.operator == "Equal"
          ? try(length(toleration.value), 0) >= 1
          : try(toleration.value, null) == null
        )
      ]) &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.cpu_millicores >= 1 &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.memory_mib >= 1 &&
      module.kueue_scheduling.contract.cpu_classes["reference-data"].schedulable_capacity.ephemeral_storage_mib >= 0 &&
      # Only one class exists here. general-cpu has a different producer and
      # is deliberately absent rather than approximated.
      join(",", keys(module.kueue_scheduling.contract.cpu_classes)) == "reference-data"
    )
    error_message = "The rendered reference-data class must satisfy every executable-placement requirement its published class contract states, and no class this repository does not produce may appear."
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.cpu_stage_requests["reference-data"].cpu_millicores == 16000 &&
      module.kueue_scheduling.contract.cpu_stage_requests["reference-data"].memory_mib == 65536
    )
    error_message = "The canonical raw AlphaFold 3 data-stage request must be published so a consumer can check it against the per-node capacity."
  }

  assert {
    condition = (
      join(",", module.kueue_scheduling.contract.model_eligible_pool_ids["alphafold3"]) ==
      "nebius-b300-preemptible-1x" &&
      contains(keys(module.kueue_scheduling.contract.model_eligible_pool_ids), "qwen3-8b")
    )
    error_message = "A declared scientific-only qualification and every selected serving model's derived eligibility must both reach the contract."
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.pool_node_label_key == "accelerator.fs2.nebius/pool-id" &&
      module.kueue_scheduling.contract.core_resource_admission == "budgeted"
    )
    error_message = "The canonical pool label and the core-admission state must be published truthfully."
  }
}

run "a_routed_model_without_declared_eligibility_is_rejected" {
  command = plan

  variables {
    scheduling = {
      cohort                             = { enabled = true, name = "inference-shared" }
      fair_share_precedence_acknowledged = true
      academic_raw_data_stages           = true
      core_pool_capacity = {
        nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
      }
      model_eligible_pool_ids = {}
    }
  }

  plan_options {
    target = [terraform_data.academic_lane_ownership]
  }

  expect_failures = [terraform_data.academic_lane_ownership]
}

run "a_declaration_cannot_overwrite_an_authoritative_placement" {
  command = plan

  variables {
    scheduling = {
      cohort                             = { enabled = true, name = "inference-shared" }
      fair_share_precedence_acknowledged = true
      academic_raw_data_stages           = true
      core_pool_capacity = {
        nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
      }
      model_eligible_pool_ids = {
        alphafold3 = ["nebius-b300-preemptible-1x"]
        # qwen3-8b has a placement contract, which is its qualification record.
        qwen3-8b = ["nebius-b300-preemptible-1x"]
      }
    }
  }

  plan_options {
    target = [terraform_data.academic_lane_ownership]
  }

  expect_failures = [terraform_data.academic_lane_ownership]
}

run "a_routed_model_with_an_empty_eligible_list_is_rejected" {
  command = plan

  variables {
    scheduling = {
      cohort                             = { enabled = true, name = "inference-shared" }
      fair_share_precedence_acknowledged = true
      academic_raw_data_stages           = true
      core_pool_capacity = {
        nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
      }
      model_eligible_pool_ids = {
        alphafold3 = []
      }
    }
  }

  plan_options {
    target = [terraform_data.academic_lane_ownership]
  }

  expect_failures = [terraform_data.academic_lane_ownership]
}

run "warm_capacity_is_searched_before_burst_capacity" {
  command = plan

  plan_options {
    target = [module.kueue_scheduling.terraform_data.contract]
  }

  variables {
    # A second pool, identical except for identity, so the only thing under
    # test is the order. Pool IDs sort burst-first, which is what the shipped
    # raw AlphaFold 3 example must not accept.
    accelerator_pool_contract = merge(var.accelerator_pool_contract, {
      pools = merge(var.accelerator_pool_contract.pools, {
        nebius-b300-warm-1x = merge(
          var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"],
          {
            id = "nebius-b300-warm-1x"
            capacity = merge(
              var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].capacity,
              {
                type            = "regular"
                min_nodes       = 1
                max_nodes       = 1
                scale_from_zero = false
                profile_bounds  = { min_nodes = 1, max_nodes = 1 }
              },
            )
            region_availability = [{
              region         = "us-north1"
              state          = "live-preflight-required"
              capacity_modes = ["regular"]
            }]
            scheduling = merge(
              var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].scheduling,
              {
                resource_flavor_name = "fs2-b300-warm-1x"
                stable_node_labels = merge(
                  var.accelerator_pool_contract.pools["nebius-b300-preemptible-1x"].scheduling.stable_node_labels,
                  {
                    "accelerator.fs2.nebius/pool-id" = "nebius-b300-warm-1x"
                    "capacity.fs2.nebius/type"       = "regular"
                    "capacity.fs2.nebius/pool"       = "warm"
                  },
                )
              },
            )
          },
        )
      })
    })
    scheduling = merge(var.scheduling, {
      default_queue_pool_order = ["nebius-b300-warm-1x", "nebius-b300-preemptible-1x"]
      # Both pools state their measured capacity, because core admission is
      # coupled to the pool and a pool with no measurement has no budget.
      core_pool_capacity = {
        nebius-b300-warm-1x        = { cpu_millicores = 22000, memory_mib = 344064 }
        nebius-b300-preemptible-1x = { cpu_millicores = 22000, memory_mib = 344064 }
      }
      model_eligible_pool_ids = {
        alphafold3 = ["nebius-b300-warm-1x", "nebius-b300-preemptible-1x"]
      }
      # One setting. No service class repeats the list; each inherits the
      # order of the ClusterQueue it routes to.
    })
  }

  assert {
    condition = (
      join(",", module.kueue_scheduling.contract.cluster_queue_pool_order["inference-accelerators"]) ==
      "nebius-b300-warm-1x,nebius-b300-preemptible-1x" &&
      join(",", module.kueue_scheduling.contract.cluster_queue_pool_order["inference-accelerators"]) !=
      join(",", sort(module.kueue_scheduling.contract.cluster_queue_pool_order["inference-accelerators"]))
    )
    error_message = "The stable ClusterQueue must search warm capacity before burst capacity, and that order must be the operator's, not alphabetical."
  }

  assert {
    condition = alltrue([
      for service_class, policy in module.kueue_scheduling.contract.service_classes :
      join(",", policy.pool_preference) == "nebius-b300-warm-1x,nebius-b300-preemptible-1x"
    ])
    error_message = "Every service class must advertise the same warm-first pool order the ClusterQueue searches."
  }

  assert {
    condition = (
      module.kueue_scheduling.contract.cluster_queues["inference-accelerators"].metadata.annotations["fs2-serve.nebius.ai/accelerator-pool-ids"] ==
      "nebius-b300-warm-1x,nebius-b300-preemptible-1x" &&
      [
        for flavor in module.kueue_scheduling.contract.cluster_queues["inference-accelerators"].spec.resourceGroups[0].flavors :
        flavor.name
      ] == ["fs2-b300-warm-1x", "fs2-b300-1x"]
    )
    error_message = "The rendered ClusterQueue must list its ResourceFlavors warm-first, because Kueue tries them in that order."
  }

  assert {
    condition = (
      join(",", module.kueue_scheduling.contract.model_eligible_pool_ids["alphafold3"]) ==
      "nebius-b300-warm-1x,nebius-b300-preemptible-1x"
    )
    error_message = "AlphaFold 3's declared eligibility must keep the operator's warm-first order."
  }
}

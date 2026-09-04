# The reviewed cold-start mechanism document is the seam that scales to many
# models: onboarding another model is another entry in one file, not new
# Terraform. These runs prove the envelope publishes it and that a declaration
# whose configDigest does not match its own content is refused.

mock_provider "helm" {}
mock_provider "kubernetes" {}
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

  accelerator_node_schedulable_capacity = {
    nebius-b300-preemptible-1x = {
      cpu_millicores = 22000
      memory_mib     = 344064
      # The root handoff includes measurement provenance.  The workloads
      # variable deliberately narrows to the two schedulable quantities; this
      # production-shaped value proves Terraform object conversion accepts and
      # discards the additional evidence rather than dropping the whole map.
      evidence = {
        pool_id        = "nebius-b300-preemptible-1x"
        source         = "fixture:utf8:nebius-b300-preemptible-1x"
        captured_at    = "2026-09-03T06:00:00Z"
        payload_sha256 = "85cae37a96eff77ba331fdb643f4ba282e3f4f945ec19297ab22dadef7157663"
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
}

run "declared_mechanisms_reach_the_model_qualification" {
  command = plan

  variables {
    model_controller = merge(var.model_controller, {
      fast_start_mechanisms_file = abspath("tests/fixtures/fast-start-mechanisms.json")
    })
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  assert {
    condition     = local.model_controller_fast_start_mechanism_names_valid
    error_message = "Only regionalCache, hostMemoryResidency and gpuResident may be declared."
  }

  assert {
    condition     = local.model_controller_fast_start_mechanism_digests_valid
    error_message = "Each declaration must carry a configDigest matching its own canonical content."
  }

  assert {
    condition     = local.model_controller_fast_start_mechanism_pools_valid
    error_message = "Each declaration must reference only selected accelerator pools."
  }

  assert {
    condition = alltrue([
      for mechanism in ["regionalCache", "hostMemoryResidency", "gpuResident"] :
      contains(keys(local.model_controller_qualifications["qwen3-8b"]), mechanism)
    ])
    error_message = "Every declared mechanism must reach the published model qualification."
  }

  assert {
    condition = (
      local.model_controller_qualifications["qwen3-8b"].hostMemoryResidency.reservedBytes == 19327352832 &&
      local.model_controller_qualifications["qwen3-8b"].gpuResident.minimumHotReplicas == 1
    )
    error_message = "The envelope must carry each mechanism's declared price and hot-floor dependency."
  }

  assert {
    condition = (
      local.model_controller_envelope.residencyHolderImage ==
      "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
    )
    error_message = "The envelope must expose the digest-pinned control-plane image that packages the residency agent."
  }
}

run "declared_storage_dependencies_are_real_rwx_claims" {
  command = plan

  variables {
    model_controller = merge(var.model_controller, {
      fast_start_mechanisms_file = abspath("tests/fixtures/fast-start-mechanisms.json")
    })
  }

  plan_options {
    target = [
      kubernetes_persistent_volume_claim_v1.fast_start_compile_cache,
      kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt,
    ]
  }

  assert {
    condition = (
      length(kubernetes_persistent_volume_claim_v1.fast_start_compile_cache) == 1 &&
      length(kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt) == 1
    )
    error_message = "The declared compile cache and residency receipt must each resolve to one Terraform-managed claim."
  }

  assert {
    condition = (
      kubernetes_persistent_volume_claim_v1.fast_start_compile_cache["fs2-models/fs2-compile-cache-rwx"].metadata[0].namespace == "fs2-models" &&
      length(kubernetes_persistent_volume_claim_v1.fast_start_compile_cache["fs2-models/fs2-compile-cache-rwx"].spec[0].access_modes) == 1 &&
      contains(kubernetes_persistent_volume_claim_v1.fast_start_compile_cache["fs2-models/fs2-compile-cache-rwx"].spec[0].access_modes, "ReadWriteMany") &&
      kubernetes_persistent_volume_claim_v1.fast_start_compile_cache["fs2-models/fs2-compile-cache-rwx"].spec[0].storage_class_name == "csi-mounted-fs-path-sc" &&
      kubernetes_persistent_volume_claim_v1.fast_start_compile_cache["fs2-models/fs2-compile-cache-rwx"].spec[0].resources[0].requests.storage == "16Gi"
    )
    error_message = "The compile-cache claim must use the declared runtime namespace and the shared-filesystem RWX class with enough capacity for its byte limit."
  }

  assert {
    condition = (
      kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt["fs2-models/fs2-residency-receipt-rwx"].metadata[0].namespace == "fs2-models" &&
      length(kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt["fs2-models/fs2-residency-receipt-rwx"].spec[0].access_modes) == 1 &&
      contains(kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt["fs2-models/fs2-residency-receipt-rwx"].spec[0].access_modes, "ReadWriteMany") &&
      kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt["fs2-models/fs2-residency-receipt-rwx"].spec[0].storage_class_name == "csi-mounted-fs-path-sc" &&
      kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt["fs2-models/fs2-residency-receipt-rwx"].spec[0].resources[0].requests.storage == "1Gi"
    )
    error_message = "The residency holder and serving init container must share one real RWX receipt claim."
  }
}

run "existing_claims_can_remain_externally_managed" {
  command = plan

  variables {
    fast_start_claims = {
      manage                     = false
      storage_class              = "adopted-rwx.example"
      compile_cache_min_size_gib = 24
      residency_receipt_size_gib = 2
    }
    model_controller = merge(var.model_controller, {
      fast_start_mechanisms_file = abspath("tests/fixtures/fast-start-mechanisms.json")
    })
  }

  plan_options {
    target = [
      kubernetes_persistent_volume_claim_v1.fast_start_compile_cache,
      kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt,
    ]
  }

  assert {
    condition = (
      length(local.fast_start_compile_cache_claims) == 1 &&
      length(local.fast_start_residency_receipt_claims) == 1 &&
      local.fast_start_compile_cache_claims["fs2-models/fs2-compile-cache-rwx"].size_gib == 24 &&
      local.fast_start_residency_receipt_claims["fs2-models/fs2-residency-receipt-rwx"].size_gib == 2 &&
      var.fast_start_claims.storage_class == "adopted-rwx.example" &&
      length(local.fast_start_managed_compile_cache_claims) == 0 &&
      length(local.fast_start_managed_residency_receipt_claims) == 0
    )
    error_message = "manage=false must retain the declared external claim contract without attempting to create either claim."
  }
}

run "a_tampered_declaration_is_refused" {
  command = plan

  variables {
    model_controller = merge(var.model_controller, {
      fast_start_mechanisms_file = abspath("tests/fixtures/fast-start-mechanisms-tampered.json")
    })
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  # Raising the reserved host memory without recomputing the digest must not be
  # publishable: the declaration would otherwise inherit a cohort measured
  # against a different reservation. The contract fails closed, so the plan
  # itself is refused rather than producing an envelope.
  expect_failures = [terraform_data.model_controller_contract]
}

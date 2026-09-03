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

run "managed_default_cache_renders_without_storage_class" {
  command = plan

  plan_options {
    target = [terraform_data.modelexpress_contract]
  }

  assert {
    condition = (
      terraform_data.modelexpress_contract[0].input.helm_values.persistence.enabled &&
      terraform_data.modelexpress_contract[0].input.helm_values.persistence.storageClass == "" &&
      terraform_data.modelexpress_contract[0].input.helm_values.persistence.accessMode == "ReadWriteOnce" &&
      terraform_data.modelexpress_contract[0].input.helm_values.deploymentStrategy.type == "Recreate" &&
      length(terraform_data.modelexpress_contract[0].input.helm_values.extraVolumes) == 0 &&
      length(terraform_data.modelexpress_contract[0].input.helm_values.extraVolumeMounts) == 0
    )
    error_message = "Managed defaults must plan an RWO/Recreate cache while omitted storage_class encodes as the chart default."
  }

  assert {
    condition = (
      yamldecode(yamlencode(terraform_data.modelexpress_contract[0].input.helm_values)).persistence.storageClass == "" &&
      length(terraform_data.modelexpress_contract[0].input.model_ids) == 1 &&
      terraform_data.modelexpress_contract[0].input.model_ids[0] == "qwen3-8b"
    )
    error_message = "The actual workloads-stage Helm value must survive YAML encoding and retain the qualified model."
  }
}

run "disabled_pvc_renders_writable_ephemeral_cache" {
  command = plan

  variables {
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
      cache = { enabled = false, size_gib = 100 }
      models = {
        qwen3-8b = {
          runtime_adapter        = "vllm"
          client_package_version = "0.5.1"
        }
      }
    }
  }

  plan_options {
    target = [terraform_data.modelexpress_contract]
  }

  assert {
    condition = (
      !terraform_data.modelexpress_contract[0].input.helm_values.persistence.enabled &&
      length(terraform_data.modelexpress_contract[0].input.helm_values.extraVolumes) == 1 &&
      terraform_data.modelexpress_contract[0].input.helm_values.extraVolumes[0].name == "model-cache-ephemeral" &&
      length(keys(terraform_data.modelexpress_contract[0].input.helm_values.extraVolumes[0].emptyDir)) == 0 &&
      length(terraform_data.modelexpress_contract[0].input.helm_values.extraVolumeMounts) == 1 &&
      terraform_data.modelexpress_contract[0].input.helm_values.extraVolumeMounts[0].name == "model-cache-ephemeral" &&
      terraform_data.modelexpress_contract[0].input.helm_values.extraVolumeMounts[0].mountPath == "/var/cache/modelexpress"
    )
    error_message = "Disabling the PVC must retain a writable emptyDir at the server cache path."
  }
}

run "managed_server_digest_changes_client_binding" {
  command = plan

  variables {
    model_express = {
      enabled          = true
      deployment_mode  = "managed"
      endpoint         = "fs2-modelexpress.fs2-modelexpress.svc.cluster.local:8001"
      metadata_backend = "kubernetes"
      namespace        = "fs2-modelexpress"
      server_image = {
        repository = "nvcr.io/nvidia/ai-dynamo/modelexpress-server"
        digest     = "sha256:8888888888888888888888888888888888888888888888888888888888888888"
      }
      cache = { enabled = true, size_gib = 100 }
      models = {
        qwen3-8b = {
          runtime_adapter        = "vllm"
          client_package_version = "0.5.1"
        }
      }
    }
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  assert {
    condition = (
      local.model_controller_modelexpress_payloads["qwen3-8b"].coordinatorImage == "nvcr.io/nvidia/ai-dynamo/modelexpress-server@sha256:8888888888888888888888888888888888888888888888888888888888888888" &&
      local.model_controller_modelexpress_bindings["qwen3-8b"].configDigest == "sha256:${sha256(jsonencode(local.model_controller_modelexpress_payloads["qwen3-8b"]))}" &&
      sha256(jsonencode(local.model_controller_modelexpress_payloads["qwen3-8b"])) != sha256(jsonencode(merge(
        local.model_controller_modelexpress_payloads["qwen3-8b"],
        { coordinatorImage = "nvcr.io/nvidia/ai-dynamo/modelexpress-server@sha256:9999999999999999999999999999999999999999999999999999999999999999" },
      )))
    )
    error_message = "Changing the managed coordinator digest must change the exact ModelExpress client binding digest."
  }

  assert {
    condition = (
      length(local.model_controller_qualifications["qwen3-8b"].fastStartRuntimeContracts) == 0 &&
      local.model_controller_pool_envelope["nebius-b300-preemptible-1x"].startupScenario == "fresh-node-zero-pod" &&
      length(local.model_controller_pool_envelope["nebius-b300-preemptible-1x"].fastStartEnvironmentBindings) == 0
    )
    error_message = "Missing observed environment/measurement inputs must fail closed without inventing a fast-start runtime contract."
  }
}

run "dynamic_controller_envelope_includes_rendered_scheduling_choices" {
  command = plan

  variables {
    scheduling = {
      cluster_queues = {
        science-batch = {}
      }
      local_queues = {
        science-batch = {
          cluster_queue   = "science-batch"
          model_ids       = ["qwen3-8b"]
          service_classes = ["customer-batch"]
        }
      }
    }
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  assert {
    condition = toset(local.model_controller_envelope.localQueues) == toset([
      "inference-models",
      "science-batch",
    ])
    error_message = "The dynamic controller envelope must expose every LocalQueue rendered by the Kueue module."
  }

  assert {
    condition = (
      contains(local.model_controller_envelope.priorityClasses, "platform-critical") &&
      contains(local.model_controller_envelope.priorityClasses, "presentation") &&
      contains(local.model_controller_envelope.priorityClasses, "interactive") &&
      contains(local.model_controller_envelope.priorityClasses, "standard") &&
      contains(local.model_controller_envelope.priorityClasses, "batch")
    )
    error_message = "The dynamic controller envelope must expose both base and service-class WorkloadPriorityClasses rendered by the Kueue module."
  }
}

run "legacy_fast_start_projection_remains_historical" {
  command = plan

  variables {
    model_controller = {
      enabled                  = true
      writes_enabled           = true
      workload_owner           = "controller"
      bootstrap_model_ids      = ["qwen3-8b"]
      fresh_install            = true
      handoff_receipt          = null
      fast_start_evidence_file = abspath("tests/fixtures/legacy-fast-start-evidence.json")
      priority_classes         = { interactive = 100, standard = 0, batch = -100 }
    }
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  assert {
    condition = (
      local.model_controller_fast_start_evidence_valid &&
      try(local.model_controller_fast_start_evidence["qwen3-8b"][0].identityState, "LegacyUnbound") == "LegacyUnbound" &&
      length(local.model_controller_qualifications["qwen3-8b"].fastStartRuntimeContracts) == 0
    )
    error_message = "A pre-v2 projection must remain visible as LegacyUnbound while missing exact current contracts keep qualification Off."
  }
}

run "globally_disabled_creates_no_modelexpress_resources" {
  command = plan

  variables {
    model_express = {
      enabled          = false
      deployment_mode  = "managed"
      endpoint         = null
      metadata_backend = "kubernetes"
      namespace        = "fs2-modelexpress"
      server_image     = null
      cache            = { enabled = true, size_gib = 100 }
      models           = {}
    }
  }

  plan_options {
    target = [terraform_data.model_controller_contract]
  }

  assert {
    condition = (
      terraform_data.model_controller_contract.input.modelexpress_resources.contract == 0 &&
      terraform_data.model_controller_contract.input.modelexpress_resources.namespace == 0 &&
      terraform_data.model_controller_contract.input.modelexpress_resources.credential == 0 &&
      terraform_data.model_controller_contract.input.modelexpress_resources.helm == 0 &&
      !contains(keys(local.model_controller_qualifications["qwen3-8b"]), "modelExpress")
    )
    error_message = "The globally disabled integration must plan no ModelExpress contract, namespace, credential, or Helm release."
  }
}

# Provider-mocked plan fixture for the scientific artifact store projection.
#
# It proves three things the deployment depends on: the routes and the Secret
# appear only when the store is configured, the generated credential never
# reaches a Helm release value, and an unreachable store is refused rather than
# silently deployed.

mock_provider "helm" {}
mock_provider "random" {}

# The stage discovers the Kubernetes API endpoints to build its egress
# allowlist. Mocked lookups return null, so the collection is supplied
# explicitly; nothing below asserts on it.
mock_provider "kubernetes" {
  mock_data "kubernetes_resources" {
    defaults = {
      objects = []
    }
  }
}

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

run "disabled_by_default_projects_no_secret_and_no_chart_values" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  assert {
    condition     = length(terraform_data.scientific_artifacts_contract) == 0
    error_message = "An unconfigured store must plan no receipt, no Kubernetes Secret, and no chart values."
  }

}

run "an_enabled_store_projects_one_secret_and_the_chart_values" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled            = true
      bucket_name        = "fs2-scientific-artifacts-synthetic"
      region             = "us-north1"
      endpoint           = "https://storage.us-north1.nebius.cloud"
      retention_seconds  = 7776000
      handle_ttl_seconds = 600
      max_bytes          = 1099511627776
      media_types        = ["application/json", "chemical/x-pdb"]
      egress_cidrs       = ["203.0.113.0/24"]
      access_key_id      = "SYNTHETICACCESSKEYID"
    }
    scientific_artifact_store_credentials = {
      access_key_id     = "SYNTHETICACCESSKEYID"
      secret_access_key = "synthetic-object-storage-secret"
    }
  }

  assert {
    condition = (
      length(terraform_data.scientific_artifacts_contract) == 1 &&
      terraform_data.scientific_artifacts_contract[0].input.secret_name == "fs2-serve-artifact-store"
    )
    error_message = "An enabled store must project exactly one Secret at the name the chart mounts."
  }

  assert {
    condition     = terraform_data.scientific_artifacts_contract[0].input.credential_revision > 0
    error_message = "The Secret must carry a revision derived from the key identity so rotation rewrites it."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract[0].input.chart_values.scientificArtifacts.enabled == true &&
      terraform_data.scientific_artifacts_contract[0].input.chart_values.scientificArtifacts.bucket == "fs2-scientific-artifacts-synthetic" &&
      terraform_data.scientific_artifacts_contract[0].input.chart_values.scientificArtifacts.endpoint == "https://storage.us-north1.nebius.cloud" &&
      terraform_data.scientific_artifacts_contract[0].input.chart_values.scientificArtifacts.region == "us-north1" &&
      join(",", terraform_data.scientific_artifacts_contract[0].input.chart_values.scientificArtifacts.egressCidrs) == "203.0.113.0/24"
    )
    error_message = "The chart values must carry the exact bucket, endpoint, region and egress allowlist."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract[0].input.chart_values.secrets.artifactStore.name == "fs2-serve-artifact-store" &&
      terraform_data.scientific_artifacts_contract[0].input.chart_values.secrets.artifactStore.key == "credentials.json"
    )
    error_message = "The chart must be pointed at the Terraform-owned Secret and key."
  }

  assert {
    condition = !strcontains(
      jsonencode(terraform_data.scientific_artifacts_contract[0].input.chart_values),
      "synthetic-object-storage-secret",
    )
    error_message = "The object-storage secret must never appear in a Helm release value."
  }

  assert {
    condition = !strcontains(
      jsonencode(terraform_data.scientific_artifacts_contract[0].input),
      "synthetic-object-storage-secret",
    )
    error_message = "The object-storage secret must never be persisted in a Terraform receipt."
  }
}

run "an_enabled_store_without_a_credential_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled            = true
      bucket_name        = "fs2-scientific-artifacts-synthetic"
      region             = "us-north1"
      endpoint           = "https://storage.us-north1.nebius.cloud"
      retention_seconds  = 7776000
      handle_ttl_seconds = 600
      max_bytes          = 1099511627776
      media_types        = ["application/json"]
      egress_cidrs       = ["203.0.113.0/24"]
    }
    scientific_artifact_store_credentials = null
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "an_enabled_store_without_object_storage_egress_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled            = true
      bucket_name        = "fs2-scientific-artifacts-synthetic"
      region             = "us-north1"
      endpoint           = "https://storage.us-north1.nebius.cloud"
      retention_seconds  = 7776000
      handle_ttl_seconds = 600
      max_bytes          = 1099511627776
      media_types        = ["application/json"]
      egress_cidrs       = []
    }
    scientific_artifact_store_credentials = {
      access_key_id     = "SYNTHETICACCESSKEYID"
      secret_access_key = "synthetic-object-storage-secret"
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "a_cross_region_bucket_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled            = true
      bucket_name        = "fs2-scientific-artifacts-synthetic"
      region             = "eu-north1"
      endpoint           = "https://storage.eu-north1.nebius.cloud"
      retention_seconds  = 7776000
      handle_ttl_seconds = 600
      max_bytes          = 1099511627776
      media_types        = ["application/json"]
      egress_cidrs       = ["203.0.113.0/24"]
    }
    scientific_artifact_store_credentials = {
      access_key_id     = "SYNTHETICACCESSKEYID"
      secret_access_key = "synthetic-object-storage-secret"
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "a_store_with_an_empty_media_allowlist_is_refused_before_any_plan" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled     = true
      bucket_name = "fs2-scientific-artifacts-synthetic"
      region      = "us-north1"
      endpoint    = "https://storage.us-north1.nebius.cloud"
      media_types = []
    }
  }

  expect_failures = [var.scientific_artifacts]
}

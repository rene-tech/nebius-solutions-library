# The chart must actually receive the academic delivery contract.
#
# Projecting nothing left the chart's academicAssets block at its disabled default
# even when the root facade had the feature switched on, so a runtime had no way to
# learn which claim to mount.

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
}

run "disabled_academic_config_is_projected_as_disabled" {
  command = plan

  plan_options {
    target = [terraform_data.academic_assets_contract]
  }

  assert {
    condition     = terraform_data.academic_assets_contract.input.helm_values.enabled == false
    error_message = "The default opt-out must reach the chart as disabled."
  }
}

run "enabled_academic_config_reaches_the_chart" {
  command = plan

  variables {
    academic_assets = {
      enabled        = true
      project_id     = "project-test"
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
      assets                    = {}
      readiness_manifest_sha256 = "2b5a21f8eca6d8e465f29c508a6717915b84e73cb351d24811223a70228a3e36"
    }
  }

  plan_options {
    target = [terraform_data.academic_assets_contract]
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.helm_values.enabled == true &&
      terraform_data.academic_assets_contract.input.helm_values.namespace == "fs2-academic-poc" &&
      terraform_data.academic_assets_contract.input.helm_values.claim == "academic-assets-runtime-rwx" &&
      terraform_data.academic_assets_contract.input.helm_values.mountRoot == "/opt/fs2/academic" &&
      terraform_data.academic_assets_contract.input.helm_values.assetGid == 65532 &&
      terraform_data.academic_assets_contract.input.helm_values.readOnly == true
    )
    error_message = "The enabled academic delivery contract must reach the chart values verbatim."
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.delivery.embed_licensed_bytes == false &&
      terraform_data.academic_assets_contract.input.delivery.general_shared_cache == false &&
      terraform_data.academic_assets_contract.input.delivery.world_readable == false
    )
    error_message = "The projected delivery must keep the mount-not-bake invariants."
  }
}

run "localized_private_generation_reaches_the_chart" {
  command = plan

  variables {
    academic_assets = {
      enabled        = true
      project_id     = "project-test"
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
        pyrosetta-bindcraft = {
          model_id              = "bindcraft"
          relative_path         = "pyrosetta-bindcraft/pyrosetta.whl"
          install_relative_path = "pyrosetta-bindcraft/site-packages"
          runtime_binding = {
            artifact_id                = "bindcraft-pyrosetta-installed-tree"
            source_sub_path            = "pyrosetta-bindcraft/site-packages"
            consumer_path              = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
            mechanism                  = "subpath-directory-mount"
            content_identity_kind      = "tree-manifest"
            content_manifest_algorithm = "fs2-tree-manifest/v1"
            content_digest_sha256      = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
            size_bytes                 = 3287122494
            source_artifact = {
              filename   = "pyrosetta.whl"
              sha256     = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
              size_bytes = 1667097173
            }
          }
        }
      }
      readiness_manifest_sha256 = "2b5a21f8eca6d8e465f29c508a6717915b84e73cb351d24811223a70228a3e36"
    }

    scientific_batch = {
      execution_map = {
        schema = "fs2-serve.nebius.ai/scientific-execution-map/v3"
        models = [{
          model_id = "bindcraft"
          stages = [
            {
              stage_id = "design"
              mounts = [{
                kind       = "private"
                claim_name = "academic-assets-runtime-rwx"
                mount_path = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
                sub_path   = "scientific-localization/private/generations/bindcraft-pyrosetta-installed-tree/sha256/a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
              }]
            },
            {
              stage_id = "aggregate"
              mounts = [{
                kind       = "private"
                claim_name = "academic-assets-runtime-rwx"
                mount_path = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
                sub_path   = "scientific-localization/private/generations/bindcraft-pyrosetta-installed-tree/sha256/a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
              }]
            },
          ]
        }]
      }
    }
  }

  plan_options {
    target = [terraform_data.academic_assets_contract]
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.helm_values.runtimeBindings["pyrosetta-bindcraft"].sourceSubPath ==
      "scientific-localization/private/generations/bindcraft-pyrosetta-installed-tree/sha256/a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
    )
    error_message = "The chart must mount the immutable localized generation rather than the PyRosetta ingestion source tree."
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.helm_values.runtimeBindings["pyrosetta-bindcraft"].artifactId == "bindcraft-pyrosetta-installed-tree" &&
      terraform_data.academic_assets_contract.input.helm_values.runtimeBindings["pyrosetta-bindcraft"].consumerPath == "/opt/fs2/academic/pyrosetta-bindcraft/site-packages" &&
      terraform_data.academic_assets_contract.input.helm_values.runtimeBindings["pyrosetta-bindcraft"].contentDigestSha256 == "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
    )
    error_message = "Selecting the localized path must preserve the exact academic artifact, consumer, and content identities."
  }

  assert {
    condition = (
      terraform_data.academic_assets_contract.input.helm_values.tenantId == "tenant-academic" &&
      terraform_data.academic_assets_contract.input.helm_values.readinessManifestSha256 == "2b5a21f8eca6d8e465f29c508a6717915b84e73cb351d24811223a70228a3e36" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.localQueue == "academic-scientific" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.clusterQueue == "inference-accelerators" &&
      terraform_data.academic_assets_contract.input.helm_values.execution.serviceAccount == "fs2-academic-runner"
    )
    error_message = "The chart must receive the exact Terraform tenant, readiness receipt, GPU queues, and runner identity."
  }
}

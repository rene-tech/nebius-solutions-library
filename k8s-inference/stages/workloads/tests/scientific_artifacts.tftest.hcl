# The scientific result store must actually reach the control plane.
#
# Terraform can create a perfectly good bucket and key and still leave the
# control plane unconfigured, because the chart only learns about the store
# through these projected values. The projection and the feature gates are
# therefore asserted directly, without standing up the whole stage.

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
}

run "the_store_is_absent_from_the_chart_until_it_is_enabled" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.enabled == false &&
      !can(terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts) &&
      terraform_data.scientific_artifacts_contract.input.credential_revision == 0
    )
    error_message = "A disabled store must project no artifact values and no credential revision."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificBatch.enabled == false &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificBatch.writesEnabled == false
    )
    error_message = "Both batch gates must stay closed by default."
  }
}

run "storage_only_projects_the_canonical_chart_values" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled               = true
      handle_ttl_seconds    = 600
      max_artifact_bytes    = 1099511627776
      retention_days        = 90
      egress_cidrs          = ["195.242.0.14/32"]
      media_types           = ["application/json", "chemical/x-pdb"]
      credential_generation = 1
      storage_contract = {
        schema     = "fs2-serve.nebius.ai/scientific-artifact-storage/v1"
        project_id = "project-modelexpresstest"
        region     = "us-north1"
        object_storage = {
          id                = "storagebucket-scientifictest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 4096
          versioning_policy = "ENABLED"
          storage_class     = "STANDARD"
          addressing_style  = "path"
          verify_tls        = true
        }
        writer = {
          service_account_id = "serviceaccount-scientifictest"
          group_id           = "group-scientifictest"
          role               = "storage.object-editor"
          paths              = ["scientific/v1/*"]
          secret_delivery    = "MYSTERY_BOX"
        }
        layout = {
          root             = "scientific/v1"
          tenant_prefix    = "scientific/v1/tenants/<tenant>"
          operation_prefix = "scientific/v1/tenants/<tenant>/operations/<operation>"
          object_key       = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
          object_uri       = "s3://fs2-modelexpress-test-scientific-artifacts/scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
        }
        retention = {
          artifact_retention_days                = 90
          abort_incomplete_multipart_upload_days = 1
          noncurrent_version_expiration_days     = 1
          expired_object_delete_marker           = true
          current_object_expiration              = "application-owned"
          lifecycle_rule_ids = [
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
            "remove-expired-delete-markers",
          ]
        }
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        key_id              = "accesskey-scientifictest"
        access_key_id       = "AJE000SCIENTIFICTEST"
        secret_reference_id = "mysteryboxsecret-scientifictest"
        resource_version    = 0
      }
    }
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.enabled == true &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.bucket == "fs2-modelexpress-test-scientific-artifacts" &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.endpoint == "https://storage.us-north1.nebius.cloud" &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.region == "us-north1" &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.addressingStyle == "path" &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.verifyTls == true
    )
    error_message = "The chart must receive the exact bucket identity from the infrastructure contract."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.handleTtlSeconds == 600 &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.maxBytes == 1099511627776 &&
      terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.retentionSeconds == 90 * 86400
    )
    error_message = "Handle lifetime, maximum artifact size and the retention window must reach the chart exactly."
  }

  assert {
    condition = (
      join(",", terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.mediaTypes) == "application/json,chemical/x-pdb" &&
      join(",", terraform_data.scientific_artifacts_contract.input.chart_values.scientificArtifacts.egressCidrs) == "195.242.0.14/32" &&
      join(",", terraform_data.scientific_artifacts_contract.input.chart_values.networkPolicy.artifactStoreCidrs) == "195.242.0.14/32"
    )
    error_message = "The approved media types and the object-storage egress allowlist must be projected verbatim."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.chart_values.secrets.artifactStore.name == "fs2-serve-artifact-store" &&
      terraform_data.scientific_artifacts_contract.input.chart_values.secrets.artifactStore.key == "credentials.json" &&
      terraform_data.scientific_artifacts_contract.input.secret_name == "fs2-serve-artifact-store" &&
      terraform_data.scientific_artifacts_contract.input.namespace == "fs2-system"
    )
    error_message = "The control plane must be pointed at the stable fs2-system/fs2-serve-artifact-store Secret."
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.object_key ==
      "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
    )
    error_message = "The canonical object layout must survive the handoff unchanged."
  }

  # Batch and academic execution stay off while the store is fully wired.
  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.batch.enabled == false &&
      terraform_data.scientific_artifacts_contract.input.batch.writes_enabled == false
    )
    error_message = "Storage must be independently deployable with both batch gates closed."
  }
}

run "the_credential_revision_is_the_only_rotation_trigger" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled               = true
      handle_ttl_seconds    = 600
      max_artifact_bytes    = 1099511627776
      retention_days        = 90
      egress_cidrs          = ["195.242.0.14/32"]
      media_types           = ["application/json", "chemical/x-pdb"]
      credential_generation = 1
      storage_contract = {
        schema     = "fs2-serve.nebius.ai/scientific-artifact-storage/v1"
        project_id = "project-modelexpresstest"
        region     = "us-north1"
        object_storage = {
          id                = "storagebucket-scientifictest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 4096
          versioning_policy = "ENABLED"
          storage_class     = "STANDARD"
          addressing_style  = "path"
          verify_tls        = true
        }
        writer = {
          service_account_id = "serviceaccount-scientifictest"
          group_id           = "group-scientifictest"
          role               = "storage.object-editor"
          paths              = ["scientific/v1/*"]
          secret_delivery    = "MYSTERY_BOX"
        }
        layout = {
          root             = "scientific/v1"
          tenant_prefix    = "scientific/v1/tenants/<tenant>"
          operation_prefix = "scientific/v1/tenants/<tenant>/operations/<operation>"
          object_key       = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
          object_uri       = "s3://fs2-modelexpress-test-scientific-artifacts/scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
        }
        retention = {
          artifact_retention_days                = 90
          abort_incomplete_multipart_upload_days = 1
          noncurrent_version_expiration_days     = 1
          expired_object_delete_marker           = true
          current_object_expiration              = "application-owned"
          lifecycle_rule_ids = [
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
            "remove-expired-delete-markers",
          ]
        }
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        key_id              = "accesskey-scientifictest"
        access_key_id       = "AJE000SCIENTIFICTEST"
        secret_reference_id = "mysteryboxsecret-scientifictest"
        resource_version    = 0
      }
    }
  }

  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.credential_revision ==
      1 * 16777216 + parseint(substr(sha256(join("|", [
        "accesskey-scientifictest",
        "AJE000SCIENTIFICTEST",
        "mysteryboxsecret-scientifictest",
        "0",
      ])), 0, 6), 16) &&
      terraform_data.scientific_artifacts_contract.input.chart_values.podAnnotations["fs2.nebius.ai/artifact-store-credential-revision"] ==
      tostring(terraform_data.scientific_artifacts_contract.input.credential_revision)
    )
    error_message = "The rollout identity must cover the key's own identity and reach the non-secret pod annotation."
  }

  # A replaced cloud key restarts at resource_version 0, so the key's identity,
  # not the version counter, has to move the rollout.
  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.credential_revision !=
      1 * 16777216 + parseint(substr(sha256(join("|", [
        "accesskey-rotatedtest",
        "AJE000ROTATEDTEST",
        "mysteryboxsecret-rotatedtest",
        "0",
      ])), 0, 6), 16)
    )
    error_message = "Replacing the cloud key must change the rollout identity even though its resource version restarts at zero."
  }

  # A deliberate operator rotation must be an increase, not just a change.
  assert {
    condition = (
      terraform_data.scientific_artifacts_contract.input.credential_revision < 2 * 16777216 &&
      terraform_data.scientific_artifacts_contract.input.credential_revision >= 1 * 16777216 &&
      terraform_data.scientific_artifacts_contract.input.credential_generation == 1
    )
    error_message = "The generation must be the leading term of the rollout identity."
  }

  assert {
    condition = length([
      for value in values(terraform_data.scientific_artifacts_contract.input.chart_values.podAnnotations) :
      value if strcontains(lower(value), "secret") || strcontains(lower(value), "key")
    ]) == 0
    error_message = "The rollout annotation must carry a revision, never credential material."
  }
}

run "batch_execution_without_the_store_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_batch = {
      enabled        = true
      writes_enabled = true
      namespace      = "fs2-models"
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "kubernetes_writes_without_the_batch_gate_are_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_batch = {
      enabled        = false
      writes_enabled = true
      namespace      = "fs2-models"
    }
  }

  expect_failures = [var.scientific_batch]
}

run "a_store_that_reuses_the_reference_data_bucket_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled               = true
      handle_ttl_seconds    = 600
      max_artifact_bytes    = 1099511627776
      retention_days        = 90
      egress_cidrs          = ["195.242.0.14/32"]
      media_types           = ["application/json", "chemical/x-pdb"]
      credential_generation = 1
      storage_contract = {
        schema     = "fs2-serve.nebius.ai/scientific-artifact-storage/v1"
        project_id = "project-modelexpresstest"
        region     = "us-north1"
        object_storage = {
          id                = "storagebucket-scientifictest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 4096
          versioning_policy = "ENABLED"
          storage_class     = "STANDARD"
          addressing_style  = "path"
          verify_tls        = true
        }
        writer = {
          service_account_id = "serviceaccount-scientifictest"
          group_id           = "group-scientifictest"
          role               = "storage.object-editor"
          paths              = ["scientific/v1/*"]
          secret_delivery    = "MYSTERY_BOX"
        }
        layout = {
          root             = "scientific/v1"
          tenant_prefix    = "scientific/v1/tenants/<tenant>"
          operation_prefix = "scientific/v1/tenants/<tenant>/operations/<operation>"
          object_key       = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
          object_uri       = "s3://fs2-modelexpress-test-scientific-artifacts/scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
        }
        retention = {
          artifact_retention_days                = 90
          abort_incomplete_multipart_upload_days = 1
          noncurrent_version_expiration_days     = 1
          expired_object_delete_marker           = true
          current_object_expiration              = "application-owned"
          lifecycle_rule_ids = [
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
            "remove-expired-delete-markers",
          ]
        }
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        key_id              = "accesskey-scientifictest"
        access_key_id       = "AJE000SCIENTIFICTEST"
        secret_reference_id = "mysteryboxsecret-scientifictest"
        resource_version    = 0
      }
    }
    reference_data = {
      enabled   = true
      namespace = "fs2-reference-data"
      queue = {
        resource_flavor = "reference-data-cpu"
        cluster_queue   = "reference-data-cpu"
        local_queue     = "reference-data"
        nominal_cpu     = "6"
        nominal_memory  = "24Gi"
      }
      network = {
        allow_public_source_staging = false
        allow_public_msa_opt_in     = false
      }
      status = {
        enabled                 = false
        image                   = null
        replicas                = 1
        service_monitor_enabled = true
      }
      pipeline = {
        enabled                 = false
        bundle_id               = "alphafold3-public-databases-v3.0"
        image                   = null
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
          id         = "mk8snodegroup-referencetest"
          name       = "reference-data-cpu"
          platform   = "cpu-d3"
          preset     = "8vcpu-32gb"
          node_count = 1
          capacity   = "regular"
          schedulable_capacity = {
            cpu_millicores        = 7000
            memory_mib            = 28672
            ephemeral_storage_mib = 114688
          }
          node_labels = {
            "workload.fs2.nebius/reference-data" = "true"
            "capacity.fs2.nebius/type"           = "regular"
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
          id               = "computefilesystem-referencetest"
          size_gib         = 2048
          type             = "NETWORK_SSD"
          block_size_bytes = 4096
          forbid_deletion  = false
          node_mount_path  = "/mnt/fs2-reference-data"
          host_path        = "/mnt/fs2-reference-data/data"
          uri              = "file:///mnt/fs2-reference-data/data"
        }
        object_storage = {
          id                = "storagebucket-referencetest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 2048
          versioning_policy = "ENABLED"
          object_prefix     = "s3://fs2-modelexpress-test-scientific-artifacts/reference-data"
        }
        layout = {
          blobs                 = "s3://x/reference-data/blobs/sha256/<sha256>"
          manifests             = "s3://x/reference-data/manifests/sha256/<manifest-sha256>.json"
          filesystem_datasets   = "file:///mnt/fs2-reference-data/data/datasets"
          preprocessing_inputs  = "s3://x/inputs/sha256/<sha256>"
          preprocessing_outputs = "s3://x/preprocessing/<tenant>/<workload>"
        }
        sizing = {
          official_alphafold3_expanded_bytes = 630000000000
          required_headroom_bytes            = 1099511627776
          minimum_size_gib                   = 1611
        }
        public_msa_default = false
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        access_key_id       = "AJE000REFERENCETEST"
        secret_reference_id = "mysteryboxsecret-referencetest"
        revision            = 1
      }
    }
  }

  expect_failures = [terraform_data.scientific_artifacts_contract]
}

run "a_subnet_wide_egress_allowlist_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled               = true
      handle_ttl_seconds    = 600
      max_artifact_bytes    = 1099511627776
      retention_days        = 90
      egress_cidrs          = ["195.242.0.0/16"]
      media_types           = ["application/json", "chemical/x-pdb"]
      credential_generation = 1
      storage_contract = {
        schema     = "fs2-serve.nebius.ai/scientific-artifact-storage/v1"
        project_id = "project-modelexpresstest"
        region     = "us-north1"
        object_storage = {
          id                = "storagebucket-scientifictest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 4096
          versioning_policy = "ENABLED"
          storage_class     = "STANDARD"
          addressing_style  = "path"
          verify_tls        = true
        }
        writer = {
          service_account_id = "serviceaccount-scientifictest"
          group_id           = "group-scientifictest"
          role               = "storage.object-editor"
          paths              = ["scientific/v1/*"]
          secret_delivery    = "MYSTERY_BOX"
        }
        layout = {
          root             = "scientific/v1"
          tenant_prefix    = "scientific/v1/tenants/<tenant>"
          operation_prefix = "scientific/v1/tenants/<tenant>/operations/<operation>"
          object_key       = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
          object_uri       = "s3://fs2-modelexpress-test-scientific-artifacts/scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
        }
        retention = {
          artifact_retention_days                = 90
          abort_incomplete_multipart_upload_days = 1
          noncurrent_version_expiration_days     = 1
          expired_object_delete_marker           = true
          current_object_expiration              = "application-owned"
          lifecycle_rule_ids = [
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
            "remove-expired-delete-markers",
          ]
        }
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        key_id              = "accesskey-scientifictest"
        access_key_id       = "AJE000SCIENTIFICTEST"
        secret_reference_id = "mysteryboxsecret-scientifictest"
        resource_version    = 0
      }
    }
  }

  expect_failures = [var.scientific_artifacts]
}

run "an_out_of_region_bucket_is_refused" {
  command = plan

  plan_options {
    target = [terraform_data.scientific_artifacts_contract]
  }

  variables {
    scientific_artifacts = {
      enabled               = true
      handle_ttl_seconds    = 600
      max_artifact_bytes    = 1099511627776
      retention_days        = 90
      egress_cidrs          = ["195.242.0.14/32"]
      media_types           = ["application/json", "chemical/x-pdb"]
      credential_generation = 1
      storage_contract = {
        schema     = "fs2-serve.nebius.ai/scientific-artifact-storage/v1"
        project_id = "project-modelexpresstest"
        region     = "eu-north1"
        object_storage = {
          id                = "storagebucket-scientifictest"
          name              = "fs2-modelexpress-test-scientific-artifacts"
          endpoint          = "https://storage.us-north1.nebius.cloud"
          max_size_gib      = 4096
          versioning_policy = "ENABLED"
          storage_class     = "STANDARD"
          addressing_style  = "path"
          verify_tls        = true
        }
        writer = {
          service_account_id = "serviceaccount-scientifictest"
          group_id           = "group-scientifictest"
          role               = "storage.object-editor"
          paths              = ["scientific/v1/*"]
          secret_delivery    = "MYSTERY_BOX"
        }
        layout = {
          root             = "scientific/v1"
          tenant_prefix    = "scientific/v1/tenants/<tenant>"
          operation_prefix = "scientific/v1/tenants/<tenant>/operations/<operation>"
          object_key       = "scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
          object_uri       = "s3://fs2-modelexpress-test-scientific-artifacts/scientific/v1/tenants/<tenant>/operations/<operation>/stages/<stage>/shards/<shard>/attempts/<attempt>/<input|output>/sha256/<digest>"
        }
        retention = {
          artifact_retention_days                = 90
          abort_incomplete_multipart_upload_days = 1
          noncurrent_version_expiration_days     = 1
          expired_object_delete_marker           = true
          current_object_expiration              = "application-owned"
          lifecycle_rule_ids = [
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
            "remove-expired-delete-markers",
          ]
        }
        lifecycle = {
          retention_mode     = "disposable"
          destroy_status     = "eligible-only-while-bucket-empty"
          destroy_completion = "full-only-when-versioned-bucket-empty"
          adoption_status    = "not-applicable"
          retained_ids       = null
        }
      }
      object_storage_access = {
        key_id              = "accesskey-scientifictest"
        access_key_id       = "AJE000SCIENTIFICTEST"
        secret_reference_id = "mysteryboxsecret-scientifictest"
        resource_version    = 0
      }
    }
  }

  expect_failures = [var.scientific_artifacts]
}

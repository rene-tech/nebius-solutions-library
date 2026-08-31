data "kubernetes_namespace_v1" "kube_system" {
  metadata {
    name = "kube-system"
  }
}

resource "terraform_data" "cluster_contract" {
  input = {
    cluster_id                       = var.cluster_id
    cluster_name                     = var.cluster_name
    kube_context                     = var.kube_context
    kube_system_uid                  = var.kube_system_uid
    project_sha256                   = nonsensitive(sha256(var.project_id))
    target_contract                  = var.target_contract
    target_sha256                    = local.target_contract_sha256
    target_region                    = local.selected_target.region
    run_id                           = var.run_id
    accelerator_pool_contract        = var.accelerator_pool_contract
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    infrastructure_contract          = var.infrastructure_contract
    infrastructure_contract_sha256   = local.infrastructure_contract_sha256
    kueue_teardown_cleanup = {
      cluster_id      = var.cluster_id
      cluster_name    = var.cluster_name
      kube_context    = var.kube_context
      kube_system_uid = var.kube_system_uid
      kubeconfig_path = abspath(var.kubeconfig_path)
      release_name    = "fs2-${var.run_id}-kueue"
      run_id          = var.run_id
      run_root        = abspath(var.run_root)
      script_path     = abspath("${path.module}/scripts/cleanup-kueue-aggregate-roles.sh")
      script_sha256   = filesha256("${path.module}/scripts/cleanup-kueue-aggregate-roles.sh")
      timeout_seconds = "180"
      retry_seconds   = "2"
    }
  }

  # Every foundation object depends directly or transitively on this receipt,
  # so its destroy provisioner runs after Helm releases, CRDs, and namespaces.
  # Kueue's aggregate-role controller can race Helm uninstall and recreate two
  # ownerless target roles. The script deletes only that exact observed orphan
  # signature; any live Kueue component or differently owned object blocks
  # teardown and leaves this receipt in state for a reviewed retry.
  provisioner "local-exec" {
    when    = destroy
    command = "\"${self.input.kueue_teardown_cleanup.script_path}\""
    quiet   = true

    environment = {
      FS2_CLEANUP_CLUSTER_ID      = self.input.kueue_teardown_cleanup.cluster_id
      FS2_CLEANUP_CLUSTER_NAME    = self.input.kueue_teardown_cleanup.cluster_name
      FS2_CLEANUP_KUBECONFIG      = self.input.kueue_teardown_cleanup.kubeconfig_path
      FS2_CLEANUP_KUBE_CONTEXT    = self.input.kueue_teardown_cleanup.kube_context
      FS2_CLEANUP_KUBE_SYSTEM_UID = self.input.kueue_teardown_cleanup.kube_system_uid
      FS2_CLEANUP_KUEUE_RELEASE   = self.input.kueue_teardown_cleanup.release_name
      FS2_CLEANUP_RETRY_SECONDS   = self.input.kueue_teardown_cleanup.retry_seconds
      FS2_CLEANUP_RUN_ID          = self.input.kueue_teardown_cleanup.run_id
      FS2_CLEANUP_RUN_ROOT        = self.input.kueue_teardown_cleanup.run_root
      FS2_CLEANUP_TIMEOUT_SECONDS = self.input.kueue_teardown_cleanup.timeout_seconds
    }
  }

  lifecycle {
    precondition {
      condition     = abspath(var.kubeconfig_path) == local.expected_kubeconfig_path
      error_message = "kubeconfig_path must be the exact run-owned <run_root>/kubeconfig file."
    }
    precondition {
      condition     = var.kube_context == var.cluster_name
      error_message = "kube_context must equal the exact bounded cluster_name emitted by infrastructure."
    }
    precondition {
      condition = (
        local.selected_context != null &&
        local.selected_cluster != null &&
        local.selected_api_server != null &&
        strcontains(local.selected_api_server, var.cluster_id)
      )
      error_message = "The exact named kubeconfig context must select an API server authority containing cluster_id."
    }
    precondition {
      condition     = data.kubernetes_namespace_v1.kube_system.metadata[0].uid == var.kube_system_uid
      error_message = "The selected Kubernetes API does not have the reviewed kube-system UID."
    }
    precondition {
      condition = (
        var.target_contract.project_id == nonsensitive(var.project_id) &&
        var.target_contract.source_registry_project_id == var.target_contract.source_registry.project_id
      )
      error_message = "The infrastructure target contract must bind the exact project and one source-registry project identity."
    }
    precondition {
      condition = (
        var.accelerator_pool_contract.target_region == local.selected_target.region &&
        var.accelerator_pool_contract.artifact_source.registry.id == local.selected_target.source_registry.id &&
        var.accelerator_pool_contract.artifact_source.registry.project_id == local.selected_target.source_registry.project_id &&
        var.accelerator_pool_contract.artifact_source.registry.fqdn == local.selected_target.source_registry.fqdn &&
        var.accelerator_pool_contract.artifact_source.closure_schema == jsondecode(file("${path.module}/../../catalog/profiles/source-registry-closure.json")).schema &&
        var.accelerator_pool_contract.artifact_source.closure_sha256 == filesha256("${path.module}/../../catalog/profiles/source-registry-closure.json")
      )
      error_message = "The v2 accelerator contract differs from the selected target region or reviewed artifact source identity."
    }
    precondition {
      condition = (
        var.infrastructure_contract == local.expected_infrastructure_contract &&
        local.legacy_infrastructure_contract_matches_v2
      )
      error_message = "The optional legacy infrastructure contract differs from the reviewed v1 fixture or authoritative v2 accelerator contract."
    }
  }
}

resource "kubernetes_secret_v1" "grafana_admin" {
  count = var.bootstrap_grafana_credentials == null ? 0 : 1

  metadata {
    name      = var.grafana_admin_secret_ref.name
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels    = local.common_labels
  }

  data = {
    (var.grafana_admin_secret_ref.user_key)     = var.bootstrap_grafana_credentials.username
    (var.grafana_admin_secret_ref.password_key) = var.bootstrap_grafana_credentials.password
  }

  type = "Opaque"

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "cluster_contract" {
  metadata {
    name      = "fs2-terraform-cluster-contract"
    namespace = kubernetes_namespace_v1.platform["fs2-system"].metadata[0].name
    labels    = local.common_labels
  }

  immutable = true
  data = merge({
    schema                           = "fs2-serve.nebius.ai/terraform-cluster-contract/v2"
    cluster_id                       = var.cluster_id
    cluster_name                     = var.cluster_name
    kube_context                     = var.kube_context
    kube_system_uid                  = var.kube_system_uid
    project_sha256                   = nonsensitive(sha256(var.project_id))
    target_sha256                    = local.target_contract_sha256
    target_region                    = local.selected_target.region
    run_id                           = var.run_id
    stage                            = "foundation"
    accelerator_pool_contract_schema = var.accelerator_pool_contract.schema
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    accelerator_pool_ids_json        = jsonencode(local.accelerator_pool_ids)
    accelerator_pool_capacity_json   = jsonencode(local.accelerator_pool_capacity_view)
    source_commit                    = var.accelerator_pool_contract.source_commit
    infrastructure_project_id        = nonsensitive(var.project_id)
    source_registry_id               = var.accelerator_pool_contract.artifact_source.registry.id
    source_registry_project_id       = var.accelerator_pool_contract.artifact_source.registry.project_id
    source_registry_fqdn             = var.accelerator_pool_contract.artifact_source.registry.fqdn
    source_registry_region           = var.accelerator_pool_contract.artifact_source.registry.region
    artifact_closure_schema          = var.accelerator_pool_contract.artifact_source.closure_schema
    artifact_closure_sha256          = var.accelerator_pool_contract.artifact_source.closure_sha256
    capacity_profile                 = var.accelerator_pool_contract.profile
    gpu_floor_profile                = var.accelerator_pool_contract.floor_profile
    maximum_gpus                     = tostring(local.accelerator_pool_maximum_gpus)
    minimum_gpus                     = tostring(local.accelerator_pool_minimum_gpus)
    }, local.legacy_infrastructure_contract_enabled ? {
    infrastructure_contract_sha256 = local.infrastructure_contract_sha256
    shared_cache_size_gib          = tostring(var.infrastructure_contract.capacity.shared_cache_size_gib)
    system_nodes                   = tostring(var.infrastructure_contract.capacity.system.nodes)
    system_max_surge               = tostring(var.infrastructure_contract.capacity.system.max_surge)
    system_max_unavailable         = tostring(var.infrastructure_contract.capacity.system.max_unavailable)
    gpu_b300_1x_gpus_per_node      = tostring(var.infrastructure_contract.capacity.gpu_b300_1x.gpus_per_node)
    gpu_b300_1x_min_nodes          = tostring(var.infrastructure_contract.capacity.gpu_b300_1x.min_nodes)
    gpu_b300_1x_max_nodes          = tostring(var.infrastructure_contract.capacity.gpu_b300_1x.max_nodes)
    gpu_b300_8x_gpus_per_node      = tostring(var.infrastructure_contract.capacity.gpu_b300_8x.gpus_per_node)
    gpu_b300_8x_min_nodes          = tostring(var.infrastructure_contract.capacity.gpu_b300_8x.min_nodes)
    gpu_b300_8x_max_nodes          = tostring(var.infrastructure_contract.capacity.gpu_b300_8x.max_nodes)
  } : {})

  depends_on = [terraform_data.cluster_contract]
}

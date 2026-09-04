output "public_endpoint" {
  description = "Terraform-owned HTTPS inference and MCP origin."
  value       = local.public_edge_enabled ? local.public_base_url : null
}

output "port_forward_contract" {
  description = "Run-scoped loopback acceptance endpoint, or null when a public edge is enabled."
  value       = local.public_edge_enabled ? null : var.public_edge_contract.port_forward
}

output "mcp_endpoint_url" {
  description = "Resolved Streamable HTTP MCP endpoint. Internal-only deployments require the run-scoped operator proxy described by port_forward_contract."
  value       = "${trimsuffix(local.public_base_url, "/")}/mcp"
}

output "inference_base_url" {
  description = "OpenAI-compatible inference base URL."
  value       = "${trimsuffix(local.public_base_url, "/")}/v1"
}

output "admin_web_interface_url" {
  description = "Resolved admin web-interface URL. Internal-only deployments require the run-scoped operator proxy described by port_forward_contract."
  value       = var.admin_console == null ? null : "${trimsuffix(local.public_base_url, "/")}/admin/"
}

output "admin_token" {
  description = "Private disposable admin credential used only to mint/revoke scoped PATs through the cluster-internal admin API; it is not valid public /v1 or /mcp authorization."
  value       = random_password.admin_token.result
  sensitive   = true
}

output "admin_bootstrap_token" {
  description = "Bootstrap credential for creating the initial admin browser session; it is not a public inference credential."
  value       = random_password.admin_token.result
  sensitive   = true
}

output "mcp_access_token" {
  description = "Terraform-owned scoped PAT provisioned in the durable control-plane token store for MCP and inference bootstrap access."
  value       = local.bootstrap_access_token
  sensitive   = true
}

output "inference_access_token" {
  description = "Alias of mcp_access_token for OpenAI-compatible inference clients; the bootstrap PAT intentionally grants both MCP and inference scopes."
  value       = local.bootstrap_access_token
  sensitive   = true
}

output "grafana_url" {
  description = "Published native-login Grafana URL, or null when publication is disabled."
  value       = local.grafana_publication.enabled ? local.grafana_publication.external_url : null
}

output "grafana_admin_username" {
  description = "Grafana bootstrap username read from the foundation-owned existing Secret."
  value       = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.user_key]
  sensitive   = true
}

output "grafana_admin_password" {
  description = "Grafana bootstrap password read from the foundation-owned existing Secret."
  value       = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.password_key]
  sensitive   = true
}

output "cluster_id" {
  description = "Nebius Managed Kubernetes cluster identifier."
  value       = var.cluster_id
}

output "cluster_name" {
  description = "Nebius Managed Kubernetes cluster name and kubeconfig context."
  value       = var.cluster_name
}

output "project_id" {
  description = "Nebius project containing this deployment."
  value       = nonsensitive(var.project_id)
}

output "region" {
  description = "Nebius region selected by the target contract."
  value       = local.selected_target.region
}

output "kubeconfig_command" {
  description = "Recreate the run-scoped kubeconfig with its exact cluster ID and context."
  value = format(
    "KUBECONFIG=%q nebius mk8s cluster get-credentials --id %q --external --force --context-name %q",
    var.kubeconfig_path,
    var.cluster_id,
    var.kube_context,
  )
}

output "access_bundle" {
  description = "Sensitive post-apply connection bundle. Request it explicitly and never place it in logs or tickets."
  sensitive   = true
  value = {
    schema = "fs2-serve.nebius.ai/access-bundle/v1"
    cluster = {
      project_id   = nonsensitive(var.project_id)
      region       = local.selected_target.region
      cluster_id   = var.cluster_id
      cluster_name = var.cluster_name
      kube_context = var.kube_context
      kubeconfig_command = format(
        "KUBECONFIG=%q nebius mk8s cluster get-credentials --id %q --external --force --context-name %q",
        var.kubeconfig_path,
        var.cluster_id,
        var.kube_context,
      )
    }
    endpoints = {
      admin_portal_url   = var.admin_console == null ? null : "${trimsuffix(local.public_base_url, "/")}/admin/"
      mcp_url            = "${trimsuffix(local.public_base_url, "/")}/mcp"
      inference_base_url = "${trimsuffix(local.public_base_url, "/")}/v1"
      grafana_url        = local.grafana_publication.enabled ? local.grafana_publication.external_url : null
    }
    credentials = {
      admin_bootstrap_token  = random_password.admin_token.result
      mcp_inference_token    = local.bootstrap_access_token
      inference_access_token = local.bootstrap_access_token
      grafana = {
        username = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.user_key]
        password = data.kubernetes_secret_v1.grafana_admin.data[data.terraform_remote_state.foundation.outputs.grafana_admin_secret_ref.password_key]
      }
    }
    mcp_access = {
      principal_id    = local.bootstrap_access_principal
      tenant_id       = local.selected_target.tenant_id
      scopes          = local.bootstrap_access_scopes
      models          = local.bootstrap_access_models
      max_concurrency = 32
    }
    reference_data = var.reference_data.enabled ? {
      lifecycle      = var.reference_data.storage_contract.lifecycle
      filesystem_id  = var.reference_data.storage_contract.filesystem.id
      bucket_id      = var.reference_data.storage_contract.object_storage.id
      bucket_name    = var.reference_data.storage_contract.object_storage.name
      cpu_pool_id    = var.reference_data.storage_contract.cpu_pool.id
      capacity_fit   = module.reference_data[0].dynamic_configuration.capacity_fit
      status_service = module.reference_data[0].dynamic_configuration.status_service
      pipeline       = module.reference_data[0].dynamic_configuration.pipeline
    } : null
    reference_data_contract = var.reference_data.enabled ? terraform_data.reference_data_contract[0].output : null
    # Identity and scope only. The bundle deliberately never carries the S3
    # secret: the control plane reads it from its own mounted Secret and hands
    # workers short-lived signed handles instead.
    scientific_artifacts = var.scientific_artifacts.enabled ? {
      lifecycle           = var.scientific_artifacts.storage_contract.lifecycle
      bucket_id           = var.scientific_artifacts.storage_contract.object_storage.id
      bucket_name         = var.scientific_artifacts.storage_contract.object_storage.name
      endpoint            = var.scientific_artifacts.storage_contract.object_storage.endpoint
      object_key          = var.scientific_artifacts.storage_contract.layout.object_key
      writer_role         = var.scientific_artifacts.storage_contract.writer.role
      credential_secret   = "fs2-system/${local.scientific_artifacts_secret_name}"
      credential_revision = local.scientific_artifacts_revision
      batch_enabled       = var.scientific_batch.enabled
    } : null
  }
}

output "deployment_contract" {
  value = {
    profile                          = var.deployment_profile
    canonical_routes                 = local.selected_model_ids
    canonical_route_count            = length(local.selected_model_ids)
    model_manifest_count             = length(local.model_manifests)
    keeper_manifest_count            = length(local.keeper_manifests)
    keda_scaledobject_count          = length(local.model_scalers)
    public_edge_mode                 = var.public_edge_contract.mode
    public_allocation_id             = var.public_edge_contract.allocation_id
    target_contract                  = var.target_contract
    target_region                    = local.selected_target.region
    target_sha256                    = local.target_contract_sha256
    accelerator_pool_contract        = var.accelerator_pool_contract
    accelerator_pool_contract_sha256 = local.accelerator_pool_contract_sha256
    infrastructure_contract          = var.infrastructure_contract
    infrastructure_contract_sha256   = local.infrastructure_contract_sha256
  }
}

output "dynamic_model_handoff_receipt" {
  description = "Copy this non-secret receipt into deployment.dynamic_models.handoff_receipt only after the explicit workload_owner=released apply has completed."
  value = (
    var.model_controller.workload_owner == "released" ?
    local.model_controller_expected_handoff_receipt :
    null
  )
}

output "dynamic_model_contract" {
  description = "Non-secret derived controller ownership, catalog, bootstrap, and immutable contract identities."
  value = {
    enabled                     = var.model_controller.enabled
    writes_enabled              = var.model_controller.writes_enabled
    workload_owner              = var.model_controller.workload_owner
    catalog_model_ids           = local.selected_model_ids
    controller_model_ids        = local.model_controller_dynamic_model_ids
    bootstrap_model_ids         = sort(tolist(var.model_controller.bootstrap_model_ids))
    ineligible_models           = local.model_controller_ineligible_reasons
    static_ineligible_model_ids = sort(keys(local.model_controller_ineligible_reasons))
    envelope_revision           = var.model_controller.enabled ? local.model_controller_envelope.revision : null
    renderer_bundles_sha256     = var.model_controller.enabled ? sha256(local.model_controller_bundles_json) : null
    terraform_manifest_count    = length(local.terraform_owned_model_manifests)
    terraform_scaler_count      = length(local.terraform_owned_model_scalers)
    bootstrap_job_enabled       = local.model_controller_bootstrap_enabled
    unsupported_gvks_retained = sort(distinct([
      for document in values(local.terraform_owned_model_manifests) : "${document.manifest.apiVersion}/${document.manifest.kind}"
      if(
        var.model_controller.workload_owner != "terraform" &&
        !contains(
          local.model_controller_supported_template_gvks,
          "${document.manifest.apiVersion}/${document.manifest.kind}",
        )
      )
    ]))
  }
}

output "fast_start_claims_contract" {
  description = "Non-secret RWX claim realization derived from the reviewed fast-start mechanism set. manage=false means the named claims are externally owned and must already exist."
  value = {
    manage             = var.fast_start_claims.manage
    storage_class      = var.fast_start_claims.storage_class
    access_mode        = "ReadWriteMany"
    compile_cache      = local.fast_start_compile_cache_claims
    residency_receipts = local.fast_start_residency_receipt_claims
  }
}

output "model_autoscaling_contract" {
  description = "Non-secret replica-owner, hot-floor, timing, and exact route-to-Deployment contract."
  value = {
    mode                      = var.model_scaling_mode
    replica_owner             = var.model_scaling_mode == "keda" ? "keda" : "terraform"
    activation_handshake      = "disabled-lean-route"
    hot_model_ids             = sort(tolist(var.hot_model_ids))
    polling_interval_seconds  = var.keda_polling_interval_seconds
    cooldown_period_seconds   = var.keda_cooldown_period_seconds
    prometheus_server_address = var.model_scaling_mode == "keda" ? local.prometheus_server_address : null
    targets = {
      for model_id, target in local.model_scalers : model_id => {
        deployment   = target.deployment
        service      = target.service
        gpu_count    = target.gpu_count
        min_replicas = target.min_replicas
        max_replicas = target.max_replicas
      }
    }
  }
}

output "model_express_contract" {
  description = "Non-secret ModelExpress deployment and exact client-binding summary. Configuration never implies a qualified fast-start level."
  value = {
    enabled          = var.model_express.enabled
    deployment_mode  = var.model_express.deployment_mode
    endpoint         = var.model_express.enabled ? var.model_express.endpoint : null
    metadata_backend = var.model_express.enabled ? var.model_express.metadata_backend : null
    namespace        = var.model_express.enabled ? var.model_express.namespace : null
    upstream_version = var.model_express.enabled ? "0.5.1" : null
    model_ids        = local.modelexpress_model_ids
    config_digests = {
      for model_id, binding in local.model_controller_modelexpress_bindings : model_id => binding.configDigest
    }
    pool_transports = {
      for model_id, binding in local.model_controller_modelexpress_bindings : model_id => binding.poolTransports
    }
  }
}

output "dcgm_attribution_contract" {
  description = "Non-secret Terraform-owned DCGM collection/scrape provenance for nominal attempt-bound Prometheus proxy evidence."
  value = {
    schema                                 = "fs2-serve.nebius.ai/dcgm-attribution-terraform/v1"
    campaign_enabled                       = var.enable_dcgm_cold_start_campaign
    attribution_metric_collection_interval = local.dcgm_collection_interval
    scrape_interval                        = local.dcgm_scrape_interval
    scrape_timeout                         = local.dcgm_scrape_timeout
    campaign_metrics                       = local.dcgm_campaign_metrics
    minimum_nominal_window_seconds         = local.dcgm_minimum_nominal_window
    missing_sample_policy                  = "FAIL_CLOSED_NO_ESTIMATE"
  }
}

output "scheduling_contract" {
  description = "Validated non-secret Kueue cohort, queue, fair-sharing, quota, and service-class contract."
  value       = module.kueue_scheduling.contract
}

output "general_cpu_class_contribution" {
  description = "The canonical general-cpu CPU stage class this deployment contributes to the scheduling contract, its digest, and the ownership facts for the queues the general lane created. The scheduling workstream assembles and publishes; this is the exact entry it merges."
  value = {
    cpu_classes_schema  = module.general_cpu_scheduling.contract.cpu_classes_schema
    cpu_classes         = module.general_cpu_scheduling.contract.cpu_classes
    cpu_class_digests   = module.general_cpu_scheduling.contract.cpu_class_digests
    external_lane_facts = module.general_cpu_scheduling.contract.external_lane_facts
  }
}

output "general_cpu_contract" {
  description = "General CPU lane: rendered classes, capacity, elasticity and the consumed scheduling ConfigMap handoff with the exact raw-byte digest the controller must verify."
  value       = terraform_data.general_cpu_contract.output
}

output "reference_data_contract" {
  description = "Same-region storage, private preprocessing and optional official staging-pipeline contract."
  value       = try(terraform_data.reference_data_contract[0].output, null)
}

output "scientific_artifacts_contract" {
  description = "Non-secret projection of the scientific result store: bucket identity, canonical object key, credential Secret reference, rotation revision, the exact control-plane chart values and the batch gates."
  value       = terraform_data.scientific_artifacts_contract.output
}

output "scientific_artifacts_status" {
  description = "Non-secret result-store state for inference-stack status: bucket identity, retention, writer scope and credential revision."
  value = var.scientific_artifacts.enabled ? {
    bucket_id           = var.scientific_artifacts.storage_contract.object_storage.id
    bucket_name         = var.scientific_artifacts.storage_contract.object_storage.name
    endpoint            = var.scientific_artifacts.storage_contract.object_storage.endpoint
    region              = var.scientific_artifacts.storage_contract.region
    object_key          = var.scientific_artifacts.storage_contract.layout.object_key
    writer_role         = var.scientific_artifacts.storage_contract.writer.role
    writer_paths        = var.scientific_artifacts.storage_contract.writer.paths
    secret_delivery     = var.scientific_artifacts.storage_contract.writer.secret_delivery
    lifecycle           = var.scientific_artifacts.storage_contract.lifecycle
    retention           = var.scientific_artifacts.storage_contract.retention
    credential_secret   = "fs2-system/${local.scientific_artifacts_secret_name}"
    credential_key      = local.scientific_artifacts_secret_key
    credential_revision = local.scientific_artifacts_revision
    handle_ttl_seconds  = var.scientific_artifacts.handle_ttl_seconds
    max_artifact_bytes  = var.scientific_artifacts.max_artifact_bytes
    media_types         = sort(var.scientific_artifacts.media_types)
    batch = {
      enabled        = var.scientific_batch.enabled
      writes_enabled = var.scientific_batch.writes_enabled
      namespace      = var.scientific_batch.namespace
    }
  } : null
}

output "reference_data_status" {
  description = "Non-secret reference storage IDs, retention state, CPU placement, status service and immutable pipeline state for inference-stack status."
  value = var.reference_data.enabled ? {
    lifecycle      = var.reference_data.storage_contract.lifecycle
    filesystem_id  = var.reference_data.storage_contract.filesystem.id
    bucket_id      = var.reference_data.storage_contract.object_storage.id
    bucket_name    = var.reference_data.storage_contract.object_storage.name
    cpu_pool_id    = var.reference_data.storage_contract.cpu_pool.id
    capacity_fit   = module.reference_data[0].dynamic_configuration.capacity_fit
    status_service = module.reference_data[0].dynamic_configuration.status_service
    pipeline       = module.reference_data[0].dynamic_configuration.pipeline
  } : null
}

output "scheduling_contract_ref" {
  description = "Immutable ConfigMap handoff and revision for the controller/admin owners; this scheduling slice does not mount or consume it."
  value = {
    schema          = module.kueue_scheduling.contract.schema
    config_map_name = kubernetes_config_map_v1.scientific_scheduling_contract.metadata[0].name
    namespace       = kubernetes_config_map_v1.scientific_scheduling_contract.metadata[0].namespace
    key             = local.scheduling_contract_key
    sha256          = local.scheduling_contract_sha256
  }
}

output "managed_resource_count" {
  description = "Expected concrete managed-address count for exact plan review."
  value = (
    # Profile-independent identity, credential, database, queue, control-plane,
    # and Grafana egress addresses. Profile-shaped collections stay explicit.
    47 +
    (local.ngc_api_key_required ? 1 : 0) +
    (local.model_nvcr_credentials_required ? 1 : 0) +
    (local.dcgm_nvcr_credentials_required ? 1 : 0) +
    (var.deployment_profile == "full_catalog" ? 1 : 0) +
    length(local.terraform_owned_model_manifests) +
    length(local.keeper_manifests) +
    length(local.terraform_owned_model_scalers) +
    length(local.fast_start_managed_compile_cache_claims) +
    length(local.fast_start_managed_residency_receipt_claims) +
    (var.model_controller.enabled ? 2 : 0) +
    (local.model_controller_bootstrap_enabled ? 3 : 0) +
    (local.admin_configuration_enabled ? 1 : 0) +
    (data.terraform_remote_state.foundation.outputs.grafana_publication_contract.enabled ? 2 : 0) +
    (var.run_acceptance_job ? 4 : 0) +
    (var.run_acceptance_job && var.deployment_profile == "full_catalog" ? 1 : 0)
    + (var.model_express.enabled ? 1 : 0)
    + (local.modelexpress_managed ? 2 : 0)
    + (local.modelexpress_nvcr_required ? 1 : 0)
    # Scheduling owns three validation resources (pool units, academic lane
    # ownership, policy contract) plus the immutable policy ConfigMap. Each
    # additive LocalQueue also has a replacement trigger for its immutable
    # namespace/ClusterQueue binding. Stable queue/WPC addresses remain in the
    # base count; only the cohort and additive policy objects increase the
    # concrete address total.
    + 4
    + (module.kueue_scheduling.contract.core_resource_flavor == null ? 0 : 1)
    + (module.kueue_scheduling.contract.cohort == null ? 0 : 1)
    + (length(module.kueue_scheduling.contract.cluster_queues) - 1)
    + (
      length(module.kueue_scheduling.contract.local_queues)
      -length(module.kueue_scheduling.contract.external_local_queue_names)
      -1
    ) * 2
    + length(setsubtract(
      toset(keys(module.kueue_scheduling.contract.workload_priority_classes)),
      toset(keys(var.model_controller.priority_classes)),
    ))
    # The general CPU lane: its own validation resource plus, when enabled, one
    # ResourceFlavor, one ClusterQueue and one LocalQueue per tenant namespace.
    + 2
    + (module.general_cpu_scheduling.contract.enabled ? (
      2 + length(module.general_cpu_scheduling.contract.manifests.local_queues)
    ) : 0)
    + (var.reference_data.enabled ? (
      12
      + (var.reference_data.network.allow_public_source_staging ? 1 : 0)
      + (var.reference_data.network.allow_public_msa_opt_in ? 1 : 0)
      + (var.reference_data.pipeline.enabled ? 2 : 0)
      + (var.reference_data.status.enabled ? 3 : 0)
      + (var.reference_data.status.enabled && var.reference_data.status.service_monitor_enabled ? 1 : 0)
    ) : 0)
  )
}

output "sensitive_state_notice" {
  value = "Generated admin, MCP/inference, Grafana, database, and cryptographic bootstrap material is stored in the run-owned local workloads state; keep the run root mode 0700/state files mode 0600 and destroy it after acceptance."
}

output "academic_assets" {
  description = "Tenant-private academic asset delivery: identities a runtime needs to mount the exact licensed assets."
  value       = module.academic_assets.academic_assets
}

output "academic_assets_managed_addresses" {
  description = "Terraform addresses of the selected academic claims, for adopting already-populated storage."
  value       = module.academic_assets.managed_addresses
}

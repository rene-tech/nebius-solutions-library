module "reference_data" {
  count  = var.reference_data.enabled ? 1 : 0
  source = "../../reference-data/terraform"

  cluster_region        = try(var.reference_data.storage_contract.region, local.selected_target.region)
  object_storage_region = try(var.reference_data.storage_contract.region, local.selected_target.region)
  object_bucket_name    = try(var.reference_data.storage_contract.object_storage.name, "disabled-reference-data.invalid")
  object_storage_access = coalesce(var.reference_data.object_storage_access, {
    access_key_id       = "DISABLED0"
    secret_reference_id = "mysteryboxsecret-disabled"
    revision            = 1
  })

  namespace                   = var.reference_data.namespace
  shared_filesystem_host_path = try(var.reference_data.storage_contract.filesystem.host_path, "/mnt/fs2-reference-data/data")
  queue                       = var.reference_data.queue

  object_storage_egress_fqdns = [
    trimsuffix(trimprefix(try(var.reference_data.storage_contract.object_storage.endpoint, "https://storage.${local.selected_target.region}.nebius.cloud"), "https://"), "/"),
  ]
  allow_public_source_staging = var.reference_data.network.allow_public_source_staging
  allow_public_msa_opt_in     = var.reference_data.network.allow_public_msa_opt_in

  status = {
    enabled  = var.reference_data.status.enabled
    image    = var.reference_data.status.image
    replicas = var.reference_data.status.replicas
  }
  service_monitor_enabled = var.reference_data.status.service_monitor_enabled
  pipeline                = var.reference_data.pipeline
}

resource "terraform_data" "reference_data_contract" {
  count = var.reference_data.enabled ? 1 : 0
  input = {
    storage = var.reference_data.storage_contract
    plane   = module.reference_data[0].dynamic_configuration
  }

  lifecycle {
    precondition {
      condition = !var.reference_data.enabled || (
        var.reference_data.storage_contract.project_id == nonsensitive(var.project_id) &&
        var.reference_data.storage_contract.region == local.selected_target.region &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611 &&
        var.reference_data.storage_contract.object_storage.max_size_gib >= 1611 &&
        var.reference_data.storage_contract.object_storage.versioning_policy == "ENABLED" &&
        !var.reference_data.storage_contract.public_msa_default
      )
      error_message = "reference data must use the infrastructure-owned same-project/same-region storage contract with at least 1 TiB headroom and private MSA defaults."
    }
  }
}

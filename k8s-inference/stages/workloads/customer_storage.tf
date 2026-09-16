variable "customer_storage" {
  description = "Automatic customer buckets. Tenant mode shares a bucket; user mode gives each owner a bucket."
  type = object({
    enabled          = optional(bool, false)
    default_mode     = optional(string, "tenant")
    quota_bytes      = optional(number, 5000000000)
    excluded_tenants = optional(set(string), [])
  })
  default = {}

  validation {
    condition = (
      contains(["tenant", "user"], var.customer_storage.default_mode) &&
      var.customer_storage.quota_bytes > 0 &&
      floor(var.customer_storage.quota_bytes) == var.customer_storage.quota_bytes
    )
    error_message = "Customer storage requires tenant or user mode and a positive whole-byte quota."
  }
}

module "customer_storage_provisioner" {
  count      = var.customer_storage.enabled ? 1 : 0
  source     = "../../modules/customer-storage-provisioner"
  project_id = var.project_id
  name       = "fs2-${var.run_id}-customer-storage-provisioner"
}

resource "kubernetes_secret_v1" "customer_storage_provisioner" {
  count = var.customer_storage.enabled ? 1 : 0
  metadata {
    name      = "fs2-customer-storage-provisioner"
    namespace = "fs2-system"
  }
  data_wo = {
    "credentials.json" = module.customer_storage_provisioner[0].credentials_json
  }
  data_wo_revision = 1
}

locals {
  customer_storage_chart_values = {
    customerStorage = {
      enabled         = var.customer_storage.enabled
      projectId       = var.project_id
      region          = var.target_contract.region
      defaultMode     = var.customer_storage.default_mode
      quotaBytes      = var.customer_storage.quota_bytes
      excludedTenants = sort(tolist(var.customer_storage.excluded_tenants))
      secretName      = var.customer_storage.enabled ? kubernetes_secret_v1.customer_storage_provisioner[0].metadata[0].name : ""
    }
  }
}

variable "customer_storage" {
  description = "Customer buckets in an isolated project. Credential Secrets are injected by an external rotator."
  type = object({
    enabled                          = optional(bool, false)
    project_id                       = optional(string, "")
    default_mode                     = optional(string, "user")
    quota_bytes                      = optional(number, 5000000000)
    excluded_tenants                 = optional(set(string), [])
    resource_credentials_secret_name = optional(string, "")
    iam_credentials_secret_name      = optional(string, "")
    resource_public_key_pem          = optional(string, "")
    iam_public_key_pem               = optional(string, "")
    auth_key_expires_at              = optional(string, "")
    egress_cidrs                     = optional(set(string), [])
    key_ttl_days                     = optional(number, 90)
  })
  default   = {}
  sensitive = true

  validation {
    condition = (
      contains(["tenant", "user"], var.customer_storage.default_mode) &&
      var.customer_storage.quota_bytes > 0 &&
      floor(var.customer_storage.quota_bytes) == var.customer_storage.quota_bytes &&
      (!var.customer_storage.enabled || (
        var.customer_storage.project_id != "" &&
        var.customer_storage.project_id != var.project_id &&
        var.customer_storage.resource_credentials_secret_name != "" &&
        var.customer_storage.iam_credentials_secret_name != "" &&
        var.customer_storage.resource_credentials_secret_name != var.customer_storage.iam_credentials_secret_name &&
        var.customer_storage.resource_public_key_pem != "" &&
        var.customer_storage.iam_public_key_pem != "" &&
        var.customer_storage.auth_key_expires_at != "" &&
        length(var.customer_storage.egress_cidrs) > 0 &&
        var.customer_storage.key_ttl_days >= 1 && var.customer_storage.key_ttl_days <= 365
      ))
    )
    error_message = "Enabled customer storage requires a distinct project, split external credentials, expiry, bounded egress, and a 1-365 day key TTL."
  }
}

module "customer_storage_provisioner" {
  count                   = var.customer_storage.enabled ? 1 : 0
  source                  = "../../modules/customer-storage-provisioner"
  project_id              = var.customer_storage.project_id
  name                    = "fs2-${var.run_id}-customer-storage-provisioner"
  resource_public_key_pem = var.customer_storage.resource_public_key_pem
  iam_public_key_pem      = var.customer_storage.iam_public_key_pem
  auth_key_expires_at     = var.customer_storage.auth_key_expires_at
}

locals {
  customer_storage_chart_values = {
    customerStorage = {
      enabled                       = var.customer_storage.enabled
      projectId                     = var.customer_storage.project_id
      region                        = var.target_contract.region
      defaultMode                   = var.customer_storage.default_mode
      quotaBytes                    = var.customer_storage.quota_bytes
      excludedTenants               = sort(tolist(var.customer_storage.excluded_tenants))
      resourceCredentialsSecretName = var.customer_storage.resource_credentials_secret_name
      iamCredentialsSecretName      = var.customer_storage.iam_credentials_secret_name
      egressCidrs                   = sort(tolist(var.customer_storage.egress_cidrs))
      keyTtlDays                    = var.customer_storage.key_ttl_days
    }
  }
}

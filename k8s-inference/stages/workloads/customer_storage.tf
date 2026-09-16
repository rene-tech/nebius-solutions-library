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
    egress_contract_json             = optional(string, "")
    key_ttl_days                     = optional(number, 90)
    rotation_window_days             = optional(number, 14)
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
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.customer_storage.auth_key_expires_at)) &&
        can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$", var.customer_storage.auth_key_expires_at)) &&
        timecmp(var.customer_storage.auth_key_expires_at, plantimestamp()) > 0 &&
        timecmp(var.customer_storage.auth_key_expires_at, timeadd(plantimestamp(), "2160h")) <= 0 &&
        var.customer_storage.egress_contract_json != "" &&
        var.customer_storage.key_ttl_days >= 1 && var.customer_storage.key_ttl_days <= 365 &&
        var.customer_storage.rotation_window_days >= 1 &&
        var.customer_storage.rotation_window_days < var.customer_storage.key_ttl_days
      ))
    )
    error_message = "Enabled customer storage requires a distinct project, split external credentials, an RFC3339 auth expiry no more than 90 days ahead, a signed egress contract, and a rotation window below the key TTL."
  }
}

data "external" "customer_storage_egress" {
  count   = var.customer_storage.enabled ? 1 : 0
  program = ["python3", "${path.module}/scripts/customer_storage_egress_contract.py", "--terraform-external"]
  query = {
    contract_json  = var.customer_storage.egress_contract_json
    public_key_pem = data.kubernetes_secret_v1.customer_storage_egress_trust[0].data["public-key.pem"]
  }
}

# This trust root is intentionally outside the Helm release and workloads
# state. A security-owned bootstrap installs the immutable public key before a
# plan; a chart caller cannot self-assert a replacement key beside its policy.
data "kubernetes_secret_v1" "customer_storage_egress_trust" {
  count = var.customer_storage.enabled ? 1 : 0

  metadata {
    name      = "fs2-customer-storage-egress-trust"
    namespace = "fs2-system"
  }
  lifecycle {
    postcondition {
      condition     = self.immutable == true && try(self.data["public-key.pem"], "") != ""
      error_message = "Customer-storage egress trust must be a pre-existing immutable Secret with public-key.pem."
    }
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "customer_storage_egress_contract" {
  count = var.customer_storage.enabled ? 1 : 0

  metadata {
    name      = "fs2-customer-storage-egress-contract"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  immutable = true
  data = {
    "contract.json" = var.customer_storage.egress_contract_json
  }
  depends_on = [terraform_data.cluster_contract]
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
      databaseSecretName            = "fs2-serve-database-storage"
      disclosureDatabaseSecretName  = "fs2-serve-database-storage-disclosure"
      cryptoSecretName              = "fs2-serve-storage-keyring"
      egressCidrs                   = var.customer_storage.enabled ? jsondecode(data.external.customer_storage_egress[0].result.cidrs_json) : []
      keyTtlDays                    = var.customer_storage.key_ttl_days
      rotationWindowDays            = var.customer_storage.rotation_window_days
    }
  }
}

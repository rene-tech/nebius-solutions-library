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
    egress_boundary = optional(object({
      schema                        = optional(string, "")
      generation                    = optional(string, "")
      contract_sha256               = optional(string, "")
      contract_config_map_name      = optional(string, "")
      trust_config_map_name         = optional(string, "")
      network_policy_name           = optional(string, "")
      boundary_policy_name          = optional(string, "")
      security_owner_group          = optional(string, "")
      security_owner_subject_sha256 = optional(string, "")
      workloads_subject_sha256      = optional(string, "")
    }), {})
    key_ttl_days         = optional(number, 90)
    rotation_window_days = optional(number, 14)
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
        var.customer_storage.egress_boundary.schema == "fs2-serve.nebius.ai/customer-storage-egress-security-handoff/v1" &&
        can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.generation)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.contract_sha256)) &&
        endswith(var.customer_storage.egress_boundary.generation, substr(var.customer_storage.egress_boundary.contract_sha256, 0, 12)) &&
        var.customer_storage.egress_boundary.contract_config_map_name == "fs2-customer-storage-egress-contract-${var.customer_storage.egress_boundary.generation}" &&
        can(regex("^fs2-customer-storage-egress-trust-g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.trust_config_map_name)) &&
        var.customer_storage.egress_boundary.network_policy_name == "fs2-customer-storage-egress-${var.customer_storage.egress_boundary.generation}" &&
        can(regex("^fs2-customer-storage-egress-boundary-g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.boundary_policy_name)) &&
        var.customer_storage.egress_boundary.security_owner_group == "fs2:customer-storage-egress-security-owner" &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.security_owner_subject_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.workloads_subject_sha256)) &&
        var.customer_storage.egress_boundary.security_owner_subject_sha256 != var.customer_storage.egress_boundary.workloads_subject_sha256 &&
        var.customer_storage.key_ttl_days >= 1 && var.customer_storage.key_ttl_days <= 365 &&
        var.customer_storage.rotation_window_days >= 1 &&
        var.customer_storage.rotation_window_days < var.customer_storage.key_ttl_days
      ))
    )
    error_message = "Enabled customer storage requires a distinct project, split external credentials, an RFC3339 auth expiry no more than 90 days ahead, a signed egress contract with the exact external security-owner handoff, and a rotation window below the key TTL."
  }
}

data "external" "customer_storage_egress" {
  count = var.customer_storage.enabled ? 1 : 0
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/scripts/customer_storage_egress_contract.py",
    "--terraform-external",
  ]
  query = {
    contract_json  = var.customer_storage.egress_contract_json
    public_key_pem = data.kubernetes_config_map_v1.customer_storage_egress_trust[0].data["public-key.pem"]
  }
}

# Every object below is read-only in this root. The distinct security-owner
# root creates append-only generations and the admission policy forbids this
# workloads identity from changing or deleting them.
data "kubernetes_config_map_v1" "customer_storage_egress_trust" {
  count = var.customer_storage.enabled ? 1 : 0

  metadata {
    name      = var.customer_storage.egress_boundary.trust_config_map_name
    namespace = "fs2-system"
  }
  lifecycle {
    postcondition {
      condition     = self.immutable == true && try(self.data["public-key.pem"], "") != ""
      error_message = "Customer-storage egress trust must be a pre-existing immutable security-owner ConfigMap."
    }
  }
  depends_on = [terraform_data.cluster_contract]
}

data "kubernetes_config_map_v1" "customer_storage_egress_contract" {
  count = var.customer_storage.enabled ? 1 : 0

  metadata {
    name      = var.customer_storage.egress_boundary.contract_config_map_name
    namespace = "fs2-system"
  }
  lifecycle {
    postcondition {
      condition = (
        self.immutable == true &&
        try(self.data["contract.json"], "") == var.customer_storage.egress_contract_json &&
        try(self.data["kubernetes-api-cidrs.json"], "") != ""
      )
      error_message = "Customer-storage egress contract must equal the immutable security-owner handoff."
    }
  }
  depends_on = [terraform_data.cluster_contract]
}

data "kubernetes_resource" "customer_storage_egress_network_policy" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  metadata {
    name      = var.customer_storage.egress_boundary.network_policy_name
    namespace = "fs2-system"
  }
}

data "kubernetes_resource" "customer_storage_egress_boundary_policy" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata {
    name = var.customer_storage.egress_boundary.boundary_policy_name
  }
}

data "kubernetes_resource" "customer_storage_egress_boundary_binding" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata {
    name = var.customer_storage.egress_boundary.boundary_policy_name
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
  customer_storage_kubernetes_api_cidrs = sort(tolist(setunion(
    local.kubernetes_api_service_cidrs,
    local.kubernetes_api_endpoint_cidrs,
  )))
  customer_storage_network_policy_name = var.customer_storage.egress_boundary.network_policy_name
  customer_storage_network_policy_spec = {
    podSelector = {
      matchLabels = {
        "app.kubernetes.io/name"                  = "fs2-serve-control-plane"
        "app.kubernetes.io/instance"              = "fs2-serve-control-plane"
        "app.kubernetes.io/component"             = "storage-reconciler"
        "fs2.nebius.ai/storage-egress-generation" = var.customer_storage.egress_boundary.generation
      }
    }
    policyTypes = ["Ingress", "Egress"]
    ingress     = []
    egress = var.customer_storage.enabled ? [
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
          podSelector = {
            matchLabels = {
              "app.kubernetes.io/instance" = "coredns"
              "app.kubernetes.io/name"     = "coredns"
              "k8s-app"                    = "coredns"
            }
          }
        }]
        ports = [
          { port = 53, protocol = "UDP" },
          { port = 53, protocol = "TCP" },
        ]
      },
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-data" } }
          podSelector       = { matchLabels = { "cnpg.io/cluster" = "fs2-control-db" } }
        }]
        ports = [{ port = 5432, protocol = "TCP" }]
      },
      {
        to = [
          for cidr in jsondecode(data.external.customer_storage_egress[0].result.cidrs_json) :
          { ipBlock = { cidr = cidr } }
        ]
        ports = [{ port = 443, protocol = "TCP" }]
      },
      {
        to = [
          for cidr in local.customer_storage_kubernetes_api_cidrs :
          { ipBlock = { cidr = cidr } }
        ]
        ports = [{ port = 443, protocol = "TCP" }]
      },
    ] : []
  }
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
      kubernetesApiCidrs            = var.customer_storage.enabled ? local.customer_storage_kubernetes_api_cidrs : []
      egressContractSha256          = var.customer_storage.enabled ? data.external.customer_storage_egress[0].result.contract_sha256 : ""
      egressGeneration              = var.customer_storage.egress_boundary.generation
      egressContractConfigMapName   = var.customer_storage.egress_boundary.contract_config_map_name
      egressTrustConfigMapName      = var.customer_storage.egress_boundary.trust_config_map_name
      egressNetworkPolicyName       = var.customer_storage.egress_boundary.network_policy_name
      egressBoundaryPolicyName      = var.customer_storage.egress_boundary.boundary_policy_name
      keyTtlDays                    = var.customer_storage.key_ttl_days
      rotationWindowDays            = var.customer_storage.rotation_window_days
    }
  }
}

# This root has read-only dependency edges to the separately credentialed
# security owner. It refuses a stale, mismatched, mutable or self-asserted
# handoff but owns none of the protected objects.
resource "terraform_data" "customer_storage_external_egress_boundary" {
  count = var.customer_storage.enabled ? 1 : 0

  input = {
    generation           = var.customer_storage.egress_boundary.generation
    contract_sha256      = data.external.customer_storage_egress[0].result.contract_sha256
    network_policy_name  = var.customer_storage.egress_boundary.network_policy_name
    boundary_policy_name = var.customer_storage.egress_boundary.boundary_policy_name
  }

  lifecycle {
    precondition {
      condition     = data.external.customer_storage_egress[0].result.contract_sha256 == var.customer_storage.egress_boundary.contract_sha256
      error_message = "The live/fresh signed contract digest differs from the external security-owner handoff."
    }
    precondition {
      condition = (
        data.kubernetes_config_map_v1.customer_storage_egress_contract[0].metadata[0].annotations["fs2.nebius.ai/storage-egress-contract-sha256"] == var.customer_storage.egress_boundary.contract_sha256 &&
        data.kubernetes_config_map_v1.customer_storage_egress_contract[0].metadata[0].labels["app.kubernetes.io/managed-by"] == "fs2-security-owner" &&
        data.kubernetes_config_map_v1.customer_storage_egress_contract[0].metadata[0].labels["fs2.nebius.ai/security-generation"] == var.customer_storage.egress_boundary.generation &&
        jsondecode(data.kubernetes_config_map_v1.customer_storage_egress_contract[0].data["kubernetes-api-cidrs.json"]) == local.customer_storage_kubernetes_api_cidrs
      )
      error_message = "The immutable contract object is not the exact externally owned generation."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.customer_storage_egress_network_policy[0].object.metadata.labels["app.kubernetes.io/managed-by"], "") == "fs2-security-owner" &&
        try(data.kubernetes_resource.customer_storage_egress_network_policy[0].object.metadata.labels["fs2.nebius.ai/security-generation"], "") == var.customer_storage.egress_boundary.generation &&
        try(data.kubernetes_resource.customer_storage_egress_network_policy[0].object.metadata.annotations["fs2.nebius.ai/storage-egress-contract-sha256"], "") == var.customer_storage.egress_boundary.contract_sha256 &&
        try(data.kubernetes_resource.customer_storage_egress_network_policy[0].object.spec, null) == local.customer_storage_network_policy_spec
      )
      error_message = "The live security-owned NetworkPolicy does not equal the signed contract and exact workload selectors."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.labels["app.kubernetes.io/managed-by"], "") == "fs2-security-owner" &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.annotations["fs2.nebius.ai/security-owner-subject-sha256"], "") == var.customer_storage.egress_boundary.security_owner_subject_sha256 &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.annotations["fs2.nebius.ai/workloads-subject-sha256"], "") == var.customer_storage.egress_boundary.workloads_subject_sha256 &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.spec.failurePolicy, "") == "Fail" &&
        strcontains(jsonencode(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.spec.validations), "request.operation != 'DELETE'") &&
        strcontains(jsonencode(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.spec.validations), var.customer_storage.egress_boundary.security_owner_group)
      )
      error_message = "The external append-only admission policy or security-owner identity is absent."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.customer_storage_egress_boundary_binding[0].object.spec.policyName, "") == var.customer_storage.egress_boundary.boundary_policy_name &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_binding[0].object.spec.validationActions, []) == ["Deny"]
      )
      error_message = "The external fail-closed admission binding is absent or not enforcing Deny."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

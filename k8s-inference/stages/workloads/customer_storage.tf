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
      boundary_policy_sha256        = optional(string, "")
      workload_policy_name          = optional(string, "")
      workload_policy_sha256        = optional(string, "")
      security_owner_group          = optional(string, "")
      security_owner_subject_sha256 = optional(string, "")
      workloads_subject_sha256      = optional(string, "")
      identity_inventory_sha256     = optional(string, "")
      provider_authority = optional(object({
        schema                                            = optional(string, "")
        generation                                        = optional(string, "")
        authority_manifest_sha256                         = optional(string, "")
        prior_head_receipt_sha256                         = optional(string, "")
        predecessor_state_custody_sha256                  = optional(string, "")
        predecessor_state_compatibility_sha256            = optional(string, "")
        contract_sha256                                   = optional(string, "")
        predecessor_compatibility_sha256                  = optional(string, "")
        boundary_policy_sha256                            = optional(string, "")
        workload_policy_sha256                            = optional(string, "")
        release_values_sha256                             = optional(string, "")
        security_group_id                                 = optional(string, "")
        node_group_id                                     = optional(string, "")
        node_selector_key                                 = optional(string, "")
        node_selector_value                               = optional(string, "")
        taint_key                                         = optional(string, "")
        taint_value                                       = optional(string, "")
        taint_effect                                      = optional(string, "")
        provider_api_cidrs                                = optional(list(string), [])
        kubernetes_api_cidrs                              = optional(list(string), [])
        authority_service_account_sha256                  = optional(string, "")
        provider_identity_sha256                          = optional(string, "")
        kubernetes_identity_inventory_sha256              = optional(string, "")
        kubernetes_service_account_inventory_sha256       = optional(string, "")
        kubernetes_system_subject_inventory_sha256        = optional(string, "")
        kubernetes_rbac_inventory_sha256                  = optional(string, "")
        kubernetes_rbac_effective_authority_sha256        = optional(string, "")
        kubernetes_rbac_inventory_receipt_sha256          = optional(string, "")
        provider_project_iam_inventory_receipt_sha256     = optional(string, "")
        provider_effective_authority_graph_receipt_sha256 = optional(string, "")
        provider_authority_adapter_sha256                 = optional(string, "")
        provider_state_custody_sha256                     = optional(string, "")
        boundary_state_custody_sha256                     = optional(string, "")
        retained_v3_boundary_policies = optional(map(object({
          name          = string
          policy_sha256 = string
          policy_spec   = any
          binding_spec = object({
            policyName        = string
            validationActions = list(string)
          })
        })), {})
        retained_v3_workload_policies = optional(map(object({
          name          = string
          policy_sha256 = string
          policy_spec   = any
          binding_spec = object({
            policyName        = string
            validationActions = list(string)
          })
        })), {})
        retained_v3_admission_custody_sha256    = optional(string, "")
        workloads_service_account_sha256        = optional(string, "")
        accepted_sai10_commit                   = optional(string, "")
        accepted_sai10_tree                     = optional(string, "")
        sai10_independent_review_receipt_sha256 = optional(string, "")
      }), {})
      predecessor_compatibility = optional(object({
        schema                 = optional(string, "")
        receipt_sha256         = optional(string, "")
        deployment_uid         = optional(string, "")
        deployment_spec_sha256 = optional(string, "")
        network_policy_uid     = optional(string, "")
        network_policy_sha256  = optional(string, "")
        contract_sha256        = optional(string, "")
        egress_cidrs           = optional(list(string), [])
        kubernetes_api_cidrs   = optional(list(string), [])
        contract_uid           = optional(string, "")
        contract_data_sha256   = optional(string, "")
        policy_uid             = optional(string, "")
        policy_spec_sha256     = optional(string, "")
        binding_uid            = optional(string, "")
        binding_spec_sha256    = optional(string, "")
      }), {})
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
        var.customer_storage.egress_boundary.schema == "fs2-serve.nebius.ai/customer-storage-egress-security-handoff/v5" &&
        can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.generation)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.contract_sha256)) &&
        endswith(var.customer_storage.egress_boundary.generation, substr(var.customer_storage.egress_boundary.contract_sha256, 0, 12)) &&
        var.customer_storage.egress_boundary.contract_config_map_name == "fs2-storage-v3-contract-${var.customer_storage.egress_boundary.generation}" &&
        can(regex("^fs2-storage-v3-trust-g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.trust_config_map_name)) &&
        var.customer_storage.egress_boundary.network_policy_name == "fs2-storage-v3-network-policy-${var.customer_storage.egress_boundary.generation}" &&
        can(regex("^fs2-storage-v3-boundary-g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.boundary_policy_name)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.boundary_policy_sha256)) &&
        can(regex("^fs2-storage-v3-workload-g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.workload_policy_name)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.workload_policy_sha256)) &&
        endswith(var.customer_storage.egress_boundary.workload_policy_name, substr(var.customer_storage.egress_boundary.workload_policy_sha256, 0, 12)) &&
        var.customer_storage.egress_boundary.security_owner_group == "fs2:customer-storage-egress-security-owner" &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.security_owner_subject_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.workloads_subject_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.identity_inventory_sha256)) &&
        var.customer_storage.egress_boundary.security_owner_subject_sha256 != var.customer_storage.egress_boundary.workloads_subject_sha256 &&
        var.customer_storage.egress_boundary.provider_authority.schema == "fs2-serve.nebius.ai/customer-storage-provider-egress-handoff/v4" &&
        var.customer_storage.egress_boundary.provider_authority.contract_sha256 == var.customer_storage.egress_boundary.contract_sha256 &&
        can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.customer_storage.egress_boundary.provider_authority.generation)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.authority_manifest_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.prior_head_receipt_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.predecessor_state_custody_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.predecessor_state_compatibility_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.predecessor_compatibility_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.boundary_policy_sha256)) &&
        var.customer_storage.egress_boundary.boundary_policy_sha256 == var.customer_storage.egress_boundary.provider_authority.boundary_policy_sha256 &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.workload_policy_sha256)) &&
        var.customer_storage.egress_boundary.workload_policy_sha256 == var.customer_storage.egress_boundary.provider_authority.workload_policy_sha256 &&
        endswith(var.customer_storage.egress_boundary.boundary_policy_name, substr(var.customer_storage.egress_boundary.boundary_policy_sha256, 0, 12)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.release_values_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.authority_service_account_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.workloads_service_account_sha256)) &&
        var.customer_storage.egress_boundary.provider_authority.authority_service_account_sha256 != var.customer_storage.egress_boundary.provider_authority.workloads_service_account_sha256 &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.provider_identity_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_identity_inventory_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_service_account_inventory_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_system_subject_inventory_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_rbac_inventory_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_rbac_effective_authority_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.kubernetes_rbac_inventory_receipt_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.provider_project_iam_inventory_receipt_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.provider_effective_authority_graph_receipt_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.provider_authority_adapter_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.provider_state_custody_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.boundary_state_custody_sha256)) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.retained_v3_admission_custody_sha256)) &&
        sha256(jsonencode({
          retained_v3_boundary_policies = var.customer_storage.egress_boundary.provider_authority.retained_v3_boundary_policies
          retained_v3_workload_policies = var.customer_storage.egress_boundary.provider_authority.retained_v3_workload_policies
        })) == var.customer_storage.egress_boundary.provider_authority.retained_v3_admission_custody_sha256 &&
        var.customer_storage.egress_boundary.identity_inventory_sha256 == var.customer_storage.egress_boundary.provider_authority.kubernetes_identity_inventory_sha256 &&
        can(regex("^[a-f0-9]{40}$", var.customer_storage.egress_boundary.provider_authority.accepted_sai10_commit)) &&
        can(regex("^[a-f0-9]{40}$", var.customer_storage.egress_boundary.provider_authority.accepted_sai10_tree)) &&
        !startswith(var.customer_storage.egress_boundary.provider_authority.accepted_sai10_commit, "1ae009b85") &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.provider_authority.sai10_independent_review_receipt_sha256)) &&
        can(regex("^vpcsecuritygroup-[a-z0-9]+$", var.customer_storage.egress_boundary.provider_authority.security_group_id)) &&
        can(regex("^mk8snodegroup-[a-z0-9]+$", var.customer_storage.egress_boundary.provider_authority.node_group_id)) &&
        var.customer_storage.egress_boundary.provider_authority.node_selector_key == "workload.fs2.nebius/customer-storage-egress" &&
        var.customer_storage.egress_boundary.provider_authority.node_selector_value == var.customer_storage.egress_boundary.provider_authority.generation &&
        var.customer_storage.egress_boundary.provider_authority.taint_key == var.customer_storage.egress_boundary.provider_authority.node_selector_key &&
        var.customer_storage.egress_boundary.provider_authority.taint_value == var.customer_storage.egress_boundary.provider_authority.generation &&
        var.customer_storage.egress_boundary.provider_authority.taint_effect == "NoSchedule" &&
        var.customer_storage.egress_boundary.provider_authority.provider_api_cidrs == jsondecode(var.customer_storage.egress_contract_json).cidrs &&
        length(var.customer_storage.egress_boundary.provider_authority.kubernetes_api_cidrs) > 0 &&
        var.customer_storage.egress_boundary.predecessor_compatibility.schema == "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1" &&
        var.customer_storage.egress_boundary.predecessor_compatibility.receipt_sha256 == var.customer_storage.egress_boundary.provider_authority.predecessor_compatibility_sha256 &&
        alltrue([
          for digest in [
            var.customer_storage.egress_boundary.predecessor_compatibility.deployment_spec_sha256,
            var.customer_storage.egress_boundary.predecessor_compatibility.network_policy_sha256,
            var.customer_storage.egress_boundary.predecessor_compatibility.contract_data_sha256,
            var.customer_storage.egress_boundary.predecessor_compatibility.policy_spec_sha256,
            var.customer_storage.egress_boundary.predecessor_compatibility.binding_spec_sha256,
          ] : can(regex("^[a-f0-9]{64}$", digest))
        ]) &&
        can(regex("^[a-f0-9]{64}$", var.customer_storage.egress_boundary.predecessor_compatibility.contract_sha256)) &&
        length(var.customer_storage.egress_boundary.predecessor_compatibility.egress_cidrs) > 0 &&
        length(var.customer_storage.egress_boundary.predecessor_compatibility.kubernetes_api_cidrs) > 0 &&
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

# The ordinary workloads root must pass the same exact ancestry and custody
# gate as the separately credentialed security roots. A digest-looking commit
# string is not authority: the verifier resolves the Git objects, rejects the
# rejected SAI-10 lineage as an ancestor of either custody or HEAD, and binds
# every handoff digest to the independently accepted dependency record.
data "external" "customer_storage_integration_dependencies" {
  count = var.customer_storage.enabled ? 1 : 0
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/../../security/verify_sai08_integration_dependencies.py",
  ]
  query = {
    dependency_record_path = "${path.module}/../../security/sai-08-integration-dependencies.json"
    expected_dependencies_json = jsonencode({
      sai_10_accepted_commit                            = var.customer_storage.egress_boundary.provider_authority.accepted_sai10_commit
      sai_10_accepted_tree                              = var.customer_storage.egress_boundary.provider_authority.accepted_sai10_tree
      sai_10_independent_review_receipt_sha256          = var.customer_storage.egress_boundary.provider_authority.sai10_independent_review_receipt_sha256
      provider_authority_manifest_sha256                = var.customer_storage.egress_boundary.provider_authority.authority_manifest_sha256
      provider_authority_prior_head_receipt_sha256      = var.customer_storage.egress_boundary.provider_authority.prior_head_receipt_sha256
      provider_project_iam_inventory_receipt_sha256     = var.customer_storage.egress_boundary.provider_authority.provider_project_iam_inventory_receipt_sha256
      provider_effective_authority_graph_receipt_sha256 = var.customer_storage.egress_boundary.provider_authority.provider_effective_authority_graph_receipt_sha256
      provider_authority_adapter_sha256                 = var.customer_storage.egress_boundary.provider_authority.provider_authority_adapter_sha256
      provider_state_custody_sha256                     = var.customer_storage.egress_boundary.provider_authority.provider_state_custody_sha256
      boundary_state_custody_sha256                     = var.customer_storage.egress_boundary.provider_authority.boundary_state_custody_sha256
      retained_v3_admission_custody_sha256              = var.customer_storage.egress_boundary.provider_authority.retained_v3_admission_custody_sha256
      kubernetes_rbac_inventory_receipt_sha256          = var.customer_storage.egress_boundary.provider_authority.kubernetes_rbac_inventory_receipt_sha256
      kubernetes_rbac_effective_authority_sha256        = var.customer_storage.egress_boundary.provider_authority.kubernetes_rbac_effective_authority_sha256
      kubernetes_service_account_inventory_sha256       = var.customer_storage.egress_boundary.provider_authority.kubernetes_service_account_inventory_sha256
      kubernetes_system_subject_inventory_sha256        = var.customer_storage.egress_boundary.provider_authority.kubernetes_system_subject_inventory_sha256
      workload_policy_sha256                            = var.customer_storage.egress_boundary.provider_authority.workload_policy_sha256
      predecessor_state_custody_sha256                  = var.customer_storage.egress_boundary.provider_authority.predecessor_state_custody_sha256
      live_predecessor_compatibility_handoff_sha256     = var.customer_storage.egress_boundary.predecessor_compatibility.receipt_sha256
    })
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

data "kubernetes_resources" "customer_storage_all_network_policies" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  namespace   = "fs2-system"
}

data "kubernetes_resources" "customer_storage_reconciler_pods" {
  count          = var.customer_storage.enabled ? 1 : 0
  api_version    = "v1"
  kind           = "Pod"
  namespace      = "fs2-system"
  label_selector = "app.kubernetes.io/name=fs2-serve-control-plane,app.kubernetes.io/instance=fs2-serve-control-plane,app.kubernetes.io/component=storage-reconciler-v3,fs2.nebius.ai/storage-egress-generation=${var.customer_storage.egress_boundary.generation}"
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

data "kubernetes_resource" "customer_storage_workload_policy" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata { name = var.customer_storage.egress_boundary.workload_policy_name }
}

data "kubernetes_resource" "customer_storage_workload_binding" {
  count       = var.customer_storage.enabled ? 1 : 0
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata { name = var.customer_storage.egress_boundary.workload_policy_name }
}

data "kubernetes_resource" "customer_storage_retained_boundary_policy" {
  for_each    = var.customer_storage.enabled ? var.customer_storage.egress_boundary.provider_authority.retained_v3_boundary_policies : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata { name = each.value.name }
}

data "kubernetes_resource" "customer_storage_retained_boundary_binding" {
  for_each    = var.customer_storage.enabled ? var.customer_storage.egress_boundary.provider_authority.retained_v3_boundary_policies : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata { name = each.value.name }
}

data "kubernetes_resource" "customer_storage_retained_workload_policy" {
  for_each    = var.customer_storage.enabled ? var.customer_storage.egress_boundary.provider_authority.retained_v3_workload_policies : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata { name = each.value.name }
}

data "kubernetes_resource" "customer_storage_retained_workload_binding" {
  for_each    = var.customer_storage.enabled ? var.customer_storage.egress_boundary.provider_authority.retained_v3_workload_policies : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata { name = each.value.name }
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
        "app.kubernetes.io/component"             = "storage-reconciler-v3"
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
      cryptoSecretName              = local.active_storage_keyring_name
      egressCidrs                   = var.customer_storage.enabled ? var.customer_storage.egress_boundary.predecessor_compatibility.egress_cidrs : []
      kubernetesApiCidrs            = var.customer_storage.enabled ? var.customer_storage.egress_boundary.predecessor_compatibility.kubernetes_api_cidrs : []
      egressContractSha256          = var.customer_storage.enabled ? var.customer_storage.egress_boundary.predecessor_compatibility.contract_sha256 : ""
      keyTtlDays                    = var.customer_storage.key_ttl_days
      rotationWindowDays            = var.customer_storage.rotation_window_days
    }
  }
  customer_storage_v3_pod_label_sets = [
    for pod in data.kubernetes_resources.customer_storage_reconciler_pods[0].objects : pod.metadata.labels
  ]
}

data "external" "customer_storage_effective_egress" {
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
    contract_json             = var.customer_storage.egress_contract_json
    public_key_pem            = data.kubernetes_config_map_v1.customer_storage_egress_trust[0].data["public-key.pem"]
    network_policies_json     = jsonencode(data.kubernetes_resources.customer_storage_all_network_policies[0].objects)
    pod_label_sets_json       = jsonencode(local.customer_storage_v3_pod_label_sets)
    kubernetes_api_cidrs_json = jsonencode(local.customer_storage_kubernetes_api_cidrs)
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
    workload_policy_name = var.customer_storage.egress_boundary.workload_policy_name
  }

  lifecycle {
    precondition {
      condition     = data.external.customer_storage_integration_dependencies[0].result.authorized == "true"
      error_message = "Customer-storage workloads cannot consume rejected or unaccepted SAI-10/custody ancestry."
    }
    precondition {
      condition     = data.external.customer_storage_egress[0].result.contract_sha256 == var.customer_storage.egress_boundary.contract_sha256
      error_message = "The live/fresh signed contract digest differs from the external security-owner handoff."
    }
    precondition {
      condition     = var.customer_storage.egress_boundary.provider_authority.kubernetes_api_cidrs == local.customer_storage_kubernetes_api_cidrs
      error_message = "The provider VPC authority Kubernetes API routes differ from the live cluster contract."
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
      condition     = data.external.customer_storage_effective_egress[0].result.effective_policy_verified == "true"
      error_message = "The effective union of all NetworkPolicies selecting the v2 reconciler is not exact."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.labels["app.kubernetes.io/managed-by"], "") == "fs2-security-owner" &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.annotations["fs2.nebius.ai/security-owner-subject-sha256"], "") == var.customer_storage.egress_boundary.security_owner_subject_sha256 &&
        try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.metadata.annotations["fs2.nebius.ai/workloads-subject-sha256"], "") == var.customer_storage.egress_boundary.workloads_subject_sha256 &&
        sha256(jsonencode(try(data.kubernetes_resource.customer_storage_egress_boundary_policy[0].object.spec, null))) == var.customer_storage.egress_boundary.boundary_policy_sha256
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
    precondition {
      condition = (
        try(data.kubernetes_resource.customer_storage_workload_policy[0].object.metadata.labels["app.kubernetes.io/managed-by"], "") == "fs2-security-owner" &&
        try(data.kubernetes_resource.customer_storage_workload_policy[0].object.metadata.annotations["fs2.nebius.ai/workload-policy-sha256"], "") == var.customer_storage.egress_boundary.workload_policy_sha256 &&
        sha256(jsonencode(try(data.kubernetes_resource.customer_storage_workload_policy[0].object.spec, null))) == var.customer_storage.egress_boundary.workload_policy_sha256 &&
        try(data.kubernetes_resource.customer_storage_workload_binding[0].object.spec.policyName, "") == var.customer_storage.egress_boundary.workload_policy_name &&
        try(data.kubernetes_resource.customer_storage_workload_binding[0].object.spec.validationActions, []) == ["Deny"]
      )
      error_message = "The exhaustive content-bound successor workload admission boundary is absent."
    }
    precondition {
      condition = alltrue([
        for generation, expected in var.customer_storage.egress_boundary.provider_authority.retained_v3_boundary_policies :
        try(data.kubernetes_resource.customer_storage_retained_boundary_policy[generation].object.spec, null) == expected.policy_spec &&
        sha256(jsonencode(try(data.kubernetes_resource.customer_storage_retained_boundary_policy[generation].object.spec, null))) == expected.policy_sha256 &&
        try(data.kubernetes_resource.customer_storage_retained_boundary_binding[generation].object.spec, null) == expected.binding_spec
      ])
      error_message = "A retained customer-storage boundary VAP or Deny binding drifted from signed custody."
    }
    precondition {
      condition = alltrue([
        for generation, expected in var.customer_storage.egress_boundary.provider_authority.retained_v3_workload_policies :
        try(data.kubernetes_resource.customer_storage_retained_workload_policy[generation].object.spec, null) == expected.policy_spec &&
        sha256(jsonencode(try(data.kubernetes_resource.customer_storage_retained_workload_policy[generation].object.spec, null))) == expected.policy_sha256 &&
        try(data.kubernetes_resource.customer_storage_retained_workload_binding[generation].object.spec, null) == expected.binding_spec
      ])
      error_message = "A retained customer-storage workload VAP or Deny binding drifted from signed custody."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

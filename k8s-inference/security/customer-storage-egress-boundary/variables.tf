variable "security_owner_kubeconfig_path" {
  description = "Dedicated security-owner kubeconfig. It must not be the workloads kubeconfig."
  type        = string

  validation {
    condition     = startswith(var.security_owner_kubeconfig_path, "/") && !strcontains(var.security_owner_kubeconfig_path, "..")
    error_message = "security_owner_kubeconfig_path must be an absolute path without parent traversal."
  }
}

variable "security_owner_kube_context" {
  description = "Exact context selected by the dedicated security-owner provider."
  type        = string

  validation {
    condition     = var.security_owner_kube_context != ""
    error_message = "security_owner_kube_context is required."
  }
}

variable "security_owner_username" {
  description = "Exact authenticated Kubernetes username from the externally signed identity inventory."
  type        = string
  validation {
    condition     = var.security_owner_username != ""
    error_message = "security_owner_username is required."
  }
}

variable "security_owner_credential_sha256" {
  description = "SHA-256 of the exact descriptor-read security-owner kubeconfig bytes."
  type        = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.security_owner_credential_sha256))
    error_message = "security_owner_credential_sha256 must be a SHA-256 digest."
  }
}

variable "security_owner_provider_principal_id" {
  description = "Provider principal bound to the Kubernetes security-owner identity."
  type        = string
  validation {
    condition     = var.security_owner_provider_principal_id != ""
    error_message = "security_owner_provider_principal_id is required."
  }
}

variable "workloads_kubeconfig_path" {
  description = "Ordinary workloads kubeconfig, used only for a fail-closed identity-separation assertion."
  type        = string

  validation {
    condition     = startswith(var.workloads_kubeconfig_path, "/") && !strcontains(var.workloads_kubeconfig_path, "..")
    error_message = "workloads_kubeconfig_path must be an absolute path without parent traversal."
  }
}

variable "workloads_kube_context" {
  description = "Exact context used by the ordinary workloads provider."
  type        = string

  validation {
    condition     = var.workloads_kube_context != ""
    error_message = "workloads_kube_context is required."
  }
}

variable "workloads_username" {
  description = "Exact authenticated workloads username from the signed inventory."
  type        = string
  validation {
    condition     = var.workloads_username != ""
    error_message = "workloads_username is required."
  }
}

variable "workloads_credential_sha256" {
  description = "SHA-256 of the exact descriptor-read workloads kubeconfig bytes."
  type        = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.workloads_credential_sha256))
    error_message = "workloads_credential_sha256 must be a SHA-256 digest."
  }
}

variable "workloads_provider_principal_id" {
  description = "Provider principal bound to the Kubernetes workloads identity."
  type        = string
  validation {
    condition     = var.workloads_provider_principal_id != ""
    error_message = "workloads_provider_principal_id is required."
  }
}

variable "non_owner_identities" {
  description = "Every human, release, break-glass and other credential able to reach this cluster."
  type = map(object({
    kubeconfig_path = string
    kube_context    = string
    username        = string
    category        = string
    credential_sha256    = string
    provider_principal_id = string
  }))

  validation {
    condition = (
      length(var.non_owner_identities) >= 4 &&
      toset([for identity in values(var.non_owner_identities) : identity.category]) == toset(["release", "human", "break-glass", "other"]) &&
      alltrue([
        for name, identity in var.non_owner_identities :
        can(regex("^[a-z0-9][a-z0-9-]{0,62}$", name)) &&
        name != "workloads" &&
        contains(["release", "human", "break-glass", "other"], identity.category) &&
        startswith(identity.kubeconfig_path, "/") &&
        !strcontains(identity.kubeconfig_path, "..") &&
        identity.kube_context != "" &&
        identity.username != "" &&
        can(regex("^[a-f0-9]{64}$", identity.credential_sha256)) &&
        identity.provider_principal_id != ""
      ])
    )
    error_message = "Enumerate every distinct release, human, break-glass and other identity with a category and absolute private kubeconfig."
  }
}

variable "security_owner_group" {
  description = "Authenticated group that alone may add protected customer-storage egress objects."
  type        = string
  default     = "fs2:customer-storage-egress-security-owner"

  validation {
    condition     = var.security_owner_group == "fs2:customer-storage-egress-security-owner"
    error_message = "The customer-storage boundary uses the dedicated fixed security-owner group."
  }
}

variable "release_identity_name" {
  description = "Exact signed-inventory release identity used only for the additive v2 Helm install."
  type        = string
  validation {
    condition     = var.release_identity_name != "" && var.release_identity_name != "workloads"
    error_message = "release_identity_name is required and cannot be the workloads identity."
  }
}

variable "release_generations" {
  description = "Append-only v2 release payloads; custody identities come only from provider_authority."
  type = map(object({
    rollout_generation              = string
    image_repository                = string
    image_digest                    = string
    image_pull_secrets              = list(string)
    storage_project_id              = string
    storage_region                  = string
    quota_bytes                     = number
    excluded_tenants                = set(string)
    resource_credentials_secret_name = string
    iam_credentials_secret_name      = string
    database_secret_name             = string
    crypto_secret_name               = string
    storage_generation               = number
    key_ttl_days                      = number
    rotation_window_days              = number
    action_timeout_seconds            = number
  }))

  validation {
    condition = length(var.release_generations) > 0 && alltrue([
      for generation, release in var.release_generations :
      generation == release.rollout_generation &&
      can(regex("^r[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      release.image_repository != "" &&
      can(regex("^sha256:[a-f0-9]{64}$", release.image_digest)) &&
      release.storage_project_id != "" &&
      release.storage_region != "" &&
      release.quota_bytes > 0 &&
      release.resource_credentials_secret_name != "" &&
      release.iam_credentials_secret_name != "" &&
      release.database_secret_name != "" &&
      release.crypto_secret_name != "" &&
      release.storage_generation >= 1 &&
      release.key_ttl_days >= 1 && release.key_ttl_days <= 365 &&
      release.rotation_window_days >= 1 &&
      release.rotation_window_days < release.key_ttl_days &&
      release.action_timeout_seconds >= 1 && release.action_timeout_seconds <= 120
    ])
    error_message = "Every release generation must be an exact bounded additive v2 payload."
  }
}

variable "current_release_generation" {
  description = "New release generation selected for this additive apply."
  type        = string
  validation {
    condition = (
      can(regex("^r[0-9]{14}-[a-f0-9]{12}$", var.current_release_generation)) &&
      contains(keys(var.release_generations), var.current_release_generation)
    )
    error_message = "current_release_generation must name a retained release generation."
  }
}

variable "provider_authority" {
  description = "Exact handoff from the independently approved Nebius VPC/node authority root."
  type = object({
    schema                                        = string
    generation                                    = string
    authority_manifest_sha256                     = string
    prior_head_receipt_sha256                     = string
    predecessor_state_custody_sha256              = string
    predecessor_state_compatibility_sha256        = string
    contract_sha256                               = string
    predecessor_compatibility_sha256              = string
    boundary_policy_sha256                        = string
    release_values_sha256                         = string
    security_group_id                             = string
    node_group_id                                 = string
    node_selector_key                             = string
    node_selector_value                           = string
    taint_key                                     = string
    taint_value                                   = string
    taint_effect                                  = string
    provider_api_cidrs                            = list(string)
    kubernetes_api_cidrs                          = list(string)
    authority_service_account_sha256              = string
    provider_identity_sha256                      = string
    kubernetes_identity_inventory_sha256           = string
    kubernetes_rbac_inventory_sha256               = string
    kubernetes_rbac_inventory_receipt_sha256       = string
    provider_project_iam_inventory_receipt_sha256 = string
    workloads_service_account_sha256              = string
    accepted_sai10_commit                         = string
    accepted_sai10_tree                           = string
    sai10_independent_review_receipt_sha256       = string
  })

  validation {
    condition = (
      var.provider_authority.schema == "fs2-serve.nebius.ai/customer-storage-provider-egress-handoff/v1" &&
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.provider_authority.generation)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.authority_manifest_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.prior_head_receipt_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_state_custody_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_state_compatibility_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.contract_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_compatibility_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.boundary_policy_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.release_values_sha256)) &&
      can(regex("^vpcsecuritygroup-[a-z0-9]+$", var.provider_authority.security_group_id)) &&
      can(regex("^mk8snodegroup-[a-z0-9]+$", var.provider_authority.node_group_id)) &&
      var.provider_authority.node_selector_key == "workload.fs2.nebius/customer-storage-egress" &&
      var.provider_authority.node_selector_value == var.provider_authority.generation &&
      var.provider_authority.taint_key == var.provider_authority.node_selector_key &&
      var.provider_authority.taint_value == var.provider_authority.generation &&
      var.provider_authority.taint_effect == "NoSchedule" &&
      length(var.provider_authority.provider_api_cidrs) > 0 &&
      length(var.provider_authority.kubernetes_api_cidrs) > 0 &&
      alltrue([
        for cidr in concat(var.provider_authority.provider_api_cidrs, var.provider_authority.kubernetes_api_cidrs) :
        can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$|^[0-9A-Fa-f:]+/128$", cidr))
      ]) &&
      alltrue([
        for digest in [
          var.provider_authority.authority_service_account_sha256,
          var.provider_authority.provider_identity_sha256,
          var.provider_authority.kubernetes_identity_inventory_sha256,
          var.provider_authority.kubernetes_rbac_inventory_sha256,
          var.provider_authority.kubernetes_rbac_inventory_receipt_sha256,
          var.provider_authority.provider_project_iam_inventory_receipt_sha256,
          var.provider_authority.workloads_service_account_sha256,
          var.provider_authority.sai10_independent_review_receipt_sha256,
        ] : can(regex("^[a-f0-9]{64}$", digest))
      ]) &&
      can(regex("^[a-f0-9]{40}$", var.provider_authority.accepted_sai10_commit)) &&
      can(regex("^[a-f0-9]{40}$", var.provider_authority.accepted_sai10_tree)) &&
      !startswith(var.provider_authority.accepted_sai10_commit, "1ae009b85") &&
      var.provider_authority.authority_service_account_sha256 != var.provider_authority.workloads_service_account_sha256
    )
    error_message = "provider_authority must be the exact provider-enforced, identity-separated VPC/node handoff."
  }
}

variable "contract_generations" {
  description = "Append-only signed egress generations. Existing keys must never be removed or changed."
  type = map(object({
    contract_json        = string
    trust_generation     = string
    kubernetes_api_cidrs = set(string)
  }))

  validation {
    condition = length(var.contract_generations) > 0 && alltrue([
      for generation, contract in var.contract_generations :
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      try(endswith(generation, substr(sha256(jsonencode(jsondecode(contract.contract_json))), 0, 12)), false) &&
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", contract.trust_generation)) &&
      contains(keys(var.trust_generations), contract.trust_generation) &&
      length(contract.kubernetes_api_cidrs) > 0 &&
      alltrue([
        for cidr in contract.kubernetes_api_cidrs :
        can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/32$|^[0-9A-Fa-f:]+/128$", cidr))
      ])
    ])
    error_message = "Every append-only contract needs generation identities and exact Kubernetes API host routes."
  }
}

variable "trust_generations" {
  description = "Append-only public Ed25519 trust generations. Existing keys must never be removed or changed."
  type = map(object({
    public_key_pem = string
  }))

  validation {
    condition = length(var.trust_generations) > 0 && alltrue([
      for generation, trust in var.trust_generations :
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      endswith(generation, substr(sha256(trust.public_key_pem), 0, 12)) &&
      startswith(trust.public_key_pem, "-----BEGIN PUBLIC KEY-----")
    ])
    error_message = "Every append-only trust generation needs a generation identity and PEM public key."
  }
}

variable "boundary_generations" {
  description = "Append-only admission-boundary generations. Removing an installed generation is forbidden."
  type        = set(string)

  validation {
    condition = length(var.boundary_generations) > 0 && alltrue([
      for generation in var.boundary_generations : can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation))
    ])
    error_message = "At least one versioned admission-boundary generation is required."
  }
}

variable "current_generation" {
  description = "Contract generation selected by the next workloads rollout."
  type        = string

  validation {
    condition = (
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.current_generation)) &&
      contains(keys(var.contract_generations), var.current_generation)
    )
    error_message = "current_generation must identify a retained contract generation."
  }
}

variable "current_boundary_generation" {
  description = "Admission-boundary generation attested in the workloads handoff."
  type        = string

  validation {
    condition = (
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.current_boundary_generation)) &&
      contains(var.boundary_generations, var.current_boundary_generation)
    )
    error_message = "current_boundary_generation must identify a retained admission boundary."
  }
}

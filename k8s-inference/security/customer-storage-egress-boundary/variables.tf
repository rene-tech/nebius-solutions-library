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

variable "non_owner_identities" {
  description = "Every human, release, break-glass and other credential able to reach this cluster."
  type = map(object({
    kubeconfig_path = string
    kube_context    = string
    username        = string
    category        = string
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
        identity.username != ""
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

variable "provider_authority" {
  description = "Exact handoff from the independently approved Nebius VPC/node authority root."
  type = object({
    schema                                        = string
    generation                                    = string
    authority_manifest_sha256                     = string
    contract_sha256                               = string
    predecessor_compatibility_sha256              = string
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
    kubernetes_security_owner_sha256              = string
    kubernetes_workloads_sha256                   = string
    kubernetes_non_owner_subjects_sha256          = string
    provider_project_iam_inventory_receipt_sha256 = string
    workloads_service_account_sha256              = string
    release_service_accounts_sha256               = string
    human_principals_sha256                       = string
  })

  validation {
    condition = (
      var.provider_authority.schema == "fs2-serve.nebius.ai/customer-storage-provider-egress-handoff/v1" &&
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.provider_authority.generation)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.authority_manifest_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.contract_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_compatibility_sha256)) &&
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
          var.provider_authority.kubernetes_security_owner_sha256,
          var.provider_authority.kubernetes_workloads_sha256,
          var.provider_authority.kubernetes_non_owner_subjects_sha256,
          var.provider_authority.provider_project_iam_inventory_receipt_sha256,
          var.provider_authority.workloads_service_account_sha256,
          var.provider_authority.release_service_accounts_sha256,
          var.provider_authority.human_principals_sha256,
        ] : can(regex("^[a-f0-9]{64}$", digest))
      ]) &&
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

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

variable "security_owner_group" {
  description = "Authenticated group that alone may add protected customer-storage egress objects."
  type        = string
  default     = "fs2:customer-storage-egress-security-owner"

  validation {
    condition     = var.security_owner_group == "fs2:customer-storage-egress-security-owner"
    error_message = "The customer-storage boundary uses the dedicated fixed security-owner group."
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

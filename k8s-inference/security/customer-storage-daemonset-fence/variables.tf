variable "security_owner_kubeconfig_path" {
  type      = string
  sensitive = true
}

variable "security_owner_kube_context" {
  type = string
}

variable "fence_generations" {
  description = "Append-only, externally signed webhook generations. Existing keys may never be omitted."
  type = map(object({
    name                         = string
    cluster_id                   = string
    external_url                 = string
    ca_bundle                    = string
    enforcer_bundle_sha256       = string
    enforcer_image_digest        = string
    runtime_state_head_sha256    = string
    cutover_url                  = string
    cutover_source_bundle_sha256 = string
    cutover_receipt_sha256       = string
    activation_public_key_pem    = string
    activation_trust_config_map_name = string
    activation_ca_config_map_name    = string
    security_owner_receipt_sha256 = string
    cutover_executor_username      = string
    cutover_deployment_names       = set(string)
  }))
  validation {
    condition = length(var.fence_generations) > 0 && alltrue([
      for generation, fence in var.fence_generations :
      can(regex("^f[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      endswith(generation, substr(sha256(jsonencode({
        cluster_id                = fence.cluster_id
        name                      = fence.name
        external_url              = fence.external_url
        ca_bundle_sha256          = sha256(fence.ca_bundle)
        enforcer_bundle_sha256    = fence.enforcer_bundle_sha256
        enforcer_image_digest     = fence.enforcer_image_digest
        runtime_state_head_sha256 = fence.runtime_state_head_sha256
        cutover_url               = fence.cutover_url
        cutover_source_bundle_sha256 = fence.cutover_source_bundle_sha256
        cutover_receipt_sha256    = fence.cutover_receipt_sha256
        activation_public_key_pem = fence.activation_public_key_pem
        activation_trust_config_map_name = fence.activation_trust_config_map_name
        activation_ca_config_map_name = fence.activation_ca_config_map_name
        security_owner_receipt_sha256 = fence.security_owner_receipt_sha256
        cutover_executor_username = fence.cutover_executor_username
        cutover_deployment_names  = sort(tolist(fence.cutover_deployment_names))
      })), 0, 12)) &&
      fence.name == "fs2-storage-daemonset-fence-${generation}" &&
      can(regex("^https://[^/]+/validate$", fence.external_url)) &&
      can(base64decode(fence.ca_bundle)) &&
      can(regex("^[a-f0-9]{64}$", fence.enforcer_bundle_sha256)) &&
      can(regex("^[^@]+@sha256:[a-f0-9]{64}$", fence.enforcer_image_digest)) &&
      can(regex("^[a-f0-9]{64}$", fence.runtime_state_head_sha256)) &&
      can(regex("^https://[^/]+/validate-storage-reconciler$", fence.cutover_url)) &&
      can(regex("^[a-f0-9]{64}$", fence.cutover_source_bundle_sha256)) &&
      can(regex("^[a-f0-9]{64}$", fence.cutover_receipt_sha256)) &&
      startswith(fence.activation_public_key_pem, "-----BEGIN PUBLIC KEY-----") &&
      can(regex("^fs2-storage-activation-trust-r[0-9]{14}-[a-f0-9]{12}$", fence.activation_trust_config_map_name)) &&
      can(regex("^fs2-storage-activation-ca-r[0-9]{14}-[a-f0-9]{12}$", fence.activation_ca_config_map_name)) &&
      can(regex("^[a-f0-9]{64}$", fence.security_owner_receipt_sha256))
      && can(regex("^fs2-security-owner:[a-z0-9][a-z0-9._-]{2,127}$", fence.cutover_executor_username))
      && length(fence.cutover_deployment_names) >= 2
      && alltrue([
        for name in fence.cutover_deployment_names :
        can(regex("^fs2-storage-v3-[a-f0-9]{12}-[a-f0-9]{12}$", name))
      ])
    ])
    error_message = "Every retained fence generation must be content-bound to an exact external HTTPS enforcer and signed owner receipt."
  }
}

variable "retained_generation_receipt_sha256" {
  description = "Separate owner receipt proving this map is a superset of every installed generation."
  type        = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.retained_generation_receipt_sha256))
    error_message = "The retained fence-generation receipt is required."
  }
}

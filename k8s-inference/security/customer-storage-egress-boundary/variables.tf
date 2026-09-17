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

variable "security_owner_groups" {
  description = "Exact sorted authenticated group set from the signed authority inventory."
  type        = list(string)
  validation {
    condition     = length(var.security_owner_groups) > 0 && var.security_owner_groups == sort(distinct(var.security_owner_groups))
    error_message = "security_owner_groups must be a non-empty sorted unique signed set."
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

variable "workloads_groups" {
  description = "Exact sorted authenticated group set from the signed authority inventory."
  type        = list(string)
  validation {
    condition     = length(var.workloads_groups) > 0 && var.workloads_groups == sort(distinct(var.workloads_groups))
    error_message = "workloads_groups must be a non-empty sorted unique signed set."
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
    kubeconfig_path       = string
    kube_context          = string
    username              = string
    groups                = list(string)
    category              = string
    credential_sha256     = string
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
        length(identity.groups) > 0 &&
        identity.groups == sort(distinct(identity.groups)) &&
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

variable "kubernetes_service_account_inventory" {
  description = "Exact sorted ServiceAccount subject closure from the signed RBAC authority receipt."
  type = list(object({
    namespace                  = string
    name                       = string
    uid                        = string
    owner                      = string
    groups                     = list(string)
    effective_authority_sha256 = string
    dangerous_permissions      = list(string)
  }))
  validation {
    condition = (
      length(var.kubernetes_service_account_inventory) > 0 &&
      alltrue([
        for subject in var.kubernetes_service_account_inventory :
        subject.namespace != "" && subject.name != "" && subject.owner != "" &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", subject.uid)) &&
        subject.groups == sort([
          "system:authenticated",
          "system:serviceaccounts",
          "system:serviceaccounts:${subject.namespace}",
        ]) &&
        can(regex("^[a-f0-9]{64}$", subject.effective_authority_sha256)) &&
        subject.dangerous_permissions == sort(distinct(subject.dangerous_permissions))
      ])
    )
    error_message = "The complete signed Kubernetes ServiceAccount subject inventory is required."
  }
}

variable "kubernetes_system_subject_inventory" {
  description = "Exact Kubernetes-native system User/Group subjects from the signed RBAC authority receipt."
  type = list(object({
    kind                       = string
    name                       = string
    uid                        = string
    namespace                  = string
    owner                      = string
    groups                     = list(string)
    effective_authority_sha256 = string
    dangerous_permissions      = list(string)
  }))
  validation {
    condition = alltrue([
      for subject in var.kubernetes_system_subject_inventory :
      contains(["User", "Group"], subject.kind) &&
      startswith(subject.name, "system:") &&
      subject.namespace == "" && subject.uid != "" && subject.owner != "" &&
      subject.groups == (
        subject.kind == "Group" ? [] :
        subject.name == "system:anonymous" ? ["system:unauthenticated"] :
        ["system:authenticated"]
      ) &&
      can(regex("^[a-f0-9]{64}$", subject.effective_authority_sha256)) &&
      subject.dangerous_permissions == sort(distinct(subject.dangerous_permissions))
    ])
    error_message = "Only explicitly owned Kubernetes-native system User/Group subjects are accepted."
  }
}

variable "controller_identities" {
  description = "Audit-proven live kube-system controller ServiceAccounts, including immutable UID and deterministic authenticated groups."
  type = map(object({
    kind                  = string
    namespace             = string
    name                  = string
    username              = string
    uid                   = string
    groups                = list(string)
    audit_evidence_sha256 = string
  }))
  validation {
    condition = (
      toset(keys(var.controller_identities)) == toset(["deployment", "replicaset", "daemonset", "scheduler", "node_health"]) &&
      alltrue([
        for identity in values(var.controller_identities) :
        (
          (
            identity.kind == "ServiceAccount" && identity.namespace == "kube-system" &&
            identity.username == "system:serviceaccount:kube-system:${identity.name}" &&
            can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", identity.uid)) &&
            identity.groups == ["system:authenticated", "system:serviceaccounts", "system:serviceaccounts:kube-system"]
          ) || (
            identity.kind == "User" && identity.namespace == "" &&
            startswith(identity.name, "system:") && identity.username == identity.name &&
            identity.uid != "" && identity.groups == ["system:authenticated"]
          )
        ) &&
        can(regex("^[a-f0-9]{64}$", identity.audit_evidence_sha256))
      ])
    )
    error_message = "Every controller must be an audit-proven kube-system ServiceAccount or native system User identity, not a controller role label."
  }
}

variable "release_identity_name" {
  description = "Exact signed-inventory release identity used only for the additive compatibility-v3 Helm install."
  type        = string
  validation {
    condition     = var.release_identity_name != "" && var.release_identity_name != "workloads"
    error_message = "release_identity_name is required and cannot be the workloads identity."
  }
}

variable "release_generations" {
  description = "Append-only release payloads; custody identities come only from provider_authority."
  type = map(object({
    rollout_generation               = string
    image_repository                 = string
    image_digest                     = string
    image_pull_secrets               = list(string)
    storage_project_id               = string
    storage_region                   = string
    quota_bytes                      = number
    excluded_tenants                 = set(string)
    resource_credentials_secret_name = string
    iam_credentials_secret_name      = string
    database_secret_name             = string
    crypto_secret_name               = string
    storage_generation               = number
    key_ttl_days                     = number
    rotation_window_days             = number
    action_timeout_seconds           = number
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
    error_message = "Every release generation must be an exact bounded additive payload."
  }
}

variable "legacy_release_generations" {
  description = "Exact retained v2 Helm release generation keys; these instances remain state-owned and are never reused for v3."
  type        = set(string)
  validation {
    condition = alltrue([
      for generation in var.legacy_release_generations : contains(keys(var.release_generations), generation)
    ])
    error_message = "legacy_release_generations must be a subset of retained release_generations."
  }
}

variable "current_release_generation" {
  description = "New release generation selected for this additive apply."
  type        = string
  validation {
    condition = (
      can(regex("^r[0-9]{14}-[a-f0-9]{12}$", var.current_release_generation)) &&
      contains(keys(var.release_generations), var.current_release_generation) &&
      !contains(var.legacy_release_generations, var.current_release_generation) &&
      try(var.non_owner_identities[var.release_identity_name].username, "") == "fs2:customer-storage-release:${var.current_release_generation}"
    )
    error_message = "current_release_generation must name a retained release generation."
  }
}

variable "provider_authority" {
  description = "Exact handoff from the independently approved Nebius VPC/node authority root."
  type = object({
    schema                                 = string
    generation                             = string
    cluster_id                             = string
    provisioning_generation                = string
    provisioning_receipt_sha256             = string
    lane_id                                = string
    authority_manifest_sha256              = string
    prior_head_receipt_sha256              = string
    predecessor_state_custody_sha256       = string
    predecessor_state_compatibility_sha256 = string
    contract_sha256                        = string
    predecessor_compatibility_sha256       = string
    boundary_policy_sha256                 = string
    workload_policy_sha256                 = string
    release_values_sha256                  = string
    security_group_id                      = string
    node_group_id                          = string
    node_selector_key                      = string
    node_selector_value                    = string
    taint_key                              = string
    taint_value                            = string
    taint_effect                           = string
    min_node_count                         = number
    max_node_count                         = number
    protected_observers = map(object({
      class                 = string
      namespace             = string
      name                  = string
      uid                   = string
      owner_identity = object({
        username = string
        uid      = string
        groups   = list(string)
      })
      daemonset_spec        = any
      daemonset_spec_sha256 = string
      maintenance_audit_sha256 = optional(string)
      snapshot_generation      = optional(string)
      snapshot_sha256          = optional(string)
    }))
    protected_observer_inventory_sha256               = string
    protected_node_names                              = list(string)
    protected_node_inventory_sha256                   = string
    protected_node_scheduling_labels                  = map(map(string))
    protected_node_scheduling_labels_sha256           = string
    protected_node_attestations                       = map(any)
    protected_node_attestation_sha256                 = string
    provider_api_cidrs                                = list(string)
    kubernetes_api_cidrs                              = list(string)
    authority_service_account_sha256                  = string
    provider_identity_sha256                          = string
    kubernetes_identity_inventory_sha256              = string
    kubernetes_service_account_inventory_sha256       = string
    kubernetes_system_subject_inventory_sha256        = string
    controller_identities                             = map(any)
    controller_audit_receipt_sha256                   = string
    node_health_mutation = object({
      identity_role       = string
      mutable_label_keys  = list(string)
      mutable_taint_keys  = list(string)
      allow_unschedulable = bool
    })
    daemonset_inventory_sha256                        = string
    daemonset_list_resource_version                   = string
    daemonset_admission_fence_receipt_sha256          = string
    daemonset_snapshot_ledger_head_sha256             = string
    node_lifecycle_mode                               = string
    kubernetes_rbac_inventory_sha256                  = string
    kubernetes_rbac_effective_authority_sha256        = string
    kubernetes_rbac_inventory_receipt_sha256          = string
    provider_project_iam_inventory_receipt_sha256     = string
    provider_effective_authority_graph_receipt_sha256 = string
    provider_authority_adapter_sha256                 = string
    provider_state_custody_sha256                     = string
    boundary_state_custody_sha256                     = string
    retained_legacy_boundary_policies = map(object({
      name          = string
      policy_sha256 = string
      policy_spec   = any
      binding_spec = object({
        policyName        = string
        validationActions = list(string)
      })
    }))
    retained_legacy_workload_policies = map(object({
      name          = string
      policy_sha256 = string
      policy_spec   = any
      binding_spec = object({
        policyName        = string
        validationActions = list(string)
      })
    }))
    retained_v3_boundary_policies = map(object({
      name          = string
      policy_sha256 = string
      policy_spec   = any
      binding_spec = object({
        policyName        = string
        validationActions = list(string)
      })
    }))
    retained_v3_workload_policies = map(object({
      name          = string
      policy_sha256 = string
      policy_spec   = any
      binding_spec = object({
        policyName        = string
        validationActions = list(string)
      })
    }))
    retained_admission_custody_sha256       = string
    workloads_service_account_sha256        = string
    accepted_sai10_commit                   = string
    accepted_sai10_tree                     = string
    sai10_independent_review_receipt_sha256 = string
  })

  validation {
    condition = (
      var.provider_authority.schema == "fs2-serve.nebius.ai/customer-storage-provider-egress-handoff/v12" &&
      var.provider_authority.cluster_id != "" &&
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.provider_authority.generation)) &&
      can(regex("^p[0-9]{14}-[a-f0-9]{12}$", var.provider_authority.provisioning_generation)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.provisioning_receipt_sha256)) &&
      can(regex("^l[0-9]{14}-[a-f0-9]{12}$", var.provider_authority.lane_id)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.authority_manifest_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.prior_head_receipt_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_state_custody_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_state_compatibility_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.contract_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.predecessor_compatibility_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.boundary_policy_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.workload_policy_sha256)) &&
      can(regex("^[a-f0-9]{64}$", var.provider_authority.release_values_sha256)) &&
      can(regex("^vpcsecuritygroup-[a-z0-9]+$", var.provider_authority.security_group_id)) &&
      can(regex("^mk8snodegroup-[a-z0-9]+$", var.provider_authority.node_group_id)) &&
      var.provider_authority.node_selector_key == "workload.fs2.nebius/customer-storage-egress-${substr(var.provider_authority.lane_id, -12, 12)}" &&
      var.provider_authority.node_selector_value == var.provider_authority.lane_id &&
      var.provider_authority.taint_key == var.provider_authority.node_selector_key &&
      var.provider_authority.taint_value == var.provider_authority.lane_id &&
      var.provider_authority.taint_effect == "NoSchedule" &&
      var.provider_authority.min_node_count == 1 &&
      var.provider_authority.max_node_count == 1 &&
      length(var.provider_authority.protected_node_names) == 1 &&
      var.provider_authority.protected_node_names == sort(distinct(var.provider_authority.protected_node_names)) &&
      alltrue([
        for node_name in var.provider_authority.protected_node_names :
        can(regex("^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$", node_name))
      ]) &&
      sha256(jsonencode(var.provider_authority.protected_node_names)) == var.provider_authority.protected_node_inventory_sha256 &&
      toset(keys(var.provider_authority.protected_node_scheduling_labels)) == toset(var.provider_authority.protected_node_names) &&
      alltrue([
        for node_name, labels in var.provider_authority.protected_node_scheduling_labels :
        length(labels) > 0 &&
        try(labels[var.provider_authority.node_selector_key], "") == var.provider_authority.lane_id
      ]) &&
      sha256(jsonencode(var.provider_authority.protected_node_scheduling_labels)) == var.provider_authority.protected_node_scheduling_labels_sha256 &&
      toset(keys(var.provider_authority.protected_node_attestations)) == toset(var.provider_authority.protected_node_names) &&
      alltrue([
        for node_name, attestation in var.provider_authority.protected_node_attestations :
        try(attestation.name, "") == node_name &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", try(attestation.uid, ""))) &&
        try(attestation.resource_version, "") != "" &&
        try(attestation.provider_id, "") != "" &&
        try(attestation.node_group_id, "") == var.provider_authority.node_group_id &&
        try(attestation.provisioning_receipt_sha256, "") == var.provider_authority.provisioning_receipt_sha256 &&
        try(attestation.observed_at, "") != "" &&
        try(attestation.labels, {}) == var.provider_authority.protected_node_scheduling_labels[node_name] &&
        contains(try(attestation.taints, []), {
          key    = var.provider_authority.taint_key
          value  = var.provider_authority.taint_value
          effect = var.provider_authority.taint_effect
        })
      ]) &&
      sha256(jsonencode(var.provider_authority.protected_node_attestations)) == var.provider_authority.protected_node_attestation_sha256 &&
      length(var.provider_authority.protected_observers) > 0 &&
      alltrue([
        for role, observer in var.provider_authority.protected_observers :
        contains(["lane", "critical-blanket-agent"], observer.class) &&
        observer.namespace != "" && observer.name != "" &&
        (observer.class == "lane" ? (
          observer.namespace == "kube-system" &&
          try(observer.daemonset_spec.template.metadata.labels["fs2.nebius.ai/protected-lane-id"], "") == var.provider_authority.lane_id
        ) : true) &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", observer.uid)) &&
        can(regex("^system:serviceaccount:[^:]+:[^:]+$", observer.owner_identity.username)) &&
        can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", observer.owner_identity.uid)) &&
        observer.owner_identity.groups == ["system:authenticated", "system:serviceaccounts", "system:serviceaccounts:${try(split(":", observer.owner_identity.username)[2], "")}"] &&
        can(regex("^[a-f0-9]{64}$", observer.daemonset_spec_sha256)) &&
        (observer.class != "critical-blanket-agent" || (
          can(regex("^[a-f0-9]{64}$", try(observer.maintenance_audit_sha256, ""))) &&
          can(regex("^s[0-9]{14}-[a-f0-9]{12}$", try(observer.snapshot_generation, ""))) &&
          can(regex("^[a-f0-9]{64}$", try(observer.snapshot_sha256, "")))
        )) &&
        sha256(jsonencode(observer.daemonset_spec)) == observer.daemonset_spec_sha256 &&
        try(observer.daemonset_spec.selector.matchLabels, {}) == try(observer.daemonset_spec.template.metadata.labels, {}) &&
        try(observer.daemonset_spec.template.metadata.labels["app.kubernetes.io/component"], "") != "" &&
        (observer.class == "lane" ? (
          try(observer.daemonset_spec.template.metadata.labels["fs2.nebius.ai/protected-lane-id"], "") == var.provider_authority.lane_id &&
          try(observer.daemonset_spec.template.spec.nodeSelector, {}) == { (var.provider_authority.node_selector_key) = var.provider_authority.lane_id } &&
          try(observer.daemonset_spec.template.spec.tolerations, []) == [{
            key      = var.provider_authority.taint_key
            operator = "Equal"
            value    = var.provider_authority.taint_value
            effect   = var.provider_authority.taint_effect
          }]
        ) : (
          try(observer.daemonset_spec.template.metadata.labels["fs2.nebius.ai/protected-lane-id"], "") == "" &&
          !contains(keys(try(observer.daemonset_spec.template.spec.nodeSelector, {})), var.provider_authority.node_selector_key) &&
          anytrue([
            for toleration in try(observer.daemonset_spec.template.spec.tolerations, []) :
            try(toleration.key, "") == "" &&
            try(toleration.operator, "") == "Exists" &&
            contains(["", "NoSchedule"], try(toleration.effect, ""))
          ])
        )) &&
        try(observer.daemonset_spec.template.spec.nodeName, "") == ""
      ]) &&
      sha256(jsonencode(var.provider_authority.protected_observers)) == var.provider_authority.protected_observer_inventory_sha256 &&
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
          var.provider_authority.kubernetes_service_account_inventory_sha256,
          var.provider_authority.kubernetes_system_subject_inventory_sha256,
          var.provider_authority.kubernetes_rbac_inventory_sha256,
          var.provider_authority.kubernetes_rbac_effective_authority_sha256,
          var.provider_authority.kubernetes_rbac_inventory_receipt_sha256,
          var.provider_authority.provider_project_iam_inventory_receipt_sha256,
          var.provider_authority.provider_effective_authority_graph_receipt_sha256,
          var.provider_authority.provider_authority_adapter_sha256,
          var.provider_authority.provider_state_custody_sha256,
          var.provider_authority.boundary_state_custody_sha256,
          var.provider_authority.protected_observer_inventory_sha256,
          var.provider_authority.protected_node_inventory_sha256,
          var.provider_authority.protected_node_scheduling_labels_sha256,
          var.provider_authority.protected_node_attestation_sha256,
          var.provider_authority.controller_audit_receipt_sha256,
          var.provider_authority.daemonset_inventory_sha256,
          var.provider_authority.daemonset_admission_fence_receipt_sha256,
          var.provider_authority.daemonset_snapshot_ledger_head_sha256,
          var.provider_authority.provisioning_receipt_sha256,
          var.provider_authority.retained_admission_custody_sha256,
          var.provider_authority.workloads_service_account_sha256,
          var.provider_authority.sai10_independent_review_receipt_sha256,
        ] : can(regex("^[a-f0-9]{64}$", digest))
      ]) &&
      var.provider_authority.daemonset_list_resource_version != "" &&
      var.provider_authority.node_lifecycle_mode == "GENERATIONAL_SINGLETON_RETAIN_PREDECESSOR" &&
      var.provider_authority.node_health_mutation.identity_role == "node_health" &&
      var.provider_authority.node_health_mutation.mutable_label_keys == sort(distinct(var.provider_authority.node_health_mutation.mutable_label_keys)) &&
      length(var.provider_authority.node_health_mutation.mutable_taint_keys) > 0 &&
      var.provider_authority.node_health_mutation.mutable_taint_keys == sort(distinct(var.provider_authority.node_health_mutation.mutable_taint_keys)) &&
      !contains(var.provider_authority.node_health_mutation.mutable_label_keys, var.provider_authority.node_selector_key) &&
      !contains(var.provider_authority.node_health_mutation.mutable_taint_keys, var.provider_authority.taint_key) &&
      var.provider_authority.node_health_mutation.allow_unschedulable &&
      can(regex("^[a-f0-9]{40}$", var.provider_authority.accepted_sai10_commit)) &&
      can(regex("^[a-f0-9]{40}$", var.provider_authority.accepted_sai10_tree)) &&
      !startswith(var.provider_authority.accepted_sai10_commit, "1ae009b85") &&
      var.provider_authority.authority_service_account_sha256 != var.provider_authority.workloads_service_account_sha256
      && var.provider_authority.controller_identities == var.controller_identities
      && alltrue([
        for generation, policy in merge(
          var.provider_authority.retained_legacy_boundary_policies,
          var.provider_authority.retained_legacy_workload_policies,
          var.provider_authority.retained_v3_boundary_policies,
          var.provider_authority.retained_v3_workload_policies,
        ) :
        can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation)) &&
        can(regex("^[a-f0-9]{64}$", policy.policy_sha256)) &&
        endswith(generation, substr(policy.policy_sha256, 0, 12)) &&
        sha256(jsonencode(policy.policy_spec)) == policy.policy_sha256 &&
        policy.binding_spec.policyName == policy.name &&
        policy.binding_spec.validationActions == ["Deny"]
      ])
      && sha256(jsonencode({
        retained_legacy_boundary_policies = var.provider_authority.retained_legacy_boundary_policies
        retained_legacy_workload_policies = var.provider_authority.retained_legacy_workload_policies
        retained_v3_boundary_policies     = var.provider_authority.retained_v3_boundary_policies
        retained_v3_workload_policies     = var.provider_authority.retained_v3_workload_policies
      })) == var.provider_authority.retained_admission_custody_sha256
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

variable "legacy_contract_generations" {
  description = "Exact retained v2 contract/NetworkPolicy keys. Successors use disjoint v3 names and selectors."
  type        = set(string)
  validation {
    condition = alltrue([
      for generation in var.legacy_contract_generations : contains(keys(var.contract_generations), generation)
    ])
    error_message = "legacy_contract_generations must be a subset of contract_generations."
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

variable "legacy_trust_generations" {
  description = "Exact retained v2 trust generation keys."
  type        = set(string)
  validation {
    condition = alltrue([
      for generation in var.legacy_trust_generations : contains(keys(var.trust_generations), generation)
    ])
    error_message = "legacy_trust_generations must be a subset of trust_generations."
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

variable "legacy_boundary_generations" {
  description = "Retained overmatching v2 boundary generations; their objects and state addresses remain immutable."
  type        = set(string)
  validation {
    condition = alltrue([
      for generation in var.legacy_boundary_generations : contains(var.boundary_generations, generation)
    ])
    error_message = "legacy_boundary_generations must be a subset of boundary_generations."
  }
}

variable "successor_boundary_generations" {
  description = "Append-only v3 boundary generations mapped to their exact contract generation."
  type = map(object({
    contract_generation = string
    policy_spec_json    = string
    policy_sha256       = string
  }))
  validation {
    condition = length(var.successor_boundary_generations) > 0 && alltrue([
      for generation, value in var.successor_boundary_generations :
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      contains(keys(var.contract_generations), value.contract_generation) &&
      !contains(var.legacy_contract_generations, value.contract_generation) &&
      can(jsondecode(value.policy_spec_json)) &&
      value.policy_sha256 == sha256(jsonencode(jsondecode(value.policy_spec_json))) &&
      endswith(generation, substr(value.policy_sha256, 0, 12))
    ])
    error_message = "Every v3 boundary generation must bind a non-legacy contract generation."
  }
}

variable "workload_policy_generations" {
  description = "Append-only content-bound workload admission generations."
  type        = set(string)
  validation {
    condition = length(var.workload_policy_generations) > 0 && alltrue([
      for generation in var.workload_policy_generations :
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation))
    ])
    error_message = "At least one append-only workload admission generation is required."
  }
}

variable "legacy_workload_policy_generations" {
  description = "Retained v2 workload-policy generation keys."
  type        = set(string)
  validation {
    condition = alltrue([
      for generation in var.legacy_workload_policy_generations : contains(var.workload_policy_generations, generation)
    ])
    error_message = "legacy_workload_policy_generations must be a subset of workload_policy_generations."
  }
}

variable "successor_workload_policy_generations" {
  description = "Append-only v3 workload policies mapped to exact contract and release generations."
  type = map(object({
    contract_generation = string
    release_generation  = string
    policy_spec_json    = string
    policy_sha256       = string
  }))
  validation {
    condition = length(var.successor_workload_policy_generations) > 0 && alltrue([
      for generation, value in var.successor_workload_policy_generations :
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", generation)) &&
      contains(keys(var.contract_generations), value.contract_generation) &&
      !contains(var.legacy_contract_generations, value.contract_generation) &&
      contains(keys(var.release_generations), value.release_generation) &&
      !contains(var.legacy_release_generations, value.release_generation) &&
      can(jsondecode(value.policy_spec_json)) &&
      value.policy_sha256 == sha256(jsonencode(jsondecode(value.policy_spec_json))) &&
      endswith(generation, substr(value.policy_sha256, 0, 12))
    ])
    error_message = "Every v3 workload policy must bind non-legacy contract and release generations."
  }
}

variable "current_workload_policy_generation" {
  description = "Content-bound workload admission generation selected for this release."
  type        = string
  validation {
    condition = (
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.current_workload_policy_generation)) &&
      contains(keys(var.successor_workload_policy_generations), var.current_workload_policy_generation)
    )
    error_message = "current_workload_policy_generation must name a retained generation."
  }
}

variable "current_generation" {
  description = "Contract generation selected by the next workloads rollout."
  type        = string

  validation {
    condition = (
      can(regex("^g[0-9]{14}-[a-f0-9]{12}$", var.current_generation)) &&
      contains(keys(var.contract_generations), var.current_generation) &&
      !contains(var.legacy_contract_generations, var.current_generation)
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
      contains(keys(var.successor_boundary_generations), var.current_boundary_generation)
    )
    error_message = "current_boundary_generation must identify a retained admission boundary."
  }
}

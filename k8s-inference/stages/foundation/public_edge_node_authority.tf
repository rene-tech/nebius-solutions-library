locals {
  public_edge_membership_expected_subject = {
    schema                   = "fs2-serve.nebius.ai/public-edge-membership-terraform-subject/v3"
    project_id               = nonsensitive(var.project_id)
    cluster_id               = var.cluster_id
    node_group_id            = var.public_edge_availability_contract.system_node_group_id
    run_id                   = var.run_id
    expected_node_count      = var.public_edge_availability_contract.system_node_count
    minimum_hostname_domains = var.public_edge_availability_contract.minimum_domains
    maximum_surge_members     = var.public_edge_availability_contract.update_strategy.max_surge
    node_selector_sha256     = sha256(jsonencode(var.public_edge_availability_contract.node_selector))
    kubeconfig_sha256         = filesha256(abspath(var.kubeconfig_path))
  }
}

# This adapter trusts only a source-enrolled Ed25519 issuer. Its production
# registry is intentionally empty until an independently reviewed provider
# membership exporter and signing authority are enrolled. No tfvar can provide
# a key, instance list, controller username, tool path, digest, or PASS result.
data "external" "public_edge_membership_contract" {
  count = local.public_edge_enabled ? 1 : 0

  program = [
    local.public_edge_gate_launcher_path,
    "public-edge-verifier",
    "receipt-contract",
  ]

  query = {
    run_root              = abspath(var.run_root)
    verifier_sha256       = local.public_edge_gate_verifier_sha256
    membership_trust_sha256 = local.public_edge_membership_trust_sha256
    provider_adapter_trust_sha256 = local.public_edge_provider_adapter_trust_sha256
    expected_subject_json = jsonencode(local.public_edge_membership_expected_subject)
  }
}

locals {
  public_edge_membership_authority = local.public_edge_enabled ? {
    verified                            = try(one(data.external.public_edge_membership_contract[*].result.verified) == "true", false)
    payload_sha256                      = try(one(data.external.public_edge_membership_contract[*].result.payload_sha256), "")
    receipt_sha256                      = try(one(data.external.public_edge_membership_contract[*].result.receipt_sha256), "")
    evidence_sha256                     = try(one(data.external.public_edge_membership_contract[*].result.evidence_sha256), "")
    node_group_resource_version         = try(one(data.external.public_edge_membership_contract[*].result.node_group_resource_version), "")
    provider_member_instance_ids        = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.provider_member_instance_ids_json)), [])
    epoch_id                            = try(one(data.external.public_edge_membership_contract[*].result.epoch_id), "")
    epoch_sequence                      = try(tonumber(one(data.external.public_edge_membership_contract[*].result.epoch_sequence)), 0)
    phase                               = try(one(data.external.public_edge_membership_contract[*].result.phase), "")
    predecessor_payload_sha256          = try(one(data.external.public_edge_membership_contract[*].result.predecessor_payload_sha256), "")
    serving_member_instance_ids         = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.serving_member_instance_ids_json)), [])
    joining_member_instance_ids         = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.joining_member_instance_ids_json)), [])
    retiring_member_instance_ids        = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.retiring_member_instance_ids_json)), [])
    admitted_member_instance_ids        = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.admitted_member_instance_ids_json)), [])
    provider_observer                   = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.provider_observer_json)), null)
  } : {
    verified                            = false
    payload_sha256                      = ""
    receipt_sha256                      = ""
    evidence_sha256                     = ""
    node_group_resource_version         = ""
    provider_member_instance_ids        = []
    epoch_id                            = ""
    epoch_sequence                      = 0
    phase                               = ""
    predecessor_payload_sha256          = ""
    serving_member_instance_ids         = []
    joining_member_instance_ids         = []
    retiring_member_instance_ids        = []
    admitted_member_instance_ids        = []
    provider_observer                   = null
  }

  public_edge_protected_node_label_keys = sort(keys(var.public_edge_availability_contract.node_selector))
  public_edge_protected_labels_unchanged_cel = join(" && ", [
    for key in local.public_edge_protected_node_label_keys :
    "((${jsonencode(key)} in object.metadata.labels) == (${jsonencode(key)} in oldObject.metadata.labels) && (!(${jsonencode(key)} in object.metadata.labels) || object.metadata.labels[${jsonencode(key)}] == oldObject.metadata.labels[${jsonencode(key)}]))"
  ])
  public_edge_claims_protected_label_cel = join(" || ", [
    for key in local.public_edge_protected_node_label_keys :
    "${jsonencode(key)} in object.metadata.labels"
  ])
  public_edge_exact_selector_cel = join(" && ", [
    for key in local.public_edge_protected_node_label_keys :
    "${jsonencode(key)} in object.metadata.labels && object.metadata.labels[${jsonencode(key)}] == ${jsonencode(var.public_edge_availability_contract.node_selector[key])}"
  ])
  public_edge_joining_labels_monotonic_cel = join(" && ", [
    for key in local.public_edge_protected_node_label_keys :
    "((${jsonencode(key)} in oldObject.metadata.labels && ${jsonencode(key)} in object.metadata.labels && object.metadata.labels[${jsonencode(key)}] == oldObject.metadata.labels[${jsonencode(key)}]) || (!(${jsonencode(key)} in oldObject.metadata.labels) && (!(${jsonencode(key)} in object.metadata.labels) || object.metadata.labels[${jsonencode(key)}] == ${jsonencode(var.public_edge_availability_contract.node_selector[key])})))"
  ])
  public_edge_joining_labels_valid_cel = join(" && ", [
    for key in local.public_edge_protected_node_label_keys :
    "(!(${jsonencode(key)} in object.metadata.labels) || object.metadata.labels[${jsonencode(key)}] == ${jsonencode(var.public_edge_availability_contract.node_selector[key])})"
  ])
  public_edge_joining_provider_monotonic_cel = "((has(oldObject.spec.providerID) && has(object.spec.providerID) && object.spec.providerID == oldObject.spec.providerID) || (!has(oldObject.spec.providerID) && (!has(object.spec.providerID) || object.spec.providerID == 'nebius://' + object.metadata.name)))"
}

data "kubernetes_resources" "public_edge_existing_node_authority" {
  count          = local.public_edge_enabled ? 1 : 0
  api_version    = "admissionregistration.k8s.io/v1"
  kind           = "ValidatingAdmissionPolicy"
  field_selector = "metadata.name=fs2-public-edge-node-authority"
}

locals {
  public_edge_existing_node_authority = local.public_edge_enabled ? try(
    one(data.kubernetes_resources.public_edge_existing_node_authority[0].objects),
    null,
  ) : null
  public_edge_existing_membership_payload_sha256 = try(
    local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/membership-payload-sha256"],
    "",
  )
  public_edge_existing_epoch_sequence = try(
    tonumber(local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/membership-epoch-sequence"]),
    0,
  )
  public_edge_existing_phase = try(
    local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/membership-phase"],
    "",
  )
  public_edge_existing_serving_member_instance_ids = try(
    jsondecode(local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/serving-member-instance-ids"]),
    [],
  )
  public_edge_existing_joining_member_instance_ids = try(
    jsondecode(local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/joining-member-instance-ids"]),
    [],
  )
  public_edge_existing_retiring_member_instance_ids = try(
    jsondecode(local.public_edge_existing_node_authority.metadata.annotations["fs2.nebius.ai/retiring-member-instance-ids"]),
    [],
  )
  public_edge_predecessor_phase = local.public_edge_existing_node_authority == null ? "genesis" : local.public_edge_existing_phase
  public_edge_predecessor_serving_member_instance_ids = local.public_edge_existing_node_authority == null ? [] : local.public_edge_existing_serving_member_instance_ids
  public_edge_predecessor_joining_member_instance_ids = local.public_edge_existing_node_authority == null ? [] : local.public_edge_existing_joining_member_instance_ids
  public_edge_predecessor_retiring_member_instance_ids = local.public_edge_existing_node_authority == null ? [] : local.public_edge_existing_retiring_member_instance_ids
}

locals {
  # This small, stable policy is the apply-time compare-and-swap anchor for the
  # dynamic signed membership policy below. It compares every successor to the
  # API server's actual oldObject, so two saved plans derived from the same
  # epoch cannot both commit. It also makes both policies and both bindings
  # non-deletable through ordinary Kubernetes admission. An independently
  # reviewed break-glass control-plane procedure is required to retire them.
  public_edge_node_authority_cas_policy_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-public-edge-node-authority-cas"
      annotations = {
        "fs2.nebius.ai/contract" = "apply-time-membership-epoch-cas/v1"
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = ["admissionregistration.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources = [
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
          ]
          scope = "Cluster"
        }]
      }
      validations = [
        {
          expression = "request.operation == 'CREATE' || !(oldObject.metadata.name in ['fs2-public-edge-node-authority-cas', 'fs2-public-edge-node-authority-cas-binding'])"
          message    = "The public-edge epoch CAS policy and binding are immutable after activation."
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'DELETE' || oldObject.metadata.name != 'fs2-public-edge-node-authority'"
          message    = "The protected public-edge Node-authority policy cannot be deleted."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicybindings' || request.operation != 'CREATE' || object.metadata.name != 'fs2-public-edge-node-authority' || (object.spec.policyName == 'fs2-public-edge-node-authority' && object.spec.validationActions == ['Deny'])"
          message    = "The public-edge Node-authority binding must activate its exact policy with Deny."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicybindings' || request.operation == 'CREATE' || oldObject.metadata.name != 'fs2-public-edge-node-authority'"
          message    = "The active public-edge Node-authority binding cannot be updated or deleted."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicies' || request.operation != 'CREATE' || object.metadata.name != 'fs2-public-edge-node-authority' || (object.metadata.annotations['fs2.nebius.ai/membership-epoch-sequence'] == '1' && object.metadata.annotations['fs2.nebius.ai/predecessor-payload-sha256'] == '${strrep("0", 64)}' && object.metadata.annotations['fs2.nebius.ai/predecessor-phase'] == 'genesis' && object.metadata.annotations['fs2.nebius.ai/predecessor-serving-member-instance-ids'] == '[]' && object.metadata.annotations['fs2.nebius.ai/predecessor-joining-member-instance-ids'] == '[]' && object.metadata.annotations['fs2.nebius.ai/predecessor-retiring-member-instance-ids'] == '[]')"
          message    = "A public-edge Node-authority policy can be created only as the exact genesis epoch."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicies' || request.operation != 'UPDATE' || object.metadata.name != 'fs2-public-edge-node-authority' || (int(object.metadata.annotations['fs2.nebius.ai/membership-epoch-sequence']) == int(oldObject.metadata.annotations['fs2.nebius.ai/membership-epoch-sequence']) + 1 && object.metadata.annotations['fs2.nebius.ai/predecessor-payload-sha256'] == oldObject.metadata.annotations['fs2.nebius.ai/membership-payload-sha256'] && object.metadata.annotations['fs2.nebius.ai/predecessor-phase'] == oldObject.metadata.annotations['fs2.nebius.ai/membership-phase'] && object.metadata.annotations['fs2.nebius.ai/predecessor-serving-member-instance-ids'] == oldObject.metadata.annotations['fs2.nebius.ai/serving-member-instance-ids'] && object.metadata.annotations['fs2.nebius.ai/predecessor-joining-member-instance-ids'] == oldObject.metadata.annotations['fs2.nebius.ai/joining-member-instance-ids'] && object.metadata.annotations['fs2.nebius.ai/predecessor-retiring-member-instance-ids'] == oldObject.metadata.annotations['fs2.nebius.ai/retiring-member-instance-ids'])"
          message    = "The public-edge Node-authority successor must be the exact next sequence and compare-and-swap every predecessor identity and member set against current API-server oldObject. Re-plan from the installed epoch."
          reason     = "Forbidden"
        },
      ]
    }
  }
  public_edge_node_authority_cas_binding_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-public-edge-node-authority-cas-binding"
    }
    spec = {
      policyName        = "fs2-public-edge-node-authority-cas"
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
    }
  }
  public_edge_node_authority_policy_annotations = {
        "fs2.nebius.ai/membership-payload-sha256"  = local.public_edge_membership_authority.payload_sha256
        "fs2.nebius.ai/membership-receipt-sha256"  = local.public_edge_membership_authority.receipt_sha256
        "fs2.nebius.ai/membership-epoch"            = local.public_edge_membership_authority.epoch_id
        "fs2.nebius.ai/membership-epoch-sequence"   = tostring(local.public_edge_membership_authority.epoch_sequence)
        "fs2.nebius.ai/membership-phase"            = local.public_edge_membership_authority.phase
        "fs2.nebius.ai/predecessor-payload-sha256"  = local.public_edge_membership_authority.predecessor_payload_sha256
        "fs2.nebius.ai/predecessor-phase"            = local.public_edge_predecessor_phase
        "fs2.nebius.ai/predecessor-serving-member-instance-ids" = jsonencode(local.public_edge_predecessor_serving_member_instance_ids)
        "fs2.nebius.ai/predecessor-joining-member-instance-ids" = jsonencode(local.public_edge_predecessor_joining_member_instance_ids)
        "fs2.nebius.ai/predecessor-retiring-member-instance-ids" = jsonencode(local.public_edge_predecessor_retiring_member_instance_ids)
        "fs2.nebius.ai/serving-member-instance-ids" = jsonencode(local.public_edge_membership_authority.serving_member_instance_ids)
        "fs2.nebius.ai/joining-member-instance-ids" = jsonencode(local.public_edge_membership_authority.joining_member_instance_ids)
        "fs2.nebius.ai/retiring-member-instance-ids" = jsonencode(local.public_edge_membership_authority.retiring_member_instance_ids)
        "fs2.nebius.ai/provider-adapter-sha256"     = try(local.public_edge_membership_authority.provider_observer.adapter_sha256, "")
  }
  public_edge_node_authority_policy_spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["nodes"]
          scope       = "Cluster"
        }]
      }
      validations = [
        {
          expression = format(
            "request.operation != 'UPDATE' || (!(object.metadata.name in %s) && !(oldObject.metadata.name in %s)) || (((%s) && object.spec.providerID == oldObject.spec.providerID) || (object.metadata.name in %s && (%s) && (%s)))",
            jsonencode(local.public_edge_membership_authority.admitted_member_instance_ids),
            jsonencode(local.public_edge_membership_authority.admitted_member_instance_ids),
            local.public_edge_protected_labels_unchanged_cel,
            jsonencode(local.public_edge_membership_authority.joining_member_instance_ids),
            local.public_edge_joining_labels_monotonic_cel,
            local.public_edge_joining_provider_monotonic_cel,
          )
          message = "Protected public-edge Node identity is immutable; a signed joining member may only initialize an absent field to its exact value."
          reason  = "Forbidden"
        },
        {
          expression = format(
            "(object.metadata.name in %s && (!has(object.spec.providerID) || object.spec.providerID == 'nebius://' + object.metadata.name) && (%s)) || (object.metadata.name in %s && has(object.spec.providerID) && object.spec.providerID == 'nebius://' + object.metadata.name && (%s)) || (!(object.metadata.name in %s) && !(%s))",
            jsonencode(local.public_edge_membership_authority.joining_member_instance_ids),
            local.public_edge_joining_labels_valid_cel,
            jsonencode(sort(tolist(setsubtract(toset(local.public_edge_membership_authority.admitted_member_instance_ids), toset(local.public_edge_membership_authority.joining_member_instance_ids))))),
            local.public_edge_exact_selector_cel,
            jsonencode(local.public_edge_membership_authority.admitted_member_instance_ids),
            local.public_edge_claims_protected_label_cel,
          )
          message = "Public-edge labels are reserved for the signed epoch; identity tuples cannot authorize or extend membership."
          reason  = "Forbidden"
        },
      ]
  }
  # Platform Security's persistent parameter object contains these exact maps,
  # their digest, the external signature evidence and the one accepted actor.
  # Admission compares the submitted policy to that live parameter; annotations
  # alone are never treated as proof.
  public_edge_node_authority_policy_content_sha256 = sha256(jsonencode({
    annotations = local.public_edge_node_authority_policy_annotations
    spec        = local.public_edge_node_authority_policy_spec
  }))
  public_edge_node_authority_policy_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-public-edge-node-authority"
      annotations = merge(local.public_edge_node_authority_policy_annotations, {
        "fs2.nebius.ai/policy-content-sha256" = local.public_edge_node_authority_policy_content_sha256
      })
    }
    spec = local.public_edge_node_authority_policy_spec
  }
  public_edge_node_authority_binding_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-public-edge-node-authority"
    }
    spec = {
      policyName        = "fs2-public-edge-node-authority"
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
    }
  }
  public_edge_node_authority_policy_sha256  = sha256(jsonencode(local.public_edge_node_authority_policy_manifest))
  public_edge_node_authority_binding_sha256 = sha256(jsonencode(local.public_edge_node_authority_binding_manifest))
  public_edge_node_authority_cas_policy_sha256  = sha256(jsonencode(local.public_edge_node_authority_cas_policy_manifest))
  public_edge_node_authority_cas_binding_sha256 = sha256(jsonencode(local.public_edge_node_authority_cas_binding_manifest))
}

resource "kubernetes_manifest" "public_edge_node_authority_cas_policy" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_cas_policy_manifest

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.public_edge_cas_bootstrap_exact && local.public_edge_node_authority_approval_exact
      error_message = "The live security-owned admission parameter must contain the exact signed Node-authority spec, annotations, actor, membership receipt, RBAC review and impersonation review for this epoch."
    }

    precondition {
      condition     = local.public_edge_cas_bootstrap_exact && local.public_edge_node_authority_approval_exact
      error_message = "Public mode requires the exact externally installed Platform Security CAS bootstrap plus a signed exact-policy approval parameter bound to the reviewed actor/RBAC/impersonation closure before Terraform may create or update Node authority."
    }
  }
}

resource "kubernetes_manifest" "public_edge_node_authority_cas_binding" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_cas_binding_manifest

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.public_edge_node_authority_cas_policy]
}

# A fresh fence cannot make mutable Node labels authoritative after it exits.
# This fail-closed admission policy continuously freezes the signed member set,
# providerID join, and exact scheduler labels. Ordinary kubelet/status updates
# remain possible when those protected values do not change. No username, UID,
# group, authentication extra, or impersonation assertion grants an exception.
# A signed prepare epoch keeps all old serving Nodes while admitting bounded
# surge IDs; cutover keeps retiring IDs admitted through quiescence/rollback.
resource "kubernetes_manifest" "public_edge_node_authority_policy" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_policy_manifest

  depends_on = [kubernetes_manifest.public_edge_node_authority_cas_binding]

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.public_edge_cas_bootstrap_exact && local.public_edge_node_authority_approval_exact
      error_message = "The live security-owned admission parameter must contain the exact signed Node-authority spec, annotations, actor, membership receipt, RBAC review and impersonation review for this epoch."
    }

    precondition {
      condition = (
        local.public_edge_membership_authority.verified &&
        length(local.public_edge_membership_authority.serving_member_instance_ids) == var.public_edge_availability_contract.system_node_count &&
        length(toset(local.public_edge_membership_authority.admitted_member_instance_ids)) == length(local.public_edge_membership_authority.admitted_member_instance_ids) &&
        toset(local.public_edge_membership_authority.admitted_member_instance_ids) == setunion(toset(local.public_edge_membership_authority.serving_member_instance_ids), toset(local.public_edge_membership_authority.joining_member_instance_ids), toset(local.public_edge_membership_authority.retiring_member_instance_ids)) &&
        toset(local.public_edge_membership_authority.provider_member_instance_ids) == toset(local.public_edge_membership_authority.admitted_member_instance_ids) &&
        length(local.public_edge_membership_authority.joining_member_instance_ids) <= var.public_edge_availability_contract.update_strategy.max_surge &&
        length(local.public_edge_membership_authority.retiring_member_instance_ids) <= var.public_edge_availability_contract.update_strategy.max_surge &&
        contains(["stable", "prepare", "cutover"], local.public_edge_membership_authority.phase) &&
        local.public_edge_membership_authority.epoch_sequence >= 1 &&
        can(regex("^[a-f0-9]{64}$", local.public_edge_membership_authority.epoch_id)) &&
        can(regex("^[a-f0-9]{64}$", local.public_edge_membership_authority.predecessor_payload_sha256)) &&
        (
          (local.public_edge_membership_authority.phase == "stable" && length(local.public_edge_membership_authority.joining_member_instance_ids) == 0 && length(local.public_edge_membership_authority.retiring_member_instance_ids) == 0) ||
          (local.public_edge_membership_authority.phase == "prepare" && length(local.public_edge_membership_authority.joining_member_instance_ids) > 0 && length(local.public_edge_membership_authority.retiring_member_instance_ids) == 0) ||
          (local.public_edge_membership_authority.phase == "cutover" && length(local.public_edge_membership_authority.joining_member_instance_ids) == 0 && length(local.public_edge_membership_authority.retiring_member_instance_ids) > 0)
        ) &&
        can(regex("^[1-9][0-9]*$", local.public_edge_membership_authority.node_group_resource_version)) &&
        can(regex("^[a-f0-9]{64}$", local.public_edge_membership_authority.provider_observer.adapter_sha256))
      )
      error_message = "Public mode requires a source-trusted exact-subject provider membership epoch with an exact stable/prepare/cutover set before installing continuous Node authority enforcement."
    }

    precondition {
      condition = (
        (
          local.public_edge_existing_node_authority == null &&
          local.public_edge_membership_authority.epoch_sequence == 1 &&
          local.public_edge_membership_authority.phase == "stable" &&
          local.public_edge_membership_authority.predecessor_payload_sha256 == strrep("0", 64)
        ) ||
        (
          local.public_edge_existing_node_authority != null &&
          local.public_edge_membership_authority.epoch_sequence == local.public_edge_existing_epoch_sequence + 1 &&
          local.public_edge_membership_authority.predecessor_payload_sha256 == local.public_edge_existing_membership_payload_sha256 &&
          (
            (
              local.public_edge_existing_phase == "stable" &&
              local.public_edge_membership_authority.phase == "stable" &&
              toset(local.public_edge_membership_authority.serving_member_instance_ids) == toset(local.public_edge_existing_serving_member_instance_ids)
            ) ||
            (
              local.public_edge_existing_phase == "stable" &&
              local.public_edge_membership_authority.phase == "prepare" &&
              toset(local.public_edge_membership_authority.serving_member_instance_ids) == toset(local.public_edge_existing_serving_member_instance_ids)
            ) ||
            (
              local.public_edge_existing_phase == "prepare" &&
              local.public_edge_membership_authority.phase == "stable" &&
              toset(local.public_edge_membership_authority.serving_member_instance_ids) == toset(local.public_edge_existing_serving_member_instance_ids)
            ) ||
            (
              local.public_edge_existing_phase == "prepare" &&
              local.public_edge_membership_authority.phase == "cutover" &&
              length(setsubtract(toset(local.public_edge_membership_authority.retiring_member_instance_ids), toset(local.public_edge_existing_serving_member_instance_ids))) == 0 &&
              toset(local.public_edge_membership_authority.serving_member_instance_ids) == setunion(
                setsubtract(toset(local.public_edge_existing_serving_member_instance_ids), toset(local.public_edge_membership_authority.retiring_member_instance_ids)),
                toset(local.public_edge_existing_joining_member_instance_ids),
              )
            ) ||
            (
              local.public_edge_existing_phase == "cutover" &&
              local.public_edge_membership_authority.phase == "stable" &&
              toset(local.public_edge_membership_authority.serving_member_instance_ids) == toset(local.public_edge_existing_serving_member_instance_ids)
            ) ||
            (
              local.public_edge_existing_phase == "cutover" &&
              local.public_edge_membership_authority.phase == "cutover" &&
              setunion(
                toset(local.public_edge_membership_authority.serving_member_instance_ids),
                toset(local.public_edge_membership_authority.retiring_member_instance_ids),
              ) == setunion(
                toset(local.public_edge_existing_serving_member_instance_ids),
                toset(local.public_edge_existing_retiring_member_instance_ids),
              )
            )
          )
        )
      )
      error_message = "The signed membership epoch must be genesis-stable or the exact next predecessor-bound stable/prepare/cutover transition; every prepare retains the prior serving set, cutover promotes only its signed joining set, and rollback/finalization remains within the installed admitted union."
    }
  }
}

resource "kubernetes_manifest" "public_edge_node_authority_binding" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_binding_manifest

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.public_edge_node_authority_policy]
}

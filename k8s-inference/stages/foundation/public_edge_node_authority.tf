locals {
  public_edge_membership_expected_subject = {
    schema                   = "fs2-serve.nebius.ai/public-edge-membership-terraform-subject/v2"
    project_id               = nonsensitive(var.project_id)
    cluster_id               = var.cluster_id
    node_group_id            = var.public_edge_availability_contract.system_node_group_id
    run_id                   = var.run_id
    expected_node_count      = var.public_edge_availability_contract.system_node_count
    minimum_hostname_domains = var.public_edge_availability_contract.minimum_domains
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
    local.public_edge_gate_verifier_path,
    local.public_edge_gate_verifier_sha256,
    "--receipt-contract",
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
    member_instance_ids                 = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.member_instance_ids_json)), [])
    kubernetes_node_controller          = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.kubernetes_node_controller_json)), null)
    provider_observer                   = try(jsondecode(one(data.external.public_edge_membership_contract[*].result.provider_observer_json)), null)
  } : {
    verified                            = false
    payload_sha256                      = ""
    receipt_sha256                      = ""
    evidence_sha256                     = ""
    node_group_resource_version         = ""
    member_instance_ids                 = []
    kubernetes_node_controller          = null
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
  public_edge_controller_groups_cel = local.public_edge_enabled ? format(
    "request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s)",
    length(local.public_edge_membership_authority.kubernetes_node_controller.groups),
    jsonencode(local.public_edge_membership_authority.kubernetes_node_controller.groups),
  ) : "false"
  public_edge_controller_extra_cel = local.public_edge_enabled ? join(" && ", concat(
    [format(
      "request.userInfo.extra.size() == %d",
      length(local.public_edge_membership_authority.kubernetes_node_controller.extra),
    )],
    [for key in sort(keys(local.public_edge_membership_authority.kubernetes_node_controller.extra)) : format(
      "%s in request.userInfo.extra && request.userInfo.extra[%s].size() == %d && request.userInfo.extra[%s].all(value, value in %s)",
      jsonencode(key),
      jsonencode(key),
      length(local.public_edge_membership_authority.kubernetes_node_controller.extra[key]),
      jsonencode(key),
      jsonencode(local.public_edge_membership_authority.kubernetes_node_controller.extra[key]),
    )],
  )) : "false"
  public_edge_controller_identity_cel = local.public_edge_enabled ? format(
    "request.userInfo.username == %s && request.userInfo.uid == %s && (%s) && (%s)",
    jsonencode(local.public_edge_membership_authority.kubernetes_node_controller.username),
    jsonencode(local.public_edge_membership_authority.kubernetes_node_controller.uid),
    local.public_edge_controller_groups_cel,
    local.public_edge_controller_extra_cel,
  ) : "false"
}

locals {
  public_edge_node_authority_policy_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-public-edge-node-authority"
      annotations = {
        "fs2.nebius.ai/membership-payload-sha256"  = local.public_edge_membership_authority.payload_sha256
        "fs2.nebius.ai/membership-receipt-sha256"  = local.public_edge_membership_authority.receipt_sha256
        "fs2.nebius.ai/controller-identity-sha256" = sha256(jsonencode(local.public_edge_membership_authority.kubernetes_node_controller))
        "fs2.nebius.ai/impersonation-review-sha256" = try(local.public_edge_membership_authority.kubernetes_node_controller.impersonation_review_sha256, "")
        "fs2.nebius.ai/provider-adapter-sha256"     = try(local.public_edge_membership_authority.provider_observer.adapter_sha256, "")
      }
    }
    spec = {
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
            "request.operation != 'UPDATE' || (%s) || ((%s) && object.spec.providerID == oldObject.spec.providerID)",
            local.public_edge_controller_identity_cel,
            local.public_edge_protected_labels_unchanged_cel,
          )
          message = "Only the signed non-impersonable managed-node controller identity may alter public-edge membership labels or providerID."
          reason  = "Forbidden"
        },
        {
          expression = format(
            "!((%s) || object.metadata.name in %s) || (object.metadata.name in %s && has(object.spec.providerID) && object.spec.providerID == 'nebius://' + object.metadata.name && (%s) && (request.operation != 'CREATE' || (%s)))",
            local.public_edge_claims_protected_label_cel,
            jsonencode(local.public_edge_membership_authority.member_instance_ids),
            jsonencode(local.public_edge_membership_authority.member_instance_ids),
            local.public_edge_exact_selector_cel,
            local.public_edge_controller_identity_cel,
          )
          message = "Public-edge labels are reserved for the exact signed NodeGroup member set, providerID join, and full controller authentication tuple."
          reason  = "Forbidden"
        },
      ]
    }
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
}

# A fresh fence cannot make mutable Node labels authoritative after it exits.
# This fail-closed admission policy continuously freezes the signed member set,
# providerID join, and exact scheduler labels. Ordinary kubelet/status updates
# remain possible when those protected values do not change. New member Nodes
# may be introduced only by the controller identity authenticated in the signed
# provider receipt; membership transitions require a newly signed receipt and
# reviewed policy update before the provider rollout.
resource "kubernetes_manifest" "public_edge_node_authority_policy" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_policy_manifest

  lifecycle {
    precondition {
      condition = (
        local.public_edge_membership_authority.verified &&
        length(local.public_edge_membership_authority.member_instance_ids) == var.public_edge_availability_contract.system_node_count &&
        length(toset(local.public_edge_membership_authority.member_instance_ids)) == length(local.public_edge_membership_authority.member_instance_ids) &&
        can(regex("^[1-9][0-9]*$", local.public_edge_membership_authority.node_group_resource_version)) &&
        length(local.public_edge_membership_authority.kubernetes_node_controller.username) >= 3 &&
        length(local.public_edge_membership_authority.kubernetes_node_controller.uid) > 0 &&
        length(local.public_edge_membership_authority.kubernetes_node_controller.groups) > 0 &&
        length(local.public_edge_membership_authority.kubernetes_node_controller.extra) > 0 &&
        local.public_edge_membership_authority.kubernetes_node_controller.impersonation_prohibited &&
        can(regex("^[a-f0-9]{64}$", local.public_edge_membership_authority.kubernetes_node_controller.impersonation_review_sha256)) &&
        can(regex("^[a-f0-9]{64}$", local.public_edge_membership_authority.provider_observer.adapter_sha256))
      )
      error_message = "Public mode requires a source-trusted exact-subject provider membership receipt before installing continuous Node authority enforcement."
    }
  }
}

resource "kubernetes_manifest" "public_edge_node_authority_binding" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = local.public_edge_node_authority_binding_manifest

  depends_on = [kubernetes_manifest.public_edge_node_authority_policy]
}

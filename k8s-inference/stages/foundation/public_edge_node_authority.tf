locals {
  public_edge_membership_expected_subject = {
    schema                   = "fs2-serve.nebius.ai/public-edge-membership-terraform-subject/v1"
    project_id               = nonsensitive(var.project_id)
    cluster_id               = var.cluster_id
    node_group_id            = var.public_edge_availability_contract.system_node_group_id
    run_id                   = var.run_id
    expected_node_count      = var.public_edge_availability_contract.system_node_count
    minimum_hostname_domains = var.public_edge_availability_contract.minimum_domains
    node_selector_sha256     = sha256(jsonencode(var.public_edge_availability_contract.node_selector))
  }
}

# This adapter trusts only a source-enrolled Ed25519 issuer. Its production
# registry is intentionally empty until an independently reviewed provider
# membership exporter and signing authority are enrolled. No tfvar can provide
# a key, instance list, controller username, tool path, digest, or PASS result.
data "external" "public_edge_membership_contract" {
  count = local.public_edge_enabled ? 1 : 0

  program = [
    "/usr/bin/python3",
    "-I",
    "-B",
    "${path.module}/scripts/verify-public-edge-node-eligibility.py",
    "--receipt-contract",
  ]

  query = {
    run_root             = abspath(var.run_root)
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
    kubernetes_node_controller_username = try(one(data.external.public_edge_membership_contract[*].result.kubernetes_node_controller_username), "")
  } : {
    verified                            = false
    payload_sha256                      = ""
    receipt_sha256                      = ""
    evidence_sha256                     = ""
    node_group_resource_version         = ""
    member_instance_ids                 = []
    kubernetes_node_controller_username = ""
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

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-public-edge-node-authority"
      annotations = {
        "fs2.nebius.ai/membership-payload-sha256" = local.public_edge_membership_authority.payload_sha256
        "fs2.nebius.ai/membership-receipt-sha256" = local.public_edge_membership_authority.receipt_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
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
            "request.operation != 'UPDATE' || request.userInfo.username == %s || ((%s) && object.spec.providerID == oldObject.spec.providerID)",
            jsonencode(local.public_edge_membership_authority.kubernetes_node_controller_username),
            local.public_edge_protected_labels_unchanged_cel,
          )
          message = "Only the signed managed-node controller may alter public-edge membership labels or providerID."
          reason  = "Forbidden"
        },
        {
          expression = format(
            "!((%s) || object.metadata.name in %s) || (object.metadata.name in %s && has(object.spec.providerID) && object.spec.providerID == 'nebius://' + object.metadata.name && (%s) && (request.operation != 'CREATE' || request.userInfo.username == %s))",
            local.public_edge_claims_protected_label_cel,
            jsonencode(local.public_edge_membership_authority.member_instance_ids),
            jsonencode(local.public_edge_membership_authority.member_instance_ids),
            local.public_edge_exact_selector_cel,
            jsonencode(local.public_edge_membership_authority.kubernetes_node_controller_username),
          )
          message = "Public-edge labels are reserved for the exact signed NodeGroup member set and providerID join."
          reason  = "Forbidden"
        },
      ]
    }
  }

  lifecycle {
    precondition {
      condition = (
        local.public_edge_membership_authority.verified &&
        length(local.public_edge_membership_authority.member_instance_ids) == var.public_edge_availability_contract.system_node_count &&
        length(toset(local.public_edge_membership_authority.member_instance_ids)) == length(local.public_edge_membership_authority.member_instance_ids) &&
        can(regex("^[1-9][0-9]*$", local.public_edge_membership_authority.node_group_resource_version)) &&
        length(local.public_edge_membership_authority.kubernetes_node_controller_username) >= 3
      )
      error_message = "Public mode requires a source-trusted exact-subject provider membership receipt before installing continuous Node authority enforcement."
    }
  }
}

resource "kubernetes_manifest" "public_edge_node_authority_binding" {
  count = local.public_edge_enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-public-edge-node-authority"
    }
    spec = {
      policyName        = "fs2-public-edge-node-authority"
      validationActions = ["Deny"]
      matchResources    = {}
    }
  }

  depends_on = [kubernetes_manifest.public_edge_node_authority_policy]
}

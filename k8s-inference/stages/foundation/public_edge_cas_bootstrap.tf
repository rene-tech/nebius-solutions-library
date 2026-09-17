locals {
  public_edge_cas_bootstrap_registry = jsondecode(file("${path.module}/trusted-public-edge-cas-bootstrap-authorities.json"))
  public_edge_cas_bootstrap_authority = try(one(local.public_edge_cas_bootstrap_registry.authorities), null)
  public_edge_required_identity_paths = [
    "anonymous",
    "authentication-webhook",
    "bootstrap-token",
    "client-certificate",
    "csr-approval",
    "csr-signing",
    "direct-user",
    "impersonated-group",
    "impersonated-uid",
    "impersonated-user",
    "impersonated-userextra",
    "kubelet-client-certificate",
    "node-credential",
    "oidc",
    "provider-control-plane",
    "requestheader-front-proxy",
    "service-account-token",
    "static-token",
  ]
  public_edge_cas_bootstrap_enrolled = (
    local.public_edge_cas_bootstrap_registry.schema == "fs2-serve.nebius.ai/trusted-public-edge-cas-bootstrap-authorities/v3" &&
    local.public_edge_cas_bootstrap_authority != null &&
    try(keys(local.public_edge_cas_bootstrap_authority) == sort([
      "approval_api_version",
      "approval_kind",
      "approval_name",
      "approval_resource",
      "extra",
      "groups",
      "id",
      "impersonation_review_sha256",
      "rbac_review_sha256",
      "uid",
      "username",
    ]), false) &&
    try(can(regex("^[a-z][a-z0-9-]{7,127}$", local.public_edge_cas_bootstrap_authority.id)), false) &&
    try(length(local.public_edge_cas_bootstrap_authority.username) > 0, false) &&
    try(length(local.public_edge_cas_bootstrap_authority.uid) > 0, false) &&
    try(local.public_edge_cas_bootstrap_authority.groups == sort(distinct(local.public_edge_cas_bootstrap_authority.groups)), false) &&
    try(can(regex("^[a-f0-9]{64}$", local.public_edge_cas_bootstrap_authority.impersonation_review_sha256)), false) &&
    try(local.public_edge_cas_bootstrap_authority.impersonation_review_sha256 != strrep("0", 64), false) &&
    try(can(regex("^[a-f0-9]{64}$", local.public_edge_cas_bootstrap_authority.rbac_review_sha256)), false) &&
    try(local.public_edge_cas_bootstrap_authority.rbac_review_sha256 != strrep("0", 64), false) &&
    try(can(regex("^[a-z0-9.-]+/v[0-9]+[a-z0-9]*$", local.public_edge_cas_bootstrap_authority.approval_api_version)), false) &&
    try(can(regex("^[A-Z][A-Za-z0-9]{2,127}$", local.public_edge_cas_bootstrap_authority.approval_kind)), false) &&
    try(can(regex("^[a-z][a-z0-9.-]{2,127}$", local.public_edge_cas_bootstrap_authority.approval_resource)), false) &&
    try(local.public_edge_cas_bootstrap_authority.approval_name == "fs2-public-edge-node-authority-approval", false)
  )
  public_edge_cas_bootstrap_creator_cel = local.public_edge_cas_bootstrap_enrolled ? format(
    "request.userInfo.username == %s && request.userInfo.uid == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && %s.all(group, group in request.userInfo.groups) && request.userInfo.extra == %s",
    jsonencode(local.public_edge_cas_bootstrap_authority.username),
    jsonencode(local.public_edge_cas_bootstrap_authority.uid),
    length(local.public_edge_cas_bootstrap_authority.groups),
    jsonencode(local.public_edge_cas_bootstrap_authority.groups),
    jsonencode(local.public_edge_cas_bootstrap_authority.groups),
    jsonencode(local.public_edge_cas_bootstrap_authority.extra),
  ) : "false"
  # This VAP expresses exact policy semantics but is not trusted to protect
  # admissionregistration objects from deletion. The external provider-IAM +
  # API-server boundary enrolled above is the preventive authority.
  public_edge_cas_bootstrap_policy_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-public-edge-cas-bootstrap"
      annotations = {
        "fs2.nebius.ai/authority-id"                = try(local.public_edge_cas_bootstrap_authority.id, "unenrolled")
        "fs2.nebius.ai/contract"                    = "security-owned-cas-bootstrap/v1"
        "fs2.nebius.ai/impersonation-review-sha256" = try(local.public_edge_cas_bootstrap_authority.impersonation_review_sha256, strrep("0", 64))
        "fs2.nebius.ai/rbac-review-sha256"          = try(local.public_edge_cas_bootstrap_authority.rbac_review_sha256, strrep("0", 64))
      }
    }
    spec = {
      failurePolicy = "Fail"
      paramKind = {
        apiVersion = try(local.public_edge_cas_bootstrap_authority.approval_api_version, "security.fs2.nebius.ai/v1")
        kind       = try(local.public_edge_cas_bootstrap_authority.approval_kind, "PublicEdgeNodeAuthorityApproval")
      }
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
          expression = "request.operation == 'CREATE' || !(oldObject.metadata.name in ['fs2-public-edge-cas-bootstrap', 'fs2-public-edge-cas-bootstrap-binding'])"
          message    = "The external Platform Security CAS bootstrap policy and binding are immutable."
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'CREATE' || object.metadata.name != 'fs2-public-edge-node-authority-cas' || ((${local.public_edge_cas_bootstrap_creator_cel}) && request.resource.resource == 'validatingadmissionpolicies' && object.metadata.annotations['fs2.nebius.ai/contract'] == 'apply-time-membership-epoch-cas/v1' && object.spec.failurePolicy == 'Fail')"
          message    = "Only the exact source-enrolled, impersonation-reviewed Platform Security creator may create the fail-closed epoch CAS policy."
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'CREATE' || object.metadata.name != 'fs2-public-edge-node-authority-cas-binding' || ((${local.public_edge_cas_bootstrap_creator_cel}) && request.resource.resource == 'validatingadmissionpolicybindings' && object.spec.policyName == 'fs2-public-edge-node-authority-cas' && object.spec.validationActions == ['Deny'])"
          message    = "Only the exact source-enrolled, impersonation-reviewed Platform Security creator may activate the exact epoch CAS binding."
          reason     = "Forbidden"
        },
        {
          expression = "request.operation == 'CREATE' || !(oldObject.metadata.name in ['fs2-public-edge-node-authority-cas', 'fs2-public-edge-node-authority-cas-binding'])"
          message    = "The epoch CAS policy and binding cannot be updated or deleted after security-owned bootstrap."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicies' || (request.operation == 'DELETE' ? oldObject.metadata.name : object.metadata.name) != 'fs2-public-edge-node-authority' || (request.operation != 'DELETE' && (${local.public_edge_cas_bootstrap_creator_cel}) && params != null && object.spec == params.spec.policySpec && object.metadata.annotations == params.spec.policyAnnotations && object.metadata.annotations['fs2.nebius.ai/policy-content-sha256'] == params.spec.policyContentSha256 && params.spec.membershipReceiptSha256 == object.metadata.annotations['fs2.nebius.ai/membership-receipt-sha256'] && params.spec.approvalReceipt.payloadSha256 == params.spec.policyContentSha256 && params.spec.approvalReceipt.signature.size() > 0 && params.spec.impersonationReviewSha256 == '${try(local.public_edge_cas_bootstrap_authority.impersonation_review_sha256, strrep("0", 64))}' && params.spec.rbacReviewSha256 == '${try(local.public_edge_cas_bootstrap_authority.rbac_review_sha256, strrep("0", 64))}')"
          message    = "The dynamic Node-authority policy must exactly equal the externally signed Platform Security parameter and be submitted by its exact UID/groups/extras-reviewed actor. Deletion is forbidden."
          reason     = "Forbidden"
        },
        {
          expression = "request.resource.resource != 'validatingadmissionpolicybindings' || (request.operation == 'DELETE' ? oldObject.metadata.name : object.metadata.name) != 'fs2-public-edge-node-authority' || (request.operation == 'CREATE' && (${local.public_edge_cas_bootstrap_creator_cel}) && params != null && object.spec.policyName == 'fs2-public-edge-node-authority' && object.spec.validationActions == ['Deny'])"
          message    = "Only the exact Platform Security actor may create the Deny binding; update and deletion are forbidden."
          reason     = "Forbidden"
        },
      ]
    }
  }
  public_edge_cas_bootstrap_binding_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-public-edge-cas-bootstrap-binding"
    }
    spec = {
      policyName        = "fs2-public-edge-cas-bootstrap"
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
      paramRef = {
        name                    = try(local.public_edge_cas_bootstrap_authority.approval_name, "fs2-public-edge-node-authority-approval")
        parameterNotFoundAction = "Deny"
      }
    }
  }
  public_edge_cas_bootstrap_policy_sha256  = sha256(jsonencode(local.public_edge_cas_bootstrap_policy_manifest))
  public_edge_cas_bootstrap_binding_sha256 = sha256(jsonencode(local.public_edge_cas_bootstrap_binding_manifest))
}

# These objects are deliberately not Terraform resources. Platform Security
# installs them first through its separately approved cluster bootstrap path,
# before ordinary principals receive VAP create permission. Terraform only
# accepts their exact source projection and cannot manufacture their authority.
data "kubernetes_resources" "public_edge_cas_bootstrap_policy" {
  count          = local.public_edge_enabled ? 1 : 0
  api_version    = "admissionregistration.k8s.io/v1"
  kind           = "ValidatingAdmissionPolicy"
  field_selector = "metadata.name=fs2-public-edge-cas-bootstrap"
}

data "kubernetes_resources" "public_edge_cas_bootstrap_binding" {
  count          = local.public_edge_enabled ? 1 : 0
  api_version    = "admissionregistration.k8s.io/v1"
  kind           = "ValidatingAdmissionPolicyBinding"
  field_selector = "metadata.name=fs2-public-edge-cas-bootstrap-binding"
}

# The approval is produced only by the external security-owned signature and
# RBAC/impersonation admission service. Terraform has read-only custody.
data "kubernetes_resources" "public_edge_node_authority_approval" {
  count          = local.public_edge_enabled ? 1 : 0
  api_version    = try(local.public_edge_cas_bootstrap_authority.approval_api_version, "security.fs2.nebius.ai/v1")
  kind           = try(local.public_edge_cas_bootstrap_authority.approval_kind, "PublicEdgeNodeAuthorityApproval")
  field_selector = "metadata.name=${try(local.public_edge_cas_bootstrap_authority.approval_name, "fs2-public-edge-node-authority-approval")}"
}

locals {
  public_edge_observed_cas_bootstrap_policy = local.public_edge_enabled ? try(
    one(data.kubernetes_resources.public_edge_cas_bootstrap_policy[0].objects),
    null,
  ) : null
  public_edge_observed_cas_bootstrap_binding = local.public_edge_enabled ? try(
    one(data.kubernetes_resources.public_edge_cas_bootstrap_binding[0].objects),
    null,
  ) : null
  public_edge_observed_node_authority_approval = local.public_edge_enabled ? try(
    one(data.kubernetes_resources.public_edge_node_authority_approval[0].objects),
    null,
  ) : null
  public_edge_node_authority_approval_projection = {
    apiVersion = try(local.public_edge_observed_node_authority_approval.apiVersion, "")
    kind       = try(local.public_edge_observed_node_authority_approval.kind, "")
    metadata = {
      name = try(local.public_edge_observed_node_authority_approval.metadata.name, "")
    }
    spec = try(local.public_edge_observed_node_authority_approval.spec, null)
  }
  public_edge_node_authority_approval_sha256 = sha256(jsonencode(local.public_edge_node_authority_approval_projection))
  public_edge_observed_preventive_boundary = try(
    local.public_edge_observed_node_authority_approval.spec.preventiveBoundary,
    null,
  )
  # These fields are an input projection only. Their authority comes from the
  # separately signed raw boundary receipt and evidence reopened by the
  # apply-time verifier; this source file deliberately contains no asserted
  # provider-IAM or API-server enforcement identities.
  public_edge_observed_preventive_boundary_well_formed = try(
    keys(local.public_edge_observed_preventive_boundary) == sort([
      "apiserver_enforcement_id",
      "configuration_sha256",
      "controller_groups",
      "controller_image_digest",
      "controller_provider_principal_id",
      "controller_uid",
      "controller_username",
      "identity_paths",
      "kind",
      "provenance_attestation_sha256",
      "provider_iam_policy_id",
      "receipt_sha256",
      "source_commit",
      "source_repository",
      "source_tree",
    ]) &&
    local.public_edge_observed_preventive_boundary.kind == "provider-iam+apiserver-admission" &&
    local.public_edge_observed_preventive_boundary.controller_groups == sort(distinct(local.public_edge_observed_preventive_boundary.controller_groups)) &&
    local.public_edge_observed_preventive_boundary.identity_paths == local.public_edge_required_identity_paths &&
    can(regex("^sha256:[a-f0-9]{64}$", local.public_edge_observed_preventive_boundary.controller_image_digest)) &&
    can(regex("^serviceaccount-[a-z0-9]+$", local.public_edge_observed_preventive_boundary.controller_provider_principal_id)) &&
    can(regex("^[a-f0-9]{40}$", local.public_edge_observed_preventive_boundary.source_commit)) &&
    can(regex("^[a-f0-9]{40}$", local.public_edge_observed_preventive_boundary.source_tree)) &&
    can(regex("^https://[^[:space:]]+$", local.public_edge_observed_preventive_boundary.source_repository)) &&
    alltrue([
      for digest in [
        local.public_edge_observed_preventive_boundary.configuration_sha256,
        local.public_edge_observed_preventive_boundary.provenance_attestation_sha256,
        local.public_edge_observed_preventive_boundary.receipt_sha256,
      ] : can(regex("^[a-f0-9]{64}$", digest)) && digest != strrep("0", 64)
    ]),
    false,
  )
  public_edge_node_authority_approval_exact = !local.public_edge_enabled || (
    local.public_edge_cas_bootstrap_enrolled &&
    try(local.public_edge_observed_node_authority_approval.metadata.name, "") == local.public_edge_cas_bootstrap_authority.approval_name &&
    try(local.public_edge_observed_node_authority_approval.spec.policySpec, null) == local.public_edge_node_authority_policy_manifest.spec &&
    try(local.public_edge_observed_node_authority_approval.spec.policyAnnotations, null) == local.public_edge_node_authority_policy_manifest.metadata.annotations &&
    try(local.public_edge_observed_node_authority_approval.spec.policyContentSha256, "") == local.public_edge_node_authority_policy_content_sha256 &&
    try(local.public_edge_observed_node_authority_approval.spec.membershipReceiptSha256, "") == local.public_edge_membership_authority.receipt_sha256 &&
    try(local.public_edge_observed_node_authority_approval.spec.approvalReceipt.payloadSha256, "") == local.public_edge_node_authority_policy_content_sha256 &&
    try(length(local.public_edge_observed_node_authority_approval.spec.approvalReceipt.issuer) > 0, false) &&
    try(length(local.public_edge_observed_node_authority_approval.spec.approvalReceipt.keyId) > 0, false) &&
    try(length(local.public_edge_observed_node_authority_approval.spec.approvalReceipt.signature) > 0, false) &&
    try(local.public_edge_observed_node_authority_approval.spec.actor.username, "") == local.public_edge_cas_bootstrap_authority.username &&
    try(local.public_edge_observed_node_authority_approval.spec.actor.uid, "") == local.public_edge_cas_bootstrap_authority.uid &&
    try(local.public_edge_observed_node_authority_approval.spec.actor.groups, []) == local.public_edge_cas_bootstrap_authority.groups &&
    try(local.public_edge_observed_node_authority_approval.spec.actor.extra, {}) == local.public_edge_cas_bootstrap_authority.extra &&
    try(local.public_edge_observed_node_authority_approval.spec.impersonationReviewSha256, "") == local.public_edge_cas_bootstrap_authority.impersonation_review_sha256 &&
    try(local.public_edge_observed_node_authority_approval.spec.rbacReviewSha256, "") == local.public_edge_cas_bootstrap_authority.rbac_review_sha256 &&
    local.public_edge_observed_preventive_boundary_well_formed
  )
  public_edge_cas_bootstrap_exact = !local.public_edge_enabled || (
    local.public_edge_cas_bootstrap_enrolled &&
    try(local.public_edge_observed_cas_bootstrap_policy.apiVersion, "") == local.public_edge_cas_bootstrap_policy_manifest.apiVersion &&
    try(local.public_edge_observed_cas_bootstrap_policy.kind, "") == local.public_edge_cas_bootstrap_policy_manifest.kind &&
    try(local.public_edge_observed_cas_bootstrap_policy.metadata.name, "") == local.public_edge_cas_bootstrap_policy_manifest.metadata.name &&
    try(local.public_edge_observed_cas_bootstrap_policy.metadata.annotations, {}) == local.public_edge_cas_bootstrap_policy_manifest.metadata.annotations &&
    try(local.public_edge_observed_cas_bootstrap_policy.spec, null) == local.public_edge_cas_bootstrap_policy_manifest.spec &&
    try(local.public_edge_observed_cas_bootstrap_binding.apiVersion, "") == local.public_edge_cas_bootstrap_binding_manifest.apiVersion &&
    try(local.public_edge_observed_cas_bootstrap_binding.kind, "") == local.public_edge_cas_bootstrap_binding_manifest.kind &&
    try(local.public_edge_observed_cas_bootstrap_binding.metadata.name, "") == local.public_edge_cas_bootstrap_binding_manifest.metadata.name &&
    try(local.public_edge_observed_cas_bootstrap_binding.spec, null) == local.public_edge_cas_bootstrap_binding_manifest.spec
  )
}

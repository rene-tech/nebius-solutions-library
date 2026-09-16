locals {
  namespace = "fs2-system"

  current_contract = var.contract_generations[var.current_generation]
  current_trust    = var.trust_generations[local.current_contract.trust_generation]

  contract_digests = {
    for generation, contract in var.contract_generations :
    generation => sha256(jsonencode(jsondecode(contract.contract_json)))
  }
  contract_names = {
    for generation in keys(var.contract_generations) :
    generation => "fs2-customer-storage-egress-contract-${generation}"
  }
  trust_names = {
    for generation in keys(var.trust_generations) :
    generation => "fs2-customer-storage-egress-trust-${generation}"
  }
  network_policy_names = {
    for generation in keys(var.contract_generations) :
    generation => "fs2-customer-storage-egress-${generation}"
  }
  boundary_policy_names = {
    for generation in var.boundary_generations :
    generation => "fs2-customer-storage-egress-boundary-${generation}"
  }

  # The externally signed provider-authority ledger commits this digest. The
  # Kubernetes root therefore cannot substitute a different predecessor or
  # alter the fixed selector compatibility rule during an additive handoff.
  predecessor_compatibility = {
    schema            = "fs2-serve.nebius.ai/customer-storage-egress-predecessor/v1"
    namespace         = local.namespace
    predecessor_label = "storage-reconciler"
    successor_label   = "storage-reconciler-v2"
    deployment = {
      name        = data.kubernetes_resource.predecessor_deployment.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_deployment.object.spec))
    }
    network_policy = {
      name        = data.kubernetes_resource.predecessor_network_policy.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_network_policy.object.spec))
    }
    contract = {
      name        = data.kubernetes_resource.predecessor_contract.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_contract.object.metadata.uid
      data_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_contract.object.data))
    }
    policy = {
      name        = data.kubernetes_resource.predecessor_policy.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_policy.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_policy.object.spec))
    }
    binding = {
      name        = data.kubernetes_resource.predecessor_binding.object.metadata.name
      uid         = data.kubernetes_resource.predecessor_binding.object.metadata.uid
      spec_sha256 = sha256(jsonencode(data.kubernetes_resource.predecessor_binding.object.spec))
    }
  }
  predecessor_compatibility_sha256 = sha256(jsonencode(local.predecessor_compatibility))
}

data "external" "current_contract" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/../../stages/workloads/scripts/customer_storage_egress_contract.py",
    "--terraform-external",
  ]
  query = {
    contract_json  = local.current_contract.contract_json
    public_key_pem = local.current_trust.public_key_pem
  }
}

data "external" "identity_separation" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_owner_identity.py",
    "--terraform-external",
  ]
  query = {
    security_owner_kubeconfig_path = var.security_owner_kubeconfig_path
    security_owner_kube_context    = var.security_owner_kube_context
    workloads_kubeconfig_path      = var.workloads_kubeconfig_path
    workloads_kube_context         = var.workloads_kube_context
    security_owner_group           = var.security_owner_group
    non_owner_identities_json      = jsonencode(var.non_owner_identities)
    protected_names_json = jsonencode({
      boundary_policy = local.boundary_policy_names[var.current_boundary_generation]
      contract        = local.contract_names[var.current_generation]
      trust           = local.trust_names[local.current_contract.trust_generation]
      network_policy  = local.network_policy_names[var.current_generation]
      namespace       = local.namespace
    })
  }
}

# Read the fixed predecessor objects exactly as compatibility inputs. They are
# never imported, changed or deleted by this root. Their UIDs and content
# digests make the additive handoff prove which live boundary remains active.
data "kubernetes_resource" "predecessor_network_policy" {
  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  metadata {
    name      = "fs2-serve-control-plane-storage-reconciler"
    namespace = local.namespace
  }
}

data "kubernetes_resource" "predecessor_contract" {
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = "fs2-customer-storage-egress-contract"
    namespace = local.namespace
  }
}

data "kubernetes_resource" "predecessor_policy" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata { name = "fs2-customer-storage-egress" }
}

data "kubernetes_resource" "predecessor_binding" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata { name = "fs2-customer-storage-egress" }
}

data "kubernetes_resource" "predecessor_deployment" {
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "fs2-serve-control-plane-storage-reconciler"
    namespace = local.namespace
  }
}

resource "terraform_data" "separate_security_owner" {
  input = {
    current_generation             = var.current_generation
    current_boundary_generation    = var.current_boundary_generation
    current_contract_sha256        = data.external.current_contract.result.contract_sha256
    security_owner_group           = var.security_owner_group
    security_owner_subject         = data.external.identity_separation.result.security_owner_subject_sha256
    workloads_subject              = data.external.identity_separation.result.workloads_subject_sha256
    non_owner_inventory            = data.external.identity_separation.result.non_owner_inventory_sha256
    provider_authority_generation  = var.provider_authority.generation
    provider_authority_manifest    = var.provider_authority.authority_manifest_sha256
    predecessor_compatibility      = local.predecessor_compatibility_sha256
    provider_security_group_id     = var.provider_authority.security_group_id
    provider_node_group_id         = var.provider_authority.node_group_id
    predecessor_network_policy_uid = data.kubernetes_resource.predecessor_network_policy.object.metadata.uid
    predecessor_contract_uid       = data.kubernetes_resource.predecessor_contract.object.metadata.uid
    predecessor_policy_uid         = data.kubernetes_resource.predecessor_policy.object.metadata.uid
    predecessor_binding_uid        = data.kubernetes_resource.predecessor_binding.object.metadata.uid
    predecessor_deployment_uid     = data.kubernetes_resource.predecessor_deployment.object.metadata.uid
  }

  lifecycle {
    precondition {
      condition     = data.external.identity_separation.result.authorized == "true"
      error_message = "Live identity preflight did not prove an external owner and non-mutating workloads authority."
    }
    precondition {
      condition = (
        data.external.identity_separation.result.security_owner_subject_sha256 == var.provider_authority.kubernetes_security_owner_sha256 &&
        data.external.identity_separation.result.workloads_subject_sha256 == var.provider_authority.kubernetes_workloads_sha256 &&
        data.external.identity_separation.result.non_owner_inventory_sha256 == var.provider_authority.kubernetes_non_owner_subjects_sha256
      )
      error_message = "Kubernetes owner, workloads, release, human, break-glass or other identity inventory differs from the root-owned provider registry."
    }
    precondition {
      condition = (
        abspath(pathexpand(var.security_owner_kubeconfig_path)) != abspath(pathexpand(var.workloads_kubeconfig_path)) &&
        filesha256(pathexpand(var.security_owner_kubeconfig_path)) != filesha256(pathexpand(var.workloads_kubeconfig_path))
      )
      error_message = "The security-owner and workloads roots require different kubeconfig files and credential bytes."
    }
    precondition {
      condition     = contains(keys(var.contract_generations), var.current_generation)
      error_message = "current_generation must be retained in contract_generations."
    }
    precondition {
      condition     = contains(var.boundary_generations, var.current_boundary_generation)
      error_message = "current_boundary_generation must be retained in boundary_generations."
    }
    precondition {
      condition = alltrue([
        for contract in values(var.contract_generations) :
        contains(keys(var.trust_generations), contract.trust_generation)
      ])
      error_message = "Every contract generation must retain its exact trust generation."
    }
    precondition {
      condition     = data.external.current_contract.result.contract_sha256 == local.contract_digests[var.current_generation]
      error_message = "The current signed-contract verifier and immutable object digest differ."
    }
    precondition {
      condition = (
        var.provider_authority.contract_sha256 == data.external.current_contract.result.contract_sha256 &&
        var.provider_authority.provider_api_cidrs == jsondecode(data.external.current_contract.result.cidrs_json) &&
        var.provider_authority.kubernetes_api_cidrs == sort(tolist(local.current_contract.kubernetes_api_cidrs))
      )
      error_message = "The Kubernetes defense-in-depth generation must equal the external provider route contract."
    }
    precondition {
      condition = (
        try(data.kubernetes_resource.predecessor_binding.object.spec.policyName, "") == "fs2-customer-storage-egress" &&
        try(data.kubernetes_resource.predecessor_binding.object.spec.validationActions, []) == ["Deny"] &&
        try(data.kubernetes_resource.predecessor_network_policy.object.spec.podSelector.matchLabels["app.kubernetes.io/component"], "") == "storage-reconciler" &&
        try(data.kubernetes_resource.predecessor_deployment.object.spec.selector.matchLabels["app.kubernetes.io/component"], "") == "storage-reconciler"
      )
      error_message = "The fixed predecessor boundary is absent or is not the exact compatibility selector."
    }
    precondition {
      condition     = local.predecessor_compatibility_sha256 == var.provider_authority.predecessor_compatibility_sha256
      error_message = "The retained predecessor objects differ from the compatibility digest in the externally signed provider authority ledger."
    }
  }
}

# The policies are themselves append-only. Every installed generation denies
# deletion of every protected generation and requires the dedicated external
# owner group for additions or changes. A workloads/Helm identity cannot remove
# the binding first, weaken the policy, or replace the selected NetworkPolicy.
resource "kubernetes_manifest" "boundary_policy" {
  for_each = local.boundary_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
      annotations = {
        "fs2.nebius.ai/security-owner-subject-sha256" = data.external.identity_separation.result.security_owner_subject_sha256
        "fs2.nebius.ai/workloads-subject-sha256"      = data.external.identity_separation.result.workloads_subject_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = ["networking.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["networkpolicies"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["configmaps"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["admissionregistration.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
            scope       = "Cluster"
          },
        ]
      }
      matchConditions = [{
        name = "customer-storage-security-boundary"
        expression = join(" ", [
          "(request.resource.group == 'admissionregistration.k8s.io' &&",
          "request.name.startsWith('fs2-customer-storage-egress-boundary-')) ||",
          "(request.namespace == '${local.namespace}' &&",
          "request.name.startsWith('fs2-customer-storage-egress-')) ||",
          "(request.resource.group == 'networking.k8s.io' && request.namespace == '${local.namespace}' &&",
          "(request.operation == 'DELETE' ?",
          "(oldObject.metadata.name.startsWith('fs2-customer-storage-egress-g') ||",
          "(has(oldObject.spec.podSelector.matchLabels) &&",
          "'app.kubernetes.io/component' in oldObject.spec.podSelector.matchLabels &&",
          "oldObject.spec.podSelector.matchLabels['app.kubernetes.io/component'] == 'storage-reconciler-v2')) :",
          "(object.metadata.name.startsWith('fs2-customer-storage-egress-g') ||",
          "(has(object.spec.podSelector.matchLabels) &&",
          "'app.kubernetes.io/component' in object.spec.podSelector.matchLabels &&",
          "object.spec.podSelector.matchLabels['app.kubernetes.io/component'] == 'storage-reconciler-v2'))))",
        ])
      }]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "Customer-storage egress security generations are append-only and cannot be deleted."
          reason     = "Forbidden"
        },
        {
          expression = "request.userInfo.groups.exists(group, group == '${var.security_owner_group}')"
          message    = "Only the separately authenticated customer-storage security owner may change this boundary."
          reason     = "Forbidden"
        },
      ]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.separate_security_owner]
}

resource "kubernetes_manifest" "boundary_binding" {
  for_each = local.boundary_policy_names

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = each.value
      labels = {
        "app.kubernetes.io/managed-by"      = "fs2-security-owner"
        "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
        "fs2.nebius.ai/security-generation" = each.key
      }
    }
    spec = {
      policyName        = each.value
      validationActions = ["Deny"]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-customer-storage-security-owner"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.boundary_policy]
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = var.trust_generations

  metadata {
    name      = local.trust_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/security-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/public-key-sha256" = sha256(each.value.public_key_pem)
    }
  }
  immutable = true
  data = {
    "public-key.pem" = each.value.public_key_pem
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_manifest.boundary_binding]
}

resource "kubernetes_config_map_v1" "contract" {
  for_each = var.contract_generations

  metadata {
    name      = local.contract_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"      = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"      = "customer-storage-egress"
      "fs2.nebius.ai/security-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256"  = local.contract_digests[each.key]
      "fs2.nebius.ai/storage-egress-valid-until"      = jsondecode(each.value.contract_json).valid_until
      "fs2.nebius.ai/storage-egress-trust-generation" = each.value.trust_generation
    }
  }
  immutable = true
  data = {
    "contract.json"             = each.value.contract_json
    "kubernetes-api-cidrs.json" = jsonencode(sort(tolist(each.value.kubernetes_api_cidrs)))
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_config_map_v1.trust,
  ]
}

resource "kubernetes_network_policy_v1" "contract" {
  for_each = var.contract_generations

  metadata {
    name      = local.network_policy_names[each.key]
    namespace = local.namespace
    labels = {
      "app.kubernetes.io/managed-by"            = "fs2-security-owner"
      "fs2.nebius.ai/security-owner"            = "customer-storage-egress"
      "fs2.nebius.ai/security-generation"       = each.key
      "fs2.nebius.ai/storage-egress-generation" = each.key
    }
    annotations = {
      "fs2.nebius.ai/storage-egress-contract-sha256" = local.contract_digests[each.key]
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name"                  = "fs2-serve-control-plane"
        "app.kubernetes.io/instance"              = "fs2-serve-control-plane"
        "app.kubernetes.io/component"             = "storage-reconciler-v2"
        "fs2.nebius.ai/storage-egress-generation" = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "coredns"
            "app.kubernetes.io/name"     = "coredns"
            "k8s-app"                    = "coredns"
          }
        }
      }
      ports {
        port     = "53"
        protocol = "UDP"
      }
      ports {
        port     = "53"
        protocol = "TCP"
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-data" }
        }
        pod_selector {
          match_labels = { "cnpg.io/cluster" = "fs2-control-db" }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    egress {
      dynamic "to" {
        for_each = toset(jsondecode(each.value.contract_json).cidrs)
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }

    egress {
      dynamic "to" {
        for_each = each.value.kubernetes_api_cidrs
        content {
          ip_block { cidr = to.value }
        }
      }
      ports {
        port     = "443"
        protocol = "TCP"
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.boundary_binding,
    kubernetes_config_map_v1.contract,
  ]
}

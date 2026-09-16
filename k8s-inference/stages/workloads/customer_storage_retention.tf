# The deployed fixed-name SAI-08 objects retain their original Terraform
# addresses and live ownership while the disjoint v2 generation is added.
# `ignore_changes = all` is intentional custody: this root neither rewrites an
# immutable predecessor nor launders it out of state. The separately signed
# live UID/spec receipt must still match before the v2 root can create anything.

locals {
  customer_storage_predecessor_network_policy_name = "fs2-serve-control-plane-storage-reconciler"
  customer_storage_predecessor_network_policy_spec = {
    podSelector = {
      matchLabels = {
        "app.kubernetes.io/name"      = "fs2-serve-control-plane"
        "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
        "app.kubernetes.io/component" = "storage-reconciler"
      }
    }
    policyTypes = ["Ingress", "Egress"]
    ingress     = []
    egress = [
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
          podSelector = { matchLabels = {
            "app.kubernetes.io/instance" = "coredns"
            "app.kubernetes.io/name"     = "coredns"
            "k8s-app"                    = "coredns"
          } }
        }]
        ports = [{ port = 53, protocol = "UDP" }, { port = 53, protocol = "TCP" }]
      },
      {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-data" } }
          podSelector       = { matchLabels = { "cnpg.io/cluster" = "fs2-control-db" } }
        }]
        ports = [{ port = 5432, protocol = "TCP" }]
      },
      {
        to = [for cidr in var.customer_storage.egress_boundary.predecessor_compatibility.egress_cidrs : { ipBlock = { cidr = cidr } }]
        ports = [{ port = 443, protocol = "TCP" }]
      },
      {
        to = [for cidr in var.customer_storage.egress_boundary.predecessor_compatibility.kubernetes_api_cidrs : { ipBlock = { cidr = cidr } }]
        ports = [{ port = 443, protocol = "TCP" }]
      },
    ]
  }
}

resource "kubernetes_config_map_v1" "customer_storage_egress_contract" {
  count = var.customer_storage.enabled ? 1 : 0

  metadata {
    name      = "fs2-customer-storage-egress-contract"
    namespace = "fs2-system"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "kubernetes_manifest" "customer_storage_egress_admission_policy" {
  count = var.customer_storage.enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-customer-storage-egress"
      annotations = {
        "fs2.nebius.ai/storage-egress-contract-sha256" = var.customer_storage.egress_boundary.predecessor_compatibility.contract_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = ["networking.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["networkpolicies"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name = "storage-reconciler-policy"
        expression = join(" ", [
          "request.namespace == 'fs2-system' &&",
          "(request.operation == 'DELETE' ?",
          "(oldObject.metadata.name == '${local.customer_storage_predecessor_network_policy_name}' ||",
          "!has(oldObject.spec.podSelector.matchLabels) ||",
          "!('app.kubernetes.io/component' in oldObject.spec.podSelector.matchLabels) ||",
          "oldObject.spec.podSelector.matchLabels['app.kubernetes.io/component'] == 'storage-reconciler') :",
          "(object.metadata.name == '${local.customer_storage_predecessor_network_policy_name}' ||",
          "!has(object.spec.podSelector.matchLabels) ||",
          "!('app.kubernetes.io/component' in object.spec.podSelector.matchLabels) ||",
          "object.spec.podSelector.matchLabels['app.kubernetes.io/component'] == 'storage-reconciler'))",
        ])
      }]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "The retained predecessor egress policy cannot be deleted."
          reason     = "Forbidden"
        },
        {
          expression = join(" ", [
            "object.metadata.name == '${local.customer_storage_predecessor_network_policy_name}' &&",
            "has(object.metadata.annotations) &&",
            "object.metadata.annotations['fs2.nebius.ai/storage-egress-contract-sha256'] == '${var.customer_storage.egress_boundary.predecessor_compatibility.contract_sha256}' &&",
            "object.spec == ${jsonencode(local.customer_storage_predecessor_network_policy_spec)}",
          ])
          message = "A predecessor-selecting NetworkPolicy must remain the externally receipted fixed policy."
          reason  = "Forbidden"
        },
      ]
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "kubernetes_manifest" "customer_storage_egress_admission_binding" {
  count = var.customer_storage.enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata   = { name = "fs2-customer-storage-egress" }
    spec = {
      policyName        = "fs2-customer-storage-egress"
      validationActions = ["Deny"]
      matchResources = {
        namespaceSelector = {
          matchLabels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

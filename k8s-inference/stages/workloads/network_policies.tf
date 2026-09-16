locals {
  # Keep a Terraform-owned allow policy in front of the namespace default deny
  # for every catalog route. This closes the first-apply window before the model
  # controller has reconciled its finer, per-Deployment owner policies and also
  # covers the small set of intentionally Terraform-owned runtimes.
  model_runtime_network_routes = {
    for model_id, route in local.selected_routes : model_id => {
      port = route.service.port
    }
  }
}

resource "kubernetes_network_policy_v1" "model_runtime_bootstrap" {
  for_each = local.model_runtime_network_routes

  metadata {
    name      = "fs2-runtime-bootstrap-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"      = "model-runtime-network"
      "fs2-serve.nebius.ai/model-id"     = each.key
      "fs2-serve.nebius.ai/policy-owner" = "terraform-bootstrap"
    })
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"  = "model-runtime"
        "fs2-serve.nebius.ai/model-id" = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(each.value.port)
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_expressions {
            key      = "k8s-app"
            operator = "In"
            values   = ["coredns", "kube-dns"]
          }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }
  }

}

resource "kubernetes_network_policy_v1" "model_namespace_default_deny" {
  metadata {
    name      = "default-deny"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "namespace-network-boundary"
    })
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  # The allow policies must exist before the namespace boundary closes. The
  # controller then replaces this coarse bootstrap coverage with exact
  # per-Deployment policies without ever opening an unrestricted interval.
  depends_on = [kubernetes_network_policy_v1.model_runtime_bootstrap]
}

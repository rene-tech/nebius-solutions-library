locals {
  # SAI-20: the control plane and model controller need only the stable
  # in-cluster Kubernetes Service address and the currently ready API endpoint
  # host routes. The target subnet is intentionally absent: it is not an API
  # identity and would authorize every HTTPS listener in that subnet.
  sai20_kubernetes_api_egress_cidrs = setunion(
    local.kubernetes_api_service_cidrs,
    local.kubernetes_api_endpoint_cidrs,
  )
  sai20_control_plane_network_policy_overrides = {
    networkPolicy = {
      kubernetesApiCidrs = sort(tolist(local.sai20_kubernetes_api_egress_cidrs))
    }
  }

  # Every listed component has a database credential and a chart-owned egress
  # rule. Future consumers must be added deliberately on both sides. The
  # website/storage entries preserve the additive SAI-08 integration surface;
  # they select no Pod until that separately reviewed source is integrated.
  sai20_database_client_components = [
    "bootstrap-access",
    "bootstrap-scientific-access",
    "bootstrap-website-access",
    "gateway",
    "maintenance",
    "migration",
    "model-controller",
    "storage-disclosure",
    "storage-reconciler",
  ]

  # SAI-08 successor 6eb13e345c8b17420d1217a70d83e4974497b2b0 uses a
  # generation-bound storage-reconciler-v3 identity. It is deliberately kept
  # out of the legacy component list above so that the two generation labels
  # cannot be bypassed by matching only the component name.
  sai20_storage_reconciler_v3_component = "storage-reconciler-v3"

  # SAI-08 exact successor 6eb13e345c8b17420d1217a70d83e4974497b2b0
  # deliberately retains the immediately preceding protected lane. Keep that
  # live rollback generation reachable until an independently signed inventory
  # records its conclusive retirement.
  sai20_storage_reconciler_v2_component = "storage-reconciler-v2"
}

resource "kubernetes_network_policy_v1" "control_database_ingress" {
  metadata {
    name      = "fs2-control-db-ingress"
    namespace = "fs2-data"
    labels    = local.common_labels
    annotations = {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
      "security.fs2.nebius.ai/custody-binding"          = kubernetes_manifest.sai20_database_object_custody_binding.manifest.metadata.name
      "security.fs2.nebius.ai/review-receipt-sha256"    = terraform_data.sai20_database_network_custody.output.independent_review_receipt_sha256
      "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      "security.fs2.nebius.ai/policy-set-custody"       = kubernetes_manifest.sai20_database_policy_set_custody_v3.manifest.metadata.name
      "security.fs2.nebius.ai/policy-set-binding"       = kubernetes_manifest.sai20_database_policy_set_custody_binding_v3.manifest.metadata.name
      "security.fs2.nebius.ai/workload-custody"         = kubernetes_manifest.sai20_database_workload_custody_binding_v3.manifest.metadata.name
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "cnpg.io/cluster" = "fs2-control-db"
      }
    }
    policy_types = ["Ingress"]

    # Runtime, maintenance, migration, bootstrap, model-controller and the
    # separately reviewed storage workers. Namespace and release identity are
    # conjoined with the finite component set; a namespace label alone never
    # authorizes PostgreSQL access.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "fs2-serve-control-plane"
            "app.kubernetes.io/name"     = "fs2-serve-control-plane"
          }
          match_expressions {
            key      = "app.kubernetes.io/component"
            operator = "In"
            values   = local.sai20_database_client_components
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    # The SAI-08 v2 release is retained as a rollback-safe database client.
    # Its content-derived egress-generation label is mandatory; omitting this
    # peer would strand a preserved client while its egress still permits 5432.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = local.sai20_storage_reconciler_v2_component
          }
          match_expressions {
            key      = "fs2.nebius.ai/storage-egress-generation"
            operator = "Exists"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    # The additive SAI-08 customer-storage successor is a database client.
    # Require both of its content-derived generation labels as well as the
    # exact v3 component label so a legacy or partially labelled Pod cannot
    # inherit this path.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = local.sai20_storage_reconciler_v3_component
          }
          match_expressions {
            key      = "fs2.nebius.ai/storage-egress-generation"
            operator = "Exists"
          }
          match_expressions {
            key      = "fs2.nebius.ai/storage-rollout-generation"
            operator = "Exists"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    # Grafana owns the reporting credential and is the sole interactive
    # observability client of PostgreSQL.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-observability"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "grafana"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    # Terraform acceptance Jobs are bound to this exact run as well as their
    # component label. Both current namespaces are explicit peers.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component"  = "acceptance"
            "app.kubernetes.io/managed-by" = "terraform"
            "app.kubernetes.io/part-of"    = "fs2-serve"
            "fs2.nebius.ai/environment"    = "disposable"
            "fs2.nebius.ai/run-id"         = var.run_id
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-observability"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component"  = "acceptance"
            "app.kubernetes.io/managed-by" = "terraform"
            "app.kubernetes.io/part-of"    = "fs2-serve"
            "fs2.nebius.ai/environment"    = "disposable"
            "fs2.nebius.ai/run-id"         = var.run_id
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
    }

    # CloudNativePG requires database instances to communicate with one
    # another. This peer remains inside fs2-data because a podSelector without
    # a namespaceSelector is namespace-local.
    ingress {
      from {
        pod_selector {
          match_labels = {
            "cnpg.io/cluster" = "fs2-control-db"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
      ports {
        port     = "8000"
        protocol = "TCP"
      }
    }

    # The operator uses PostgreSQL and the instance-manager status endpoint
    # for declarative lifecycle and failover management.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "cnpg-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "cloudnative-pg"
          }
        }
      }
      ports {
        port     = "5432"
        protocol = "TCP"
      }
      ports {
        port     = "8000"
        protocol = "TCP"
      }
    }

    # Preserve the existing PodMonitor without granting Prometheus database or
    # instance-manager access.
    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-observability"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name" = "prometheus"
          }
        }
      }
      ports {
        port     = "9187"
        protocol = "TCP"
      }
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

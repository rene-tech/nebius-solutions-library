locals {
  common_labels = {
    "app.kubernetes.io/name"       = "fs2-reference-data"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "app.kubernetes.io/managed-by" = "terraform"
  }
  tools_sha256        = filesha256("${path.module}/../reference_data.py")
  tools_config_map    = "fs2-reference-data-tools-${substr(local.tools_sha256, 0, 12)}"
  object_bucket_name  = var.create_object_bucket ? one(nebius_storage_v1_bucket.reference_data[*].name) : var.object_bucket_name
  object_endpoint     = "https://storage.${var.object_storage_region}.nebius.cloud"
  object_prefix       = "s3://${local.object_bucket_name}/reference-data"
  filesystem_file_uri = "file://${var.shared_filesystem_host_path}"
}

resource "terraform_data" "region_contract" {
  input = {
    cluster_region        = var.cluster_region
    object_storage_region = var.object_storage_region
  }
  lifecycle {
    precondition {
      condition     = var.cluster_region == var.object_storage_region
      error_message = "reference data, object storage, shared filesystem and preprocessing must stay in the cluster region."
    }
  }
}

resource "nebius_storage_v1_bucket" "reference_data" {
  count = var.create_object_bucket ? 1 : 0

  parent_id         = var.project_id
  name              = var.object_bucket_name
  versioning_policy = "ENABLED"

  depends_on = [terraform_data.region_contract]
}

resource "kubernetes_namespace_v1" "reference_data" {
  metadata {
    name = var.namespace
    labels = merge(local.common_labels, {
      "kubernetes.io/metadata.name"        = var.namespace
      "reference-data.fs2.nebius.ai/plane" = "private"
    })
  }

  depends_on = [terraform_data.region_contract]
}

resource "kubernetes_service_account_v1" "reference_data" {
  metadata {
    name      = "fs2-reference-data"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  automount_service_account_token = false
}

resource "kubernetes_config_map_v1" "tools" {
  metadata {
    name      = local.tools_config_map
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
    annotations = {
      "reference-data.fs2.nebius.ai/source-sha256" = local.tools_sha256
    }
  }
  immutable = true
  data = {
    "reference_data.py" = file("${path.module}/../reference_data.py")
  }
}

resource "kubernetes_manifest" "cpu_flavor" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name   = var.queue.resource_flavor
      labels = local.common_labels
    }
    spec = {
      nodeLabels = {
        "workload.fs2.nebius/system" = "true"
        "capacity.fs2.nebius/type"   = "regular"
        "capacity.fs2.nebius/pool"   = "system"
      }
    }
  }
}

resource "kubernetes_manifest" "cpu_cluster_queue" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ClusterQueue"
    metadata = {
      name   = var.queue.cluster_queue
      labels = local.common_labels
    }
    spec = {
      namespaceSelector = {
        matchLabels = {
          "reference-data.fs2.nebius.ai/plane" = "private"
        }
      }
      queueingStrategy = "BestEffortFIFO"
      resourceGroups = [{
        coveredResources = ["cpu", "memory"]
        flavors = [{
          name = var.queue.resource_flavor
          resources = [
            { name = "cpu", nominalQuota = var.queue.nominal_cpu },
            { name = "memory", nominalQuota = var.queue.nominal_memory },
          ]
        }]
      }]
      stopPolicy = "None"
    }
  }
  depends_on = [kubernetes_manifest.cpu_flavor]
}

resource "kubernetes_manifest" "local_queue" {
  manifest = {
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "LocalQueue"
    metadata = {
      name      = var.queue.local_queue
      namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
      labels    = local.common_labels
    }
    spec = { clusterQueue = var.queue.cluster_queue }
  }
  depends_on = [kubernetes_manifest.cpu_cluster_queue]
}

resource "kubernetes_network_policy_v1" "default_deny" {
  metadata {
    name      = "default-deny"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }
}

resource "kubernetes_network_policy_v1" "dns" {
  metadata {
    name      = "allow-dns"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {}
    policy_types = ["Egress"]
    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_labels = { "k8s-app" = "kube-dns" }
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

resource "kubernetes_network_policy_v1" "private_object_storage" {
  count = length(var.object_storage_egress_cidrs) > 0 ? 1 : 0
  metadata {
    name      = "private-msa-object-storage"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "private-msa"
        "reference-data.fs2.nebius.ai/network-mode" = "private-only"
      }
    }
    policy_types = ["Egress"]
    dynamic "egress" {
      for_each = var.object_storage_egress_cidrs
      content {
        to {
          ip_block {
            cidr = egress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "public_source_staging" {
  count = var.allow_public_source_staging ? 1 : 0
  metadata {
    name      = "public-source-staging-opt-in"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {
      match_labels = {
        "reference-data.fs2.nebius.ai/network-mode" = "public-source-staging"
      }
    }
    policy_types = ["Egress"]
    egress {
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "public_msa_opt_in" {
  count = var.allow_public_msa_opt_in ? 1 : 0
  metadata {
    name      = "public-msa-explicit-opt-in"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "private-msa"
        "reference-data.fs2.nebius.ai/network-mode" = "public-opt-in"
      }
    }
    policy_types = ["Egress"]
    egress {
      to {
        ip_block {
          cidr = "0.0.0.0/0"
        }
      }
      ports {
        protocol = "TCP"
        port     = "443"
      }
    }
  }
}

resource "kubernetes_deployment_v1" "status" {
  count = var.status.enabled ? 1 : 0
  metadata {
    name      = "fs2-reference-data-status"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "status" })
  }
  spec {
    replicas = var.status.replicas
    selector { match_labels = { "app.kubernetes.io/name" = "fs2-reference-data-status" } }
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/name"      = "fs2-reference-data-status"
          "app.kubernetes.io/component" = "status"
        })
        annotations = { "reference-data.fs2.nebius.ai/tools-sha256" = local.tools_sha256 }
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.reference_data.metadata[0].name
        automount_service_account_token = false
        enable_service_links            = false
        node_selector = {
          "workload.fs2.nebius/system" = "true"
          "capacity.fs2.nebius/type"   = "regular"
          "capacity.fs2.nebius/pool"   = "system"
        }
        security_context {
          run_as_non_root = true
          run_as_user     = 1000
          run_as_group    = 1000
          fs_group        = 1000
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name              = "status"
          image             = var.status.image
          image_pull_policy = "IfNotPresent"
          command = [
            "python", "/opt/fs2/reference-data/reference_data.py", "serve-status",
            "--root", "/reference-data", "--port", "8080",
          ]
          port {
            name           = "http"
            container_port = 8080
            protocol       = "TCP"
          }
          resources {
            requests = { cpu = "50m", memory = "64Mi", ephemeral-storage = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi", ephemeral-storage = "256Mi" }
          }
          readiness_probe {
            http_get {
              path = "/readyz"
              port = "http"
            }
            period_seconds = 10
          }
          liveness_probe {
            http_get {
              path = "/healthz"
              port = "http"
            }
            period_seconds = 30
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "reference-data"
            mount_path = "/reference-data"
            read_only  = true
          }
          volume_mount {
            name       = "tools"
            mount_path = "/opt/fs2/reference-data"
            read_only  = true
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "reference-data"
          host_path {
            path = var.shared_filesystem_host_path
            type = "Directory"
          }
        }
        volume {
          name = "tools"
          config_map {
            name         = kubernetes_config_map_v1.tools.metadata[0].name
            default_mode = "0555"
          }
        }
        volume {
          name = "tmp"
          empty_dir {
            size_limit = "256Mi"
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "status" {
  count = var.status.enabled ? 1 : 0
  metadata {
    name      = "fs2-reference-data-status"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "status" })
  }
  spec {
    selector = { "app.kubernetes.io/name" = "fs2-reference-data-status" }
    port {
      name        = "http"
      port        = 8080
      target_port = "http"
    }
    type = "ClusterIP"
  }
}

resource "kubernetes_network_policy_v1" "status_ingress" {
  count = var.status.enabled ? 1 : 0
  metadata {
    name      = "status-ingress"
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name" = "fs2-reference-data-status"
      }
    }
    policy_types = ["Ingress"]
    dynamic "ingress" {
      for_each = var.status_ingress_namespaces
      content {
        from {
          namespace_selector {
            match_labels = {
              "kubernetes.io/metadata.name" = ingress.value
            }
          }
        }
        ports {
          protocol = "TCP"
          port     = "8080"
        }
      }
    }
  }
}

resource "kubernetes_manifest" "status_service_monitor" {
  count = var.status.enabled && var.service_monitor_enabled ? 1 : 0
  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "ServiceMonitor"
    metadata = {
      name      = "fs2-reference-data"
      namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
      labels    = local.common_labels
    }
    spec = {
      selector          = { matchLabels = { "app.kubernetes.io/component" = "status" } }
      namespaceSelector = { matchNames = [var.namespace] }
      endpoints         = [{ port = "http", path = "/metrics", interval = "30s" }]
    }
  }
  depends_on = [kubernetes_service_v1.status]
}

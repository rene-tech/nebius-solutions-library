terraform {
  required_version = ">= 1.9.0"
  backend "local" {}
  required_providers {
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38" }
  }
}

variable "kube_context" {
  type        = string
  description = "Explicit shared test-fleet context. Never the Scientific AI customer cluster."
  validation {
    condition     = startswith(var.kube_context, "nebius-mk8s-s2pr2-control-")
    error_message = "This isolated pilot may deploy only to the s2pr2 control cluster."
  }
}
variable "kube_config_path" {
  type        = string
  default     = "~/.kube/config"
  description = "Operator kubeconfig; the explicit test-fleet context is still required."
}
variable "image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.image))
    error_message = "Pin the exact qualified API image digest."
  }
}
variable "private_config_file" {
  type        = string
  description = "0600 operator file outside Git; Terraform state must also stay private."
}
variable "api_host" {
  type = string
}

provider "kubernetes" {
  config_path    = pathexpand(var.kube_config_path)
  config_context = var.kube_context
}

locals {
  name = "scientific-ai-pilot"
}

resource "kubernetes_namespace_v1" "application" {
  metadata {
    name = local.name
    labels = {
      "app.kubernetes.io/name"             = local.name
      "pod-security.kubernetes.io/enforce" = "restricted"
    }
    annotations = {
      "scientific-ai.nebius/owner"   = "scientific-ai-serverless2-migration"
      "scientific-ai.nebius/purpose" = "Isolated GROMACS API/MCP acceptance; no customer traffic"
    }
  }
}

resource "kubernetes_secret_v1" "configuration" {
  metadata {
    name      = "application-config"
    namespace = kubernetes_namespace_v1.application.metadata[0].name
  }
  data = { "config.json" = sensitive(file(var.private_config_file)) }
}

resource "kubernetes_deployment_v1" "application" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.application.metadata[0].name
  }
  spec {
    replicas = 1
    selector { match_labels = { app = local.name } }
    template {
      metadata {
        labels = { app = local.name }
        annotations = {
          "checksum/configuration" = sha256(sensitive(file(var.private_config_file)))
        }
      }
      spec {
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          fs_group        = 10001
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name  = "api"
          image = var.image
          port { container_port = 8000 }
          env {
            name  = "SCIENTIFIC_AI_CONFIG"
            value = "/run/config/config.json"
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          resources {
            requests = { cpu = "500m", memory = "512Mi" }
            limits   = { cpu = "2", memory = "2Gi" }
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
          }
          liveness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 15
          }
          volume_mount {
            name       = "config"
            mount_path = "/run/config"
            read_only  = true
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "config"
          secret {
            secret_name  = kubernetes_secret_v1.configuration.metadata[0].name
            default_mode = "0440"
          }
        }
        volume {
          name = "tmp"
          empty_dir { size_limit = "10Gi" }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "application" {
  metadata {
    name      = local.name
    namespace = kubernetes_namespace_v1.application.metadata[0].name
  }
  spec {
    selector = { app = local.name }
    port {
      port        = 8000
      target_port = 8000
    }
  }
}

resource "kubernetes_manifest" "route" {
  manifest = {
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "HTTPRoute"
    metadata = {
      name      = local.name
      namespace = kubernetes_namespace_v1.application.metadata[0].name
    }
    spec = {
      parentRefs = [{ name = "serverless2-external", namespace = "envoy-gateway-system", sectionName = "https" }]
      hostnames  = [var.api_host]
      rules = [{
        matches     = [{ path = { type = "PathPrefix", value = "/scientific-ai" } }]
        filters     = [{ type = "URLRewrite", urlRewrite = { path = { type = "ReplacePrefixMatch", replacePrefixMatch = "/" } } }]
        backendRefs = [{ name = local.name, port = 8000 }]
        timeouts    = { request = "600s", backendRequest = "600s" }
      }]
    }
  }
}

output "api_url" { value = "https://${var.api_host}/scientific-ai/v1" }
output "mcp_url" { value = "https://${var.api_host}/scientific-ai/mcp" }

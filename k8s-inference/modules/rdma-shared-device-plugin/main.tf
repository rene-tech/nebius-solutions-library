terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "= 3.2.1"
    }
  }
}

variable "pool_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,40}$", var.pool_id))
    error_message = "pool_id must be a bounded canonical accelerator pool identifier."
  }
}

variable "gpu_cluster_id" {
  type = string
  validation {
    condition     = can(regex("^computegpucluster-[a-z0-9]+$", var.gpu_cluster_id))
    error_message = "An exact provider GPU-cluster identity is required."
  }
}

locals {
  name = "fs2-rdma-${var.pool_id}"
  # This installs only the device allocator. Managed Nebius images own OFED
  # and GPU drivers: https://docs.nebius.com/kubernetes/gpu/set-up .
  # One allocation exposes all selected HCAs. It is deliberately NOT a large
  # shared count: a full-node scientific Pod exclusively reserves this bundle.
  config = jsonencode({
    periodicUpdateInterval = 60
    configList = [{
      resourceName   = "hca"
      resourcePrefix = "rdma.fs2.nebius"
      rdmaHcaMax     = 1
      selectors = {
        vendors   = ["15b3"]
        drivers   = ["mlx5_core"]
        linkTypes = ["infiniband"]
      }
    }]
  })
  config_map = {
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata   = { name = local.name, namespace = "kube-system" }
    data       = { "config.json" = local.config }
  }
  daemon_set = {
    apiVersion = "apps/v1"
    kind       = "DaemonSet"
    metadata   = { name = local.name, namespace = "kube-system" }
    spec = {
      selector = { matchLabels = { app = local.name } }
      template = {
        metadata = {
          labels      = { app = local.name }
          annotations = { "fs2-serve.nebius.ai/config-sha256" = sha256(local.config) }
        }
        spec = {
          hostNetwork                  = true
          dnsPolicy                    = "ClusterFirstWithHostNet"
          priorityClassName            = "system-node-critical"
          automountServiceAccountToken = false
          nodeSelector = {
            "accelerator.fs2.nebius/pool-id"     = var.pool_id
            "topology.fs2.nebius/scope"          = "gpu_cluster"
            "topology.nebius.com/gpu-cluster-id" = var.gpu_cluster_id
          }
          tolerations = [{ key = "dedicated", operator = "Equal", value = "fs2-inference", effect = "NoSchedule" }]
          containers = [{
            name            = "rdma-device-plugin"
            image           = "ghcr.io/mellanox/k8s-rdma-shared-dev-plugin:v1.5.4@sha256:97e10cbcdba89f295152b4d55cb266078764aacb7180938c7f840fb3d712f875"
            imagePullPolicy = "IfNotPresent"
            # Upstream allocator needs host device discovery/registration.
            # This privilege belongs only to this infrastructure DaemonSet;
            # scientific Pods receive devices through the extended resource.
            securityContext = { privileged = true }
            resources = {
              requests = { cpu = "20m", memory = "64Mi" }
              limits   = { cpu = "200m", memory = "256Mi" }
            }
            volumeMounts = [
              { name = "device-plugin", mountPath = "/var/lib/kubelet/device-plugins" },
              { name = "plugins-registry", mountPath = "/var/lib/kubelet/plugins_registry" },
              { name = "config", mountPath = "/k8s-rdma-shared-dev-plugin", readOnly = true },
              { name = "devs", mountPath = "/dev" },
            ]
          }]
          volumes = [
            { name = "device-plugin", hostPath = { path = "/var/lib/kubelet/device-plugins", type = "Directory" } },
            { name = "plugins-registry", hostPath = { path = "/var/lib/kubelet/plugins_registry", type = "DirectoryOrCreate" } },
            { name = "config", configMap = { name = local.name } },
            { name = "devs", hostPath = { path = "/dev", type = "Directory" } },
          ]
        }
      }
    }
  }
}

resource "kubernetes_manifest" "config" {
  manifest = local.config_map
}

resource "kubernetes_manifest" "plugin" {
  manifest   = local.daemon_set
  depends_on = [kubernetes_manifest.config]
}

output "manifests" {
  value = [local.config_map, local.daemon_set]
}

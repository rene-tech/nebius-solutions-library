mock_provider "kubernetes" {}

variables {
  pool_id        = "h100-reserved-8x"
  gpu_cluster_id = "computegpucluster-example"
}

run "exact_pool_plugin_only" {
  command = plan
  assert {
    condition     = local.daemon_set.spec.template.spec.nodeSelector["topology.nebius.com/gpu-cluster-id"] == var.gpu_cluster_id && local.daemon_set.spec.template.spec.nodeSelector["accelerator.fs2.nebius/pool-id"] == var.pool_id
    error_message = "Device plugin must target the exact pool and fabric identity."
  }
  assert {
    condition     = jsondecode(local.config).configList[0].rdmaHcaMax == 1 && jsondecode(local.config).configList[0].resourcePrefix == "rdma.fs2.nebius"
    error_message = "Each full-node Pod must exclusively claim the HCA bundle."
  }
  assert {
    condition     = length(local.daemon_set.spec.template.spec.containers) == 1 && !local.daemon_set.spec.template.spec.automountServiceAccountToken && strcontains(local.daemon_set.spec.template.spec.containers[0].image, "@sha256:")
    error_message = "Only the digest-pinned allocator is installed, with no API token or driver sidecar."
  }
}

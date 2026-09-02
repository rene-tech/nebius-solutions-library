mock_provider "kubernetes" {}

variables {
  cluster_region        = "eu-north1"
  object_storage_region = "eu-north1"
  object_bucket_name    = "fs2-reference-data-empty-volume-test"
  object_storage_access = {
    access_key_id       = "accesskey-test"
    secret_reference_id = "secret-test"
    revision            = 1
  }
  namespace = "fs2-reference-data"
  cpu_pool = {
    id         = "mk8snodegroup-reference-test"
    name       = "reference-data-cpu"
    platform   = "cpu-d3"
    preset     = "8vcpu-32gb"
    node_count = 1
    capacity   = "regular"
    schedulable_capacity = {
      cpu_millicores        = 7000
      memory_mib            = 28672
      ephemeral_storage_mib = 114688
    }
    node_labels = {
      "workload.fs2.nebius/reference-data" = "true"
      "capacity.fs2.nebius/type"           = "regular"
      "capacity.fs2.nebius/pool"           = "reference-data"
      "storage.fs2.nebius/reference-data"  = "true"
    }
    taint = {
      key    = "workload.fs2.nebius/reference-data"
      value  = "true"
      effect = "NoSchedule"
    }
  }
  shared_filesystem_host_path = "/mnt/fs2-reference-data/data"
  status = {
    enabled  = true
    image    = "registry.eu-north1.nebius.cloud/reference/status@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    replicas = 1
  }
  pipeline = { enabled = false }
}

run "fresh_empty_volume_status_rollout_is_service_ready" {
  command = apply

  plan_options {
    # Credential and batch resources are intentionally outside this bootstrap
    # acceptance. Terraform's mock providers cannot model ephemeral resources,
    # and the status service needs neither object credentials nor a dataset Job.
    target = [kubernetes_deployment_v1.status]
  }

  assert {
    condition = (
      kubernetes_deployment_v1.status[0].wait_for_rollout == true &&
      kubernetes_deployment_v1.status[0].spec[0].template[0].spec[0].container[0].readiness_probe[0].http_get[0].path == "/healthz" &&
      kubernetes_deployment_v1.status[0].spec[0].template[0].spec[0].container[0].liveness_probe[0].http_get[0].path == "/healthz"
    )
    error_message = "A fresh empty volume must not make provider rollout wait on dataset readiness."
  }

  assert {
    condition     = kubernetes_deployment_v1.status[0].spec[0].template[0].spec[0].volume[0].host_path[0].path == "/mnt/fs2-reference-data/data"
    error_message = "Empty-volume bootstrap must mount the dedicated reference path."
  }
}

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

run "live_database_namespace_is_rejected" {
  command = plan

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    namespace = "fs2-data"
  }

  expect_failures = [var.namespace]
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

run "reference_queue_admits_every_declared_consumer_namespace" {
  command = plan

  plan_options {
    target = [kubernetes_manifest.cpu_cluster_queue]
  }

  variables {
    queue = {
      additional_namespaces = ["fs2-academic-poc", "fs2-models"]
    }
  }

  assert {
    condition = (
      kubernetes_manifest.cpu_cluster_queue.manifest.spec.namespaceSelector.matchExpressions[0].key ==
      "kubernetes.io/metadata.name" &&
      kubernetes_manifest.cpu_cluster_queue.manifest.spec.namespaceSelector.matchExpressions[0].operator ==
      "In" &&
      jsonencode(kubernetes_manifest.cpu_cluster_queue.manifest.spec.namespaceSelector.matchExpressions[0].values) ==
      jsonencode(["fs2-academic-poc", "fs2-models", "fs2-reference-data"])
    )
    error_message = "The reference-data ClusterQueue must admit its own namespace and every namespace with a published LocalQueue."
  }
}

run "replacement_credential_identity_gets_a_new_immutable_secret_name" {
  command = apply

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    object_storage_access = {
      access_key_id       = "replacement-access-key"
      secret_reference_id = "replacement-secret"
      revision            = 1
    }
  }

  assert {
    condition = (
      terraform_data.region_contract.output.object_storage_secret == "fs2-reference-data-object-storage-${substr(sha256(jsonencode({
        access_key_id       = "replacement-access-key"
        secret_reference_id = "replacement-secret"
        revision            = 1
      })), 0, 12)}" &&
      terraform_data.region_contract.output.object_storage_secret != "fs2-reference-data-object-storage-${substr(sha256(jsonencode({
        access_key_id       = "accesskey-test"
        secret_reference_id = "secret-test"
        revision            = 1
      })), 0, 12)}"
    )
    error_message = "Replacing a zero-based Nebius access key must select a new immutable Kubernetes Secret even when both provider resource versions are zero."
  }
}

run "same_credential_ids_with_a_new_revision_get_a_new_immutable_secret_name" {
  command = apply

  plan_options {
    target = [terraform_data.region_contract]
  }

  variables {
    object_storage_access = {
      access_key_id       = "accesskey-test"
      secret_reference_id = "secret-test"
      revision            = 2
    }
  }

  assert {
    condition = (
      terraform_data.region_contract.output.object_storage_secret == "fs2-reference-data-object-storage-${substr(sha256(jsonencode({
        access_key_id       = "accesskey-test"
        secret_reference_id = "secret-test"
        revision            = 2
      })), 0, 12)}" &&
      terraform_data.region_contract.output.object_storage_secret != "fs2-reference-data-object-storage-${substr(sha256(jsonencode({
        access_key_id       = "accesskey-test"
        secret_reference_id = "secret-test"
        revision            = 1
      })), 0, 12)}"
    )
    error_message = "Incrementing the Nebius revision must replace the immutable Kubernetes Secret even when both cloud credential IDs remain unchanged."
  }
}

run "every_reference_policy_is_scoped_away_from_database_pods" {
  command = plan

  variables {
    object_storage_egress_cidrs = ["10.200.0.0/16"]
    object_storage_egress_fqdns = ["storage.eu-north1.nebius.cloud"]
    allow_public_source_staging = true
    allow_public_msa_opt_in     = true
  }

  plan_options {
    target = [
      kubernetes_network_policy_v1.default_deny,
      kubernetes_network_policy_v1.dns,
      kubernetes_network_policy_v1.private_object_storage,
      kubernetes_manifest.private_object_storage_fqdn,
      kubernetes_network_policy_v1.public_source_staging,
      kubernetes_network_policy_v1.public_msa_opt_in,
      kubernetes_network_policy_v1.status_ingress,
    ]
  }

  assert {
    condition = alltrue([
      kubernetes_network_policy_v1.default_deny.metadata[0].namespace == "fs2-reference-data",
      kubernetes_network_policy_v1.dns.metadata[0].namespace == "fs2-reference-data",
      kubernetes_network_policy_v1.private_object_storage[0].metadata[0].namespace == "fs2-reference-data",
      kubernetes_manifest.private_object_storage_fqdn[0].manifest.metadata.namespace == "fs2-reference-data",
      kubernetes_network_policy_v1.public_source_staging[0].metadata[0].namespace == "fs2-reference-data",
      kubernetes_network_policy_v1.public_msa_opt_in[0].metadata[0].namespace == "fs2-reference-data",
      kubernetes_network_policy_v1.status_ingress[0].metadata[0].namespace == "fs2-reference-data",
    ])
    error_message = "Every reference-data policy must be namespaced to fs2-reference-data, so no selector can match a CloudNativePG pod in fs2-data."
  }
}

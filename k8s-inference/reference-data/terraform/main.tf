locals {
  common_labels = {
    "app.kubernetes.io/name"       = "fs2-reference-data"
    "app.kubernetes.io/part-of"    = "fs2-serve"
    "app.kubernetes.io/managed-by" = "terraform"
  }
  tools_sha256          = filesha256("${path.module}/../reference_data.py")
  tools_config_map      = "fs2-reference-data-tools-${substr(local.tools_sha256, 0, 12)}"
  source_catalog        = jsondecode(file("${path.module}/../source-catalog.json"))
  source_catalog_sha256 = filesha256("${path.module}/../source-catalog.json")
  selected_bundle       = local.source_catalog.bundles[var.pipeline.bundle_id]
  pipeline_command = [
    "python", "/opt/fs2/reference-data/reference_data.py", "stage",
    "--catalog", "/etc/fs2-stage/catalog.json",
    "--bundle", var.pipeline.bundle_id,
    "--root", "/reference-data",
    "--object-store-prefix", "s3://${var.object_bucket_name}/reference-data",
  ]
  pipeline_resources = {
    requests = {
      cpu                 = var.pipeline.cpu
      memory              = var.pipeline.memory
      "ephemeral-storage" = var.pipeline.ephemeral_storage
    }
    limits = {
      cpu                 = var.pipeline.cpu
      memory              = var.pipeline.memory
      "ephemeral-storage" = var.pipeline.ephemeral_storage
    }
  }
  status_resources = {
    requests = { cpu = "50m", memory = "64Mi", "ephemeral-storage" = "64Mi" }
    limits   = { cpu = "250m", memory = "256Mi", "ephemeral-storage" = "256Mi" }
  }
  pipeline_cpu_millicores = endswith(var.pipeline.cpu, "m") ? tonumber(trimsuffix(var.pipeline.cpu, "m")) : tonumber(var.pipeline.cpu) * 1000
  pipeline_memory_parts   = regex("^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$", var.pipeline.memory)
  pipeline_memory_mib     = tonumber(local.pipeline_memory_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.pipeline_memory_parts[1])
  pipeline_ephemeral_parts = regex(
    "^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$",
    var.pipeline.ephemeral_storage,
  )
  pipeline_ephemeral_mib = tonumber(local.pipeline_ephemeral_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.pipeline_ephemeral_parts[1])
  queue_cpu_millicores   = endswith(var.queue.nominal_cpu, "m") ? tonumber(trimsuffix(var.queue.nominal_cpu, "m")) : tonumber(var.queue.nominal_cpu) * 1000
  queue_memory_parts     = regex("^([1-9][0-9]*)(Ki|Mi|Gi|Ti)$", var.queue.nominal_memory)
  queue_memory_mib       = tonumber(local.queue_memory_parts[0]) * lookup({ Ki = 1 / 1024, Mi = 1, Gi = 1024, Ti = 1048576 }, local.queue_memory_parts[1])
  required_capacity = {
    cpu_millicores = (
      (var.pipeline.enabled ? local.pipeline_cpu_millicores : 0) +
      (var.status.enabled ? 50 * var.status.replicas : 0)
    )
    memory_mib = (
      (var.pipeline.enabled ? local.pipeline_memory_mib : 0) +
      (var.status.enabled ? 64 * var.status.replicas : 0)
    )
    ephemeral_storage_mib = (
      (var.pipeline.enabled ? local.pipeline_ephemeral_mib : 0) +
      (var.status.enabled ? 64 * var.status.replicas : 0)
    )
  }
  total_schedulable_capacity = {
    for resource, capacity in var.cpu_pool.schedulable_capacity :
    resource => capacity * var.cpu_pool.node_count
  }
  pipeline_tolerations = [{
    key      = var.cpu_pool.taint.key
    operator = "Equal"
    value    = var.cpu_pool.taint.value
    effect   = var.cpu_pool.taint.effect
  }]
  pipeline_catalog_name = "fs2-stage-af3-catalog-${substr(sha256(jsonencode({
    bundle_id = var.pipeline.bundle_id
    catalog   = local.source_catalog_sha256
  })), 0, 12)}"
  pipeline_pod_template = {
    metadata = {
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"               = "reference-data-stager"
        "reference-data.fs2.nebius.ai/bundle"       = var.pipeline.bundle_id
        "reference-data.fs2.nebius.ai/network-mode" = "public-source-staging"
      })
    }
    spec = {
      restartPolicy                = "Never"
      serviceAccountName           = "fs2-reference-data"
      automountServiceAccountToken = false
      enableServiceLinks           = false
      nodeSelector                 = var.cpu_pool.node_labels
      tolerations                  = local.pipeline_tolerations
      securityContext = {
        runAsNonRoot        = true
        runAsUser           = 1000
        runAsGroup          = 1000
        fsGroup             = 1000
        fsGroupChangePolicy = "OnRootMismatch"
        seccompProfile      = { type = "RuntimeDefault" }
      }
      containers = [{
        name            = "stager"
        image           = var.pipeline.image
        imagePullPolicy = "IfNotPresent"
        command         = local.pipeline_command
        env = [
          {
            name = "AWS_ACCESS_KEY_ID"
            valueFrom = {
              secretKeyRef = {
                name = "fs2-reference-data-object-storage"
                key  = "access-key-id"
              }
            }
          },
          {
            name = "AWS_SECRET_ACCESS_KEY"
            valueFrom = {
              secretKeyRef = {
                name = "fs2-reference-data-object-storage"
                key  = "secret-access-key"
              }
            }
          },
          { name = "AWS_ENDPOINT_URL", value = "https://storage.${var.object_storage_region}.nebius.cloud" },
          { name = "AWS_DEFAULT_REGION", value = var.object_storage_region },
          { name = "HOME", value = "/work" },
        ]
        resources = local.pipeline_resources
        securityContext = {
          allowPrivilegeEscalation = false
          readOnlyRootFilesystem   = true
          capabilities             = { drop = ["ALL"] }
        }
        volumeMounts = [
          { name = "reference-data", mountPath = "/reference-data" },
          { name = "tools", mountPath = "/opt/fs2/reference-data", readOnly = true },
          { name = "catalog", mountPath = "/etc/fs2-stage", readOnly = true },
          { name = "work", mountPath = "/work" },
        ]
      }]
      volumes = [
        {
          name = "reference-data"
          hostPath = {
            path = var.shared_filesystem_host_path
            type = "Directory"
          }
        },
        {
          name = "tools"
          configMap = {
            name        = local.tools_config_map
            defaultMode = 365
          }
        },
        {
          name = "catalog"
          configMap = {
            name        = local.pipeline_catalog_name
            defaultMode = 292
          }
        },
        {
          name = "work"
          emptyDir = {
            sizeLimit = var.pipeline.ephemeral_storage
          }
        },
      ]
    }
  }
  # This secret-free value is the complete rendered Job input contract. The
  # Job name changes whenever any pod-template or job-level field changes,
  # avoiding an in-place batch/v1 Job update that Kubernetes must reject.
  pipeline_job_contract = {
    namespace  = var.namespace
    generation = var.pipeline.generation
    metadata = {
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"               = "reference-data-stager"
        "kueue.x-k8s.io/queue-name"                 = var.queue.local_queue
        "kueue.x-k8s.io/priority-class"             = "batch"
        "reference-data.fs2.nebius.ai/bundle"       = var.pipeline.bundle_id
        "reference-data.fs2.nebius.ai/network-mode" = "public-source-staging"
      })
      annotations = {
        "reference-data.fs2.nebius.ai/catalog-sha256" = local.source_catalog_sha256
        "reference-data.fs2.nebius.ai/resumable"      = "true"
        "reference-data.fs2.nebius.ai/checksums"      = "source-identity,sha256,tree-sha256"
      }
    }
    spec = {
      suspend                 = true
      backoffLimit            = var.pipeline.backoff_limit
      activeDeadlineSeconds   = var.pipeline.active_deadline_seconds
      ttlSecondsAfterFinished = 604800
      template                = local.pipeline_pod_template
    }
  }
  pipeline_identity = substr(sha256(jsonencode({
    bundle_id = var.pipeline.bundle_id
    revision  = local.selected_bundle.revision
    catalog   = local.source_catalog_sha256
    job       = local.pipeline_job_contract
  })), 0, 12)
  object_bucket_name  = var.object_bucket_name
  object_endpoint     = "https://storage.${var.object_storage_region}.nebius.cloud"
  object_prefix       = "s3://${local.object_bucket_name}/reference-data"
  filesystem_file_uri = "file://${var.shared_filesystem_host_path}"
  credentials_secret  = "fs2-reference-data-object-storage"
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
    precondition {
      condition = (
        !var.pipeline.enabled ||
        (
          var.allow_public_source_staging &&
          local.selected_bundle.access.staging_policy == "automatic-public" &&
          local.selected_bundle.upstream.revision == "231efc9bb9c13b45cc59e43f7107869084ee9624" &&
          local.selected_bundle.upstream.source_sha256 == "152b5a1a2af4c3128f9618939adec8f0389b604ddc767e3de81ccb8a6dc00d19"
        )
      )
      error_message = "the official AlphaFold3 staging pipeline requires explicit public-source egress and the reviewed pinned upstream commit/script digest."
    }
    precondition {
      condition = (
        (!var.pipeline.enabled || (
          local.pipeline_cpu_millicores <= var.cpu_pool.schedulable_capacity.cpu_millicores &&
          local.pipeline_memory_mib <= var.cpu_pool.schedulable_capacity.memory_mib &&
          local.pipeline_ephemeral_mib <= var.cpu_pool.schedulable_capacity.ephemeral_storage_mib &&
          local.pipeline_cpu_millicores <= local.queue_cpu_millicores &&
          local.pipeline_memory_mib <= local.queue_memory_mib
        )) &&
        local.queue_cpu_millicores <= local.total_schedulable_capacity.cpu_millicores &&
        local.queue_memory_mib <= local.total_schedulable_capacity.memory_mib &&
        local.required_capacity.cpu_millicores <= local.total_schedulable_capacity.cpu_millicores &&
        local.required_capacity.memory_mib <= local.total_schedulable_capacity.memory_mib &&
        local.required_capacity.ephemeral_storage_mib <= local.total_schedulable_capacity.ephemeral_storage_mib
      )
      error_message = "pipeline/status requests and Kueue quotas exceed the declared schedulable capacity of the dedicated tainted CPU preprocessing pool; the system node is not fallback capacity."
    }
  }
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "object_storage" {
  secret_id = var.object_storage_access.secret_reference_id
  key       = "secret"
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

resource "kubernetes_secret_v1" "object_storage" {
  metadata {
    name      = local.credentials_secret
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels    = local.common_labels
  }
  type = "Opaque"
  data_wo = {
    "access-key-id"     = var.object_storage_access.access_key_id
    "secret-access-key" = ephemeral.nebius_mysterybox_v1_secret_payload_entry.object_storage.data.string_value
  }
  data_wo_revision = var.object_storage_access.revision
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
      nodeLabels = var.cpu_pool.node_labels
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

resource "kubernetes_manifest" "private_object_storage_fqdn" {
  count = length(var.object_storage_egress_fqdns) > 0 ? 1 : 0
  manifest = {
    apiVersion = "cilium.io/v2"
    kind       = "CiliumNetworkPolicy"
    metadata = {
      name      = "private-msa-object-storage-fqdn"
      namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
      labels    = local.common_labels
    }
    spec = {
      endpointSelector = {
        matchLabels = {
          "app.kubernetes.io/component"               = "private-msa"
          "reference-data.fs2.nebius.ai/network-mode" = "private-only"
        }
      }
      egress = [{
        toFQDNs = [for name in sort(tolist(var.object_storage_egress_fqdns)) : {
          matchName = name
        }]
        toPorts = [{
          ports = [{ port = "443", protocol = "TCP" }]
        }]
      }]
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

resource "kubernetes_config_map_v1" "pipeline_catalog" {
  count = var.pipeline.enabled ? 1 : 0
  metadata {
    name      = local.pipeline_catalog_name
    namespace = kubernetes_namespace_v1.reference_data.metadata[0].name
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "reference-data-stager"
    })
    annotations = {
      "reference-data.fs2.nebius.ai/catalog-sha256"  = local.source_catalog_sha256
      "reference-data.fs2.nebius.ai/upstream-commit" = local.selected_bundle.upstream.revision
    }
  }
  immutable = true
  data = {
    "catalog.json" = jsonencode({
      schema       = local.source_catalog.schema
      generated_at = local.source_catalog.generated_at
      bundles = {
        (var.pipeline.bundle_id) = local.selected_bundle
      }
    })
  }
}

resource "kubernetes_manifest" "pipeline" {
  count = var.pipeline.enabled ? 1 : 0
  manifest = {
    apiVersion = "batch/v1"
    kind       = "Job"
    metadata = {
      name        = "fs2-stage-af3-${local.pipeline_identity}"
      namespace   = kubernetes_namespace_v1.reference_data.metadata[0].name
      labels      = local.pipeline_job_contract.metadata.labels
      annotations = local.pipeline_job_contract.metadata.annotations
    }
    spec = local.pipeline_job_contract.spec
  }
  computed_fields = [
    "metadata.annotations",
    "metadata.labels",
    "spec.suspend",
  ]

  depends_on = [
    kubernetes_manifest.local_queue,
    kubernetes_network_policy_v1.public_source_staging,
  ]
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
        node_selector                   = var.cpu_pool.node_labels
        toleration {
          key      = var.cpu_pool.taint.key
          operator = "Equal"
          value    = var.cpu_pool.taint.value
          effect   = var.cpu_pool.taint.effect
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
            requests = local.status_resources.requests
            limits   = local.status_resources.limits
          }
          readiness_probe {
            http_get {
              path = "/healthz"
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

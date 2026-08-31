locals {
  control_plane_overrides = {
    replicaCount = 2
    image = {
      repository = var.control_plane_image.repository
      digest     = var.control_plane_image.digest
      pullPolicy = "IfNotPresent"
    }
    catalog = {
      delivery                          = "image"
      imagePath                         = "/opt/fs2/catalog"
      bindingsConfigMapName             = kubernetes_config_map_v1.serving_bindings.metadata[0].name
      rolloutDigest                     = var.catalog_rollout_digest
      evidencePersistentVolumeClaimName = "unused-with-lean-routes"
      persistentVolumeClaimName         = "unused-with-image-delivery"
      leanRoutes = {
        enabled       = true
        configMapName = kubernetes_config_map_v1.lean_routes.metadata[0].name
        key           = "lean-routes.json"
      }
    }
    config = merge({
      publicBaseUrl          = local.public_base_url
      publicAuthorityMode    = local.public_edge_enabled ? "ip" : "dns"
      authorizationServerUrl = local.public_base_url
      allowNonClusterUrls    = !local.public_edge_enabled
      otlpEndpoint           = "http://fs2-otel-gateway.fs2-observability.svc.cluster.local:4318/v1/traces"
      syncWaitSeconds        = "30"
      maxSyncWaitSeconds     = "30"
      }, var.model_scaling_mode == "keda" ? {
      activationTimeoutSeconds = "7200"
    } : {})
    networkPolicy = {
      kubernetesApiCidrs = sort(tolist(local.kubernetes_api_service_cidrs))
      dns                = { podLabels = { "k8s-app" = "coredns" } }
      prometheus = {
        namespaceLabels = { "kubernetes.io/metadata.name" = "fs2-observability" }
        podLabels = {
          "app.kubernetes.io/instance" = "fs2-${var.run_id}-monitoring-prometheus"
          "app.kubernetes.io/name"     = "prometheus"
        }
        port = 9090
      }
      runtime = {
        namespaceLabels = { "kubernetes.io/metadata.name" = "fs2-models" }
        podLabels = {
          "app.kubernetes.io/component" = "model-runtime"
          "app.kubernetes.io/part-of"   = "fs2-serve"
        }
        ports = [8000]
      }
      otlp = {
        namespaceLabels = { "kubernetes.io/metadata.name" = "fs2-observability" }
        podLabels = {
          "app.kubernetes.io/instance" = "fs2-${var.run_id}-otel-gateway"
          "app.kubernetes.io/name"     = "opentelemetry-collector"
        }
        port = 4318
      }
    }
    adminReadAdapters = {
      capacity = {
        enabled = true
      }
      observability = {
        enabled       = true
        prometheusUrl = local.prometheus_server_address
        links = {
          allowedHosts = sort(tolist(var.admin_observability_links.allowed_hosts))
          grafana = {
            url                   = var.admin_observability_links.grafana.url
            verifiedExternalRoute = var.admin_observability_links.grafana.verified_external_route
          }
          prometheus = {
            url                   = var.admin_observability_links.prometheus.url
            verifiedExternalRoute = var.admin_observability_links.prometheus.verified_external_route
          }
          loki = {
            url                   = var.admin_observability_links.loki.url
            verifiedExternalRoute = var.admin_observability_links.loki.verified_external_route
          }
        }
      }
    }
    publicLoadBalancer = {
      enabled               = local.public_edge_enabled
      targetProjectId       = var.project_id
      allocationProjectId   = local.public_edge_enabled ? var.public_edge_contract.allocation_project_id : ""
      allocationType        = "public-ipv4"
      allocationId          = local.public_edge_enabled ? var.public_edge_contract.allocation_id : ""
      externalTrafficPolicy = "Cluster"
    }
    publicTls = {
      enabled   = local.public_edge_enabled
      ipAddress = local.public_edge_enabled ? var.public_edge_contract.public_ipv4_address : ""
      issuerRef = { name = "fs2-serve-ip-acme-staging", kind = "Issuer", group = "cert-manager.io" }
      acmeIssuer = {
        enabled                     = local.public_edge_enabled
        name                        = "fs2-serve-ip-acme-staging"
        environment                 = "staging"
        email                       = local.public_edge_enabled ? var.acme_email : ""
        accountPrivateKeySecretName = "fs2-serve-ip-acme-staging-account"
        profile                     = "shortlived"
      }
    }
    publicGateway = { enabled = local.public_edge_enabled }
    httpRoute = {
      enabled       = local.public_edge_enabled
      authorityMode = local.public_edge_enabled ? "ip" : "dns"
    }
    grafanaDashboard = { enabled = true }
    serviceMonitor = merge({ enabled = true }, var.model_scaling_mode == "keda" ? {
      interval = "5s"
    } : {})
    prometheusRule = { enabled = true }
    nodeSelector = {
      "workload.fs2.nebius/system" = "true"
      "capacity.fs2.nebius/type"   = "regular"
      "capacity.fs2.nebius/pool"   = "system"
    }
  }
}

resource "helm_release" "control_plane" {
  name             = "fs2-serve-control-plane"
  namespace        = "fs2-system"
  chart            = "${local.fs2_root}/charts/control-plane/fs2-serve-control-plane"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true
  timeout          = 1800

  values = [
    file("${local.fs2_root}/charts/control-plane/control-plane.values.yaml"),
    yamlencode(local.control_plane_overrides),
    yamlencode(local.admin_control_plane_overrides),
  ]

  depends_on = [
    kubernetes_manifest.control_database,
    kubernetes_secret_v1.database_consumer,
    kubernetes_secret_v1.grafana_datasource,
    kubernetes_secret_v1.payload_keyring,
    kubernetes_secret_v1.ledger_keyring,
    kubernetes_secret_v1.token_pepper,
    kubernetes_secret_v1.route_attestors,
    kubernetes_secret_v1.admin,
    kubernetes_config_map_v1.serving_bindings,
    kubernetes_config_map_v1.lean_routes,
    kubernetes_config_map_v1.admin_configuration,
    kubernetes_manifest.model,
  ]
}

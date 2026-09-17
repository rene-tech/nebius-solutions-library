locals {
  acme_issuer_name      = "fs2-serve-ip-acme-${var.acme_environment}"
  admin_model_namespace = "fs2-models"
  control_plane_schema_compatibility_image_ref = "${var.control_plane_schema_compatibility_image.repository}@${var.control_plane_schema_compatibility_image.digest}"
  # Read exactly the namespaces from the validated scheduling contract,
  # including operator-defined and externally-owned scientific queues. The
  # reference-data and batch owners can also place Pods outside that contract,
  # so include their enabled execution namespaces explicitly. The model
  # namespace is already covered by the base capacity-reader Role.
  admin_scientific_namespaces = sort(distinct([
    for namespace in concat(
      [
        for queue in values(module.kueue_scheduling.contract.local_queues) :
        queue.metadata.namespace
      ],
      var.scientific_batch.enabled ? [var.scientific_batch.namespace] : [],
      var.reference_data.enabled ? [var.reference_data.namespace] : [],
    ) : namespace
    if namespace != local.admin_model_namespace
  ]))
  runtime_attribution_namespaces = sort(distinct(concat(
    [local.admin_model_namespace],
    local.admin_scientific_namespaces,
  )))
  control_plane_overrides = {
    replicaCount = 2
    autoscaling = {
      enabled                        = var.control_plane_autoscaling.enabled
      minReplicas                    = var.control_plane_autoscaling.min_replicas
      maxReplicas                    = var.control_plane_autoscaling.max_replicas
      targetCPUUtilizationPercentage = var.control_plane_autoscaling.target_cpu_utilization_percentage
    }
    rolloutStrategy = {
      maxUnavailable = var.control_plane_rollout.max_unavailable
      maxSurge       = var.control_plane_rollout.max_surge
    }
    image = {
      repository = var.control_plane_image.repository
      digest     = var.control_plane_image.digest
      pullPolicy = "IfNotPresent"
    }
    # This image is intentionally independent from the application image. A
    # source-forward rollback may select an older gateway, but migration and
    # wait-schema must continue using the newest schema-compatible binary.
    migration = {
      compatibilityImage = {
        repository = var.control_plane_schema_compatibility_image.repository
        digest     = var.control_plane_schema_compatibility_image.digest
      }
    }
    # Publish the tenant-private academic delivery contract to the chart so a model
    # runtime learns which claim to mount, where, and which group grants read
    # access. The control plane itself never mounts licensed bytes.
    academicAssets = local.academic_chart_values
    catalog = {
      delivery                          = "image"
      imagePath                         = "/opt/fs2/catalog"
      bindingsConfigMapName             = kubernetes_config_map_v1.serving_bindings.metadata[0].name
      rolloutDigest                     = var.catalog_rollout_digest
      evidencePersistentVolumeClaimName = "unused-with-lean-routes"
      persistentVolumeClaimName         = "unused-with-image-delivery"
      leanRoutes = {
        enabled            = true
        configMapName      = kubernetes_config_map_v1.lean_routes.metadata[0].name
        key                = "lean-routes.json"
        deploymentRuntimes = true
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
      requestDebugEnabled    = var.request_debug_enabled
      adminSessionTrustedProxyCidrs = local.public_edge_enabled ? sort(tolist(var.admin_session_trusted_proxy_cidrs)) : []
      }, var.model_scaling_mode == "keda" ? {
      activationTimeoutSeconds = "7200"
    } : {})
    networkPolicy = {
      kubernetesApiCidrs = sort(tolist(local.kubernetes_api_egress_cidrs))
      artifactStoreCidrs = sort(tolist(var.scientific_artifacts.egress_cidrs))
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
        ports = local.selected_runtime_ports
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
      kubernetesCacheTtlSeconds = "15"
      context = {
        project = nonsensitive(var.project_id)
        cluster = var.cluster_name
        region  = local.selected_target.region
        label   = var.cluster_name
      }
      capacity = {
        enabled            = true
        nodeScalerProvider = local.admin_configuration_enabled ? "nebius-managed-node-group-autoscaler" : ""
        # Scientific lanes live outside the model namespace, so the admin
        # capacity reader is told about exactly the ones this stage enables.
        # Without them its queue and GPU-allocation projections silently omit
        # the academic and reference-data workloads.
        kueueExtraNamespaces = local.admin_scientific_namespaces
      }
      observability = {
        enabled       = true
        prometheusUrl = local.prometheus_server_address
        lokiUrl       = local.grafana_loki_datasource_url
        installed = {
          alertmanager = local.observability_operator.alertmanager.enabled
          tempo        = local.observability_operator.tempo.enabled
        }
        datasourceUids = {
          alertmanager = coalesce(local.observability_operator.alertmanager.grafana_datasource_uid, "")
          tempo        = local.observability_operator.tempo.grafana_datasource_uid
        }
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
          otel = {
            url                   = var.admin_observability_links.otel.url
            verifiedExternalRoute = var.admin_observability_links.otel.verified_external_route
          }
          dcgm = {
            url                   = var.admin_observability_links.dcgm.url
            verifiedExternalRoute = var.admin_observability_links.dcgm.verified_external_route
          }
          kueue = {
            url                   = var.admin_observability_links.kueue.url
            verifiedExternalRoute = var.admin_observability_links.kueue.verified_external_route
          }
          keda = {
            url                   = var.admin_observability_links.keda.url
            verifiedExternalRoute = var.admin_observability_links.keda.verified_external_route
          }
          alertmanager = {
            url                   = var.admin_observability_links.alertmanager.url
            verifiedExternalRoute = var.admin_observability_links.alertmanager.verified_external_route
          }
          tempo = {
            url                   = var.admin_observability_links.tempo.url
            verifiedExternalRoute = var.admin_observability_links.tempo.verified_external_route
          }
        }
      }
    }
    # Publish exact kubelet Pod -> GPU UUID observations for the lifecycle
    # ledger. This is GPU-model agnostic and schedules only on Nebius GPU nodes.
    runtimeAttribution = {
      enabled    = true
      namespaces = local.runtime_attribution_namespaces
    }
    modelController = {
      enabled                             = var.model_controller.enabled
      writesEnabled                       = var.model_controller.writes_enabled
      infrastructureEnvelopeConfigMapName = local.model_controller_envelope_name
      rendererBundlesConfigMapName        = local.model_controller_bundles_name
      prometheusServerAddress             = local.prometheus_server_address
      admission = {
        enabled = var.model_controller.writes_enabled
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
      issuerRef = { name = local.acme_issuer_name, kind = "Issuer", group = "cert-manager.io" }
      acmeIssuer = {
        enabled                     = local.public_edge_enabled
        name                        = local.acme_issuer_name
        environment                 = var.acme_environment
        email                       = local.public_edge_enabled ? var.acme_email : ""
        accountPrivateKeySecretName = "${local.acme_issuer_name}-account"
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

# Helm rollback renders the stored target revision, so a check which exists
# only in the current chart cannot protect a rollback to a pre-check revision.
# This Terraform-owned, append-only admission boundary is installed before the
# release and survives every Helm revision. It refuses both the pre-upgrade
# migration Job and the gateway Deployment unless their schema-sensitive
# containers retain the independently pinned compatibility image.
resource "kubernetes_manifest" "control_plane_schema_compatibility_policy" {
  provider = kubernetes.release_identity
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = "fs2-control-plane-schema-compatibility"
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["deployments"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobs"]
            scope       = "Namespaced"
          },
        ]
      }
      matchConditions = [{
        name       = "schema-sensitive-control-plane-workload"
        expression = "request.namespace == 'fs2-system' && ((request.resource.group == 'apps' && request.resource.resource == 'deployments' && request.name == 'fs2-serve-control-plane') || (request.resource.group == 'batch' && request.resource.resource == 'jobs' && request.name == 'fs2-serve-control-plane-migrate'))"
      }]
      validations = [{
        expression = "(object.kind != 'Deployment' || (has(object.spec.template.spec.initContainers) && object.spec.template.spec.initContainers.filter(container, container.name == 'wait-schema' && container.image == ${jsonencode(local.control_plane_schema_compatibility_image_ref)}).size() == 1)) && (object.kind != 'Job' || object.spec.template.spec.containers.filter(container, container.name == 'migrate' && container.image == ${jsonencode(local.control_plane_schema_compatibility_image_ref)}).size() == 1)"
        message    = "control-plane migration and schema-wait containers must use the Terraform-pinned schema compatibility image; rollback to an application-image fallback is refused"
      }]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_epoch_router_lifecycle_binding,
  ]
}

resource "kubernetes_manifest" "control_plane_schema_compatibility_policy_binding" {
  provider = kubernetes.release_identity
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = "fs2-control-plane-schema-compatibility"
      labels = local.common_labels
    }
    spec = {
      policyName        = "fs2-control-plane-schema-compatibility"
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.control_plane_schema_compatibility_policy]
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
    yamlencode(local.bootstrap_access_overrides),
    yamlencode(local.scientific_access_overrides),
    yamlencode(local.scientific_chart_overrides),
  ]

  lifecycle {
    precondition {
      condition = (
        var.release_identity_kubeconfig_path != "" &&
        var.release_identity_kube_context != "" &&
        abspath(var.release_identity_kubeconfig_path) != abspath(var.kubeconfig_path) &&
        var.release_identity_kube_context != var.kube_context
      )
      error_message = "The schema-compatibility admission guard requires the separately custodied release-identity provider; the general run provider is refused."
    }

    precondition {
      condition     = local.release_identity_admission_bundle_valid
      error_message = "Helm is refused until the external security-owned admission receipt pins the exact schema-compatibility guard, stable bootstrap boundary, and every configured generation policy by canonical manifest, UID, and full provider object."
    }

    precondition {
      condition = local.public_edge_enabled ? (
        length(var.admin_session_trusted_proxy_cidrs) > 0
        ) : (
        length(var.admin_session_trusted_proxy_cidrs) == 0
      )
      error_message = "Public-edge mode requires at least one exact Envoy backend-peer CIDR for operator-login source attribution; internal-only mode must use the direct peer and leave admin_session_trusted_proxy_cidrs empty."
    }

    precondition {
      condition = (
        local.observability_operator.schema == "fs2-serve.nebius.ai/observability-operator/v1" &&
        local.observability_operator.tempo.enabled &&
        local.observability_operator.tempo.service_port == 3200 &&
        length(local.observability_operator.tempo.grafana_datasource_uid) > 0 &&
        local.observability_operator.alertmanager.service_port == 9093 &&
        (
          !local.observability_operator.alertmanager.enabled ||
          try(length(local.observability_operator.alertmanager.grafana_datasource_uid) > 0, false)
        ) &&
        !local.observability_operator.raw_backends_public &&
        local.observability_operator.operator_surface == "grafana-native-auth"
      )
      error_message = "The foundation observability handoff must retain private raw backends and the reviewed Grafana-native Alertmanager/Tempo operator surface."
    }

    precondition {
      condition     = length(local.admin_scientific_namespaces) <= 32
      error_message = "The admin capacity projection supports at most 32 distinct non-model queue namespaces; reduce or consolidate scheduling.local_queues, scientific_batch.namespace, and reference_data.namespace."
    }

    precondition {
      condition     = length(local.runtime_attribution_namespaces) <= 32
      error_message = "Runtime GPU attribution supports at most 32 distinct model and scientific workload namespaces."
    }
  }

  depends_on = [
    kubernetes_manifest.control_plane_schema_compatibility_policy_binding,
    kubernetes_manifest.model_deployment_crd,
    kubernetes_manifest.control_database,
    kubernetes_secret_v1.database_consumer,
    kubernetes_secret_v1.grafana_datasource,
    kubernetes_secret_v1.payload_keyring,
    kubernetes_secret_v1.ledger_keyring,
    kubernetes_secret_v1.token_pepper,
    kubernetes_secret_v1.route_attestors,
    kubernetes_secret_v1.admin,
    kubernetes_secret_v1.bootstrap_access,
    kubernetes_secret_v1.scientific_access,
    kubernetes_secret_v1.scientific_artifact_store,
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache,
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache_additional,
    kubernetes_job_v1.scientific_runtime_cache_bootstrap,
    kubernetes_job_v1.scientific_runtime_cache_bootstrap_additional,
    terraform_data.scientific_artifacts_contract,
    kubernetes_config_map_v1.serving_bindings,
    kubernetes_config_map_v1.lean_routes,
    kubernetes_config_map_v1.admin_configuration,
    kubernetes_config_map_v1.model_controller_envelope,
    kubernetes_config_map_v1.model_controller_bundles,
    kubernetes_persistent_volume_claim_v1.fast_start_compile_cache,
    kubernetes_persistent_volume_claim_v1.fast_start_residency_receipt,
    kubernetes_persistent_volume_claim_v1.scientific_snapshots,
    kubernetes_config_map_v1.scientific_snapshot_sources,
    kubernetes_persistent_volume_claim_v1.serving_snapshots,
    kubernetes_config_map_v1.serving_snapshot_sources,
    kubernetes_config_map_v1.scientific_scheduling_contract,
    kubernetes_manifest.model,
    # The chart renders capacity-reader Roles in every scientific queue
    # namespace. Order Helm after the owners that create those namespaces and
    # their LocalQueues so an atomic install cannot race namespace creation.
    module.academic_assets,
    module.reference_data,
    kubernetes_manifest.additional_local_queue,
    kubernetes_manifest.general_cpu_local_queue,
    kubernetes_manifest.model_local_queue,
  ]
}

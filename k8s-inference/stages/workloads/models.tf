resource "kubernetes_manifest" "model" {
  for_each = local.model_manifests

  manifest = each.value.manifest

  # The manifest supplies a stable zero bootstrap. The ScaledObject establishes
  # the configured hot floor, then KEDA's generated HPA owns changes through
  # the Deployment scale subresource. An explicit list replaces the provider's
  # default computed metadata maps. Provider 3.2.1 compares annotations at map
  # granularity on apply, so the controller-owned revision requires the whole
  # annotations map to remain computed alongside the HPA-owned replica count.
  computed_fields = each.value.autoscaled ? [
    "metadata.annotations",
    "spec.replicas",
  ] : null

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-models"
  }

  lifecycle {
    precondition {
      condition     = local.model_placement_validations[each.key]
      error_message = "GPU Deployment ${each.value.manifest.metadata.name} must resolve through an explicit fixture binding whose GPU count, host architecture, selection mode, pod constraints, and tolerations match every compatible pool."
    }
  }

  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_secret_v1.ngc_api_key,
    kubernetes_secret_v1.nvcrio_cred,
    helm_release.dcgm_exporter,
  ]
}

resource "kubernetes_manifest" "model_scaler" {
  for_each = local.model_scalers

  manifest = {
    apiVersion = "keda.sh/v1alpha1"
    kind       = "ScaledObject"
    metadata = {
      name      = "fs2-model-${each.key}"
      namespace = "fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"       = "model-autoscaler"
        "fs2-serve.nebius.ai/model-id"      = each.key
        "fs2-serve.nebius.ai/replica-owner" = "keda"
      })
    }
    spec = {
      scaleTargetRef = {
        apiVersion = "apps/v1"
        kind       = "Deployment"
        name       = each.value.deployment
      }
      pollingInterval = each.value.polling_interval_seconds
      cooldownPeriod  = each.value.cooldown_seconds
      minReplicaCount = each.value.min_replicas
      maxReplicaCount = each.value.max_replicas
      fallback = {
        failureThreshold = var.keda_fallback_failure_threshold
        replicas         = 1
        behavior         = "static"
      }
      # KEDA's default is false. Do not send the optional advanced field:
      # KEDA 2.20 canonicalizes an explicit false to an omitted value, which
      # makes kubernetes_manifest provider 3.2.1 report false -> null as an
      # inconsistent result after an otherwise successful apply.
      triggers = [{
        type       = "prometheus"
        metricType = "AverageValue"
        metadata = {
          serverAddress       = local.prometheus_server_address
          metricName          = each.value.metric_name
          query               = each.value.prometheus_query
          threshold           = tostring(each.value.target_queue_depth)
          activationThreshold = "0"
          ignoreNullValues    = "false"
        }
      }]
    }
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-model-autoscaling"
  }

  wait {
    condition {
      type   = "Ready"
      status = "True"
    }
  }

  timeouts {
    create = "10m"
    update = "10m"
  }

  lifecycle {
    precondition {
      condition     = local.control_plane_overrides.catalog.leanRoutes.enabled
      error_message = "KEDA model scaling requires the activation-disabled lean-route overlay."
    }

    precondition {
      condition     = local.autoscaling_target_document_counts[each.key] == 1
      error_message = "model ${each.key} must map to exactly one Deployment in the selected profile."
    }

    precondition {
      condition     = local.autoscaling_target_gpu_counts[each.key] == each.value.gpu_count
      error_message = "model ${each.key} autoscaling target must match its reviewed GPU count."
    }
  }

  depends_on = [
    kubernetes_manifest.model,
    helm_release.control_plane,
  ]
}

resource "kubernetes_manifest" "cold_start_keeper" {
  for_each = local.keeper_manifests

  manifest = each.value.manifest

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-cold-start"
  }

  depends_on = [terraform_data.cluster_contract]
}

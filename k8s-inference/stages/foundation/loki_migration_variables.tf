variable "loki_migration_acknowledgements" {
  description = "Source-pinned pretransition and posttransition acknowledgements. Authorization also requires a current release-owner projection signed by a source-pinned public trust root after rereading live Helm storage, effective configuration, datasource content, workload inventory and admission custody."
  type = map(object({
    schema = string
    stage  = string
    binding = object({
      namespace        = string
      config_map_name  = string
      uid               = string
      resource_version = string
      record_sha256    = string
    })
    target = object({
      run_id          = string
      cluster_id      = string
      kube_system_uid = string
    })
    source = object({
      commit = string
      tree   = string
    })
    revisions = object({
      loki_helm          = number
      otel_gateway_helm  = number
      control_plane_helm = number
      grafana_helm       = number
    })
    deployments = map(object({
      uid                     = string
      generation              = number
      resource_version        = string
      pod_template_sha256     = string
      container_images_sha256 = string
    }))
    grafana_datasource = object({
      namespace        = string
      name             = string
      uid               = string
      resource_version = string
    })
    freshness_markers = object({
      foundation = object({
        uid              = string
        resource_version = string
        content_sha256   = string
      })
      workloads = object({
        uid              = string
        resource_version = string
        content_sha256   = string
      })
    })
    payload_safety_inventory = object({
      namespace        = string
      name             = string
      uid               = string
      resource_version = string
      inventory_sha256 = string
      permit_sha256    = string
      data_sha256      = string
      image_count      = number
    })
    owner_projection = object({
      namespace          = string
      name               = string
      uid                = string
      resource_version   = string
      projection_sha256  = string
      attestation_sha256 = string
      trust_root = object({
        namespace        = string
        name             = string
        uid              = string
        resource_version = string
        content_sha256   = string
      })
    })
    proof = object({
      observed_at                     = string
      valid_until                     = string
      sealed_evidence_sha256          = string
      marker_sha256                   = string
      auth_enabled                    = bool
      writer_identity                 = string
      writer_scoped_header_configured = bool
      writer_marker_ingested          = bool
      marker_storage_tenant           = string
      grafana_legacy_read             = bool
      grafana_scoped_read             = bool
      control_plane_legacy_read       = bool
      control_plane_scoped_read       = bool
    })
  }))
  default  = {}
  nullable = false

  validation {
    condition = (
      length(setsubtract(toset(keys(var.loki_migration_acknowledgements)), toset(["pretransition", "posttransition"]))) == 0 &&
      alltrue([
        for stage, acknowledgement in var.loki_migration_acknowledgements : try(
          acknowledgement.schema == "fs2-serve.nebius.ai/loki-migration-acknowledgement/v4" &&
          acknowledgement.stage == stage &&
          acknowledgement.binding.namespace == "fs2-observability" &&
          can(regex("^fs2-loki-(?:pretransition|posttransition)-ack-[0-9a-f]{12}$", acknowledgement.binding.config_map_name)) &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.binding.uid)) &&
          length(trimspace(acknowledgement.binding.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.binding.record_sha256)) &&
          can(regex("^[a-z][a-z0-9]{5,11}$", acknowledgement.target.run_id)) &&
          can(regex("^mk8scluster-[a-z0-9]+$", acknowledgement.target.cluster_id)) &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.target.kube_system_uid)) &&
          can(regex("^[0-9a-f]{40}$", acknowledgement.source.commit)) &&
          can(regex("^[0-9a-f]{40}$", acknowledgement.source.tree)) &&
          alltrue([
            for revision in values(acknowledgement.revisions) :
            floor(revision) == revision && revision >= 1
          ]) &&
          toset(keys(acknowledgement.deployments)) == toset(["loki", "otel_gateway", "control_plane", "grafana"]) &&
          alltrue([
            for deployment in values(acknowledgement.deployments) :
            can(regex("^[0-9a-fA-F-]{20,}$", deployment.uid)) &&
            floor(deployment.generation) == deployment.generation &&
            deployment.generation >= 1 &&
            length(trimspace(deployment.resource_version)) >= 1 &&
            can(regex("^[0-9a-f]{64}$", deployment.pod_template_sha256)) &&
            can(regex("^[0-9a-f]{64}$", deployment.container_images_sha256))
          ]) &&
          acknowledgement.grafana_datasource.namespace == "fs2-observability" &&
          acknowledgement.grafana_datasource.name == "fs2-serve-postgres-grafana-datasource" &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.grafana_datasource.uid)) &&
          length(trimspace(acknowledgement.grafana_datasource.resource_version)) >= 1 &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.freshness_markers.foundation.uid)) &&
          length(trimspace(acknowledgement.freshness_markers.foundation.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.freshness_markers.foundation.content_sha256)) &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.freshness_markers.workloads.uid)) &&
          length(trimspace(acknowledgement.freshness_markers.workloads.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.freshness_markers.workloads.content_sha256)) &&
          acknowledgement.payload_safety_inventory.namespace == "fs2-system" &&
          can(regex("^fs2-runtime-log-safety-[0-9a-f]{12}$", acknowledgement.payload_safety_inventory.name)) &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.payload_safety_inventory.uid)) &&
          length(trimspace(acknowledgement.payload_safety_inventory.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.payload_safety_inventory.inventory_sha256)) &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.payload_safety_inventory.permit_sha256)) &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.payload_safety_inventory.data_sha256)) &&
          floor(acknowledgement.payload_safety_inventory.image_count) == acknowledgement.payload_safety_inventory.image_count &&
          acknowledgement.payload_safety_inventory.image_count >= 1 &&
          acknowledgement.owner_projection.namespace == "fs2-observability" &&
          can(regex("^fs2-loki-(?:pretransition|posttransition)-owner-[0-9a-f]{12}$", acknowledgement.owner_projection.name)) &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.owner_projection.uid)) &&
          length(trimspace(acknowledgement.owner_projection.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.owner_projection.projection_sha256)) &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.owner_projection.attestation_sha256)) &&
          acknowledgement.owner_projection.trust_root.namespace == "fs2-system" &&
          acknowledgement.owner_projection.trust_root.name == "fs2-observability-release-attestors" &&
          can(regex("^[0-9a-fA-F-]{20,}$", acknowledgement.owner_projection.trust_root.uid)) &&
          length(trimspace(acknowledgement.owner_projection.trust_root.resource_version)) >= 1 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.owner_projection.trust_root.content_sha256)) &&
          can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", acknowledgement.proof.observed_at)) &&
          can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", acknowledgement.proof.valid_until)) &&
          timecmp(acknowledgement.proof.valid_until, acknowledgement.proof.observed_at) > 0 &&
          timecmp(acknowledgement.proof.valid_until, timeadd(acknowledgement.proof.observed_at, "5m")) <= 0 &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.proof.sealed_evidence_sha256)) &&
          can(regex("^[0-9a-f]{64}$", acknowledgement.proof.marker_sha256)) &&
          acknowledgement.proof.writer_identity == "fs2-otel-gateway" &&
          acknowledgement.proof.writer_scoped_header_configured &&
          acknowledgement.proof.writer_marker_ingested &&
          contains(["fake", "fs2-platform"], acknowledgement.proof.marker_storage_tenant),
          false,
        )
      ])
    )
    error_message = "loki_migration_acknowledgements accepts only strict v4 pretransition/posttransition envelopes bound to exact target, source, revisions, deployments, datasource, complete payload-safety permit data, release-owner projection, public trust-root custody, and a validity window no longer than five minutes."
  }
}

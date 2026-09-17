locals {
  alertmanager_service_name = "fs2-${var.run_id}-monitoring-alertmanager"
  # kube-prometheus-stack owns this stable datasource and its provisioning
  # file. A second sidecar ConfigMap with the same datasource.yaml key races
  # the chart-owned file and can leave the run-scoped UID absent in Grafana.
  alertmanager_grafana_datasource = "alertmanager"
  tempo_service_name              = "fs2-tempo"
  tempo_grafana_datasource        = "fs2-${var.run_id}-tempo"
  loki_legacy_tenant_id           = "fake"
  loki_write_tenant_id            = "fs2-platform"
  loki_read_tenant_header         = "${local.loki_legacy_tenant_id}|${local.loki_write_tenant_id}"
  loki_legacy_retention_hours     = 168
  loki_auth_enforced              = var.loki_access_phase != "network-bound"
  loki_phase_rank = {
    network-bound            = 0
    auth-enforced-validation = 1
    enforced-dual-read       = 2
  }
  loki_client_configuration_payload = {
    schema                      = "fs2-serve.nebius.ai/loki-client-compatibility/v1"
    run_id                      = var.run_id
    write_tenant_id             = local.loki_write_tenant_id
    read_tenant_header          = local.loki_read_tenant_header
    writer_release              = "fs2-${var.run_id}-otel-gateway"
    reader_release              = "fs2-serve-control-plane"
    grafana_datasource_uid      = "fs2-${var.run_id}-loki"
  }
  expected_loki_client_configuration_claim = sha256(jsonencode(local.loki_client_configuration_payload))

  # Auth-off Loki assigns every write to `fake`, even when the writer sends a
  # tenant header. Pretransition evidence therefore proves exact client/image
  # readiness plus legacy reads only. Posttransition evidence is a distinct,
  # later envelope that proves scoped ingestion after auth is already enabled.
  accepted_loki_migration_acknowledgement_sha256 = {
    pretransition  = null
    posttransition = null
  }
  # Public release-owner attestors are provisioned outside both Terraform
  # states. A reviewed successor must pin the exact canonical key-set digest
  # here; a tfvar, acknowledgement, or ConfigMap cannot choose its own signer.
  accepted_observability_release_attestors_sha256 = null
  loki_migration_authorization_intent_records = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => {
      schema                   = acknowledgement.schema
      stage                    = acknowledgement.stage
      target                   = acknowledgement.target
      source                   = acknowledgement.source
      revisions                = acknowledgement.revisions
      deployments              = acknowledgement.deployments
      grafana_datasource       = acknowledgement.grafana_datasource
      freshness_markers        = acknowledgement.freshness_markers
      payload_safety_inventory = acknowledgement.payload_safety_inventory
      proof                    = acknowledgement.proof
    }
  }
  loki_migration_authorization_intent_sha256 = {
    for stage, record in local.loki_migration_authorization_intent_records :
    stage => sha256(jsonencode(record))
  }
  loki_migration_acknowledgement_records = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => {
      schema                   = acknowledgement.schema
      stage                    = acknowledgement.stage
      target                   = acknowledgement.target
      source                   = acknowledgement.source
      revisions                = acknowledgement.revisions
      deployments              = acknowledgement.deployments
      grafana_datasource       = acknowledgement.grafana_datasource
      freshness_markers        = acknowledgement.freshness_markers
      payload_safety_inventory = acknowledgement.payload_safety_inventory
      owner_projection         = acknowledgement.owner_projection
      proof                    = acknowledgement.proof
    }
  }
  loki_migration_acknowledgement_record_json = {
    for stage, record in local.loki_migration_acknowledgement_records :
    stage => jsonencode(record)
  }
  loki_migration_acknowledgement_sha256 = {
    for stage, acknowledgement in var.loki_migration_acknowledgements :
    stage => sha256(jsonencode(acknowledgement))
  }
  loki_migration_acknowledgement_source_accepted = {
    for stage in ["pretransition", "posttransition"] : stage => (
      local.accepted_loki_migration_acknowledgement_sha256[stage] != null &&
      try(
        local.loki_migration_acknowledgement_sha256[stage] == local.accepted_loki_migration_acknowledgement_sha256[stage],
        false,
      )
    )
  }
  loki_active_acknowledgement_stage = (
    var.loki_access_phase == "auth-enforced-validation" ? "pretransition" :
    var.loki_access_phase == "enforced-dual-read" ? "posttransition" : null
  )
  loki_active_acknowledgement = (
    local.loki_active_acknowledgement_stage == null ? null :
    try(var.loki_migration_acknowledgements[local.loki_active_acknowledgement_stage], null)
  )
  loki_active_acknowledgement_source_accepted = (
    local.loki_active_acknowledgement_stage != null &&
    try(local.loki_migration_acknowledgement_source_accepted[local.loki_active_acknowledgement_stage], false)
  )
  loki_active_authorization_records = local.loki_active_acknowledgement_source_accepted ? {
    (local.loki_active_acknowledgement_stage) = local.loki_active_acknowledgement
  } : {}

  # SAI-03 admission/label custody remains independently NO-GO. Do not replace
  # this null with a caller-provided value: a reviewed successor must pin the
  # exact accepted receipt in source before this label-selected policy can be
  # treated as an identity boundary or applied.
  accepted_loki_identity_custody_receipt = null
  loki_identity_custody_ready = (
    local.accepted_loki_identity_custody_receipt != null &&
    var.loki_identity_custody_receipt == local.accepted_loki_identity_custody_receipt
  )

  # Prometheus currently scrapes Loki's metrics on the shared 3100 listener.
  # That exception grants more than an HTTP-path-aware metrics mediator would,
  # so it also requires a distinct independently accepted, source-pinned risk
  # receipt. It remains fail-closed until such a receipt exists.
  accepted_loki_prometheus_health_exception_receipt = null
  loki_prometheus_health_exception_ready = (
    local.accepted_loki_prometheus_health_exception_receipt != null &&
    var.loki_prometheus_health_exception_receipt == local.accepted_loki_prometheus_health_exception_receipt
  )
}

# timestamp() is intentionally unknown during planning. Its persisted output
# makes every authorization data source below apply-time-only, so a saved plan
# cannot reuse a projection or current-state read after the five-minute window.
# Input changes update this guard in place; they do not replace live resources.
resource "terraform_data" "loki_apply_time_authorization" {
  for_each = local.loki_active_authorization_records

  input = {
    authorization_time = timestamp()
    stage              = each.key
    projection_sha256  = each.value.owner_projection.projection_sha256
  }

  lifecycle {
    precondition {
      condition = (
        timecmp(timestamp(), each.value.proof.observed_at) >= 0 &&
        timecmp(each.value.proof.valid_until, timestamp()) > 0
      )
      error_message = "The SAI-22 migration evidence expired before apply; create and source-pin a new owner projection and acknowledgement, then produce a new plan."
    }
  }
}

# Non-secret current-state marker. It exposes only release names/revisions and
# is rewritten after a successful Helm change, avoiding reads of Helm Secrets.
resource "kubernetes_config_map_v1" "loki_release_freshness" {
  metadata {
    name      = "fs2-loki-foundation-freshness"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels    = local.common_labels
  }

  data = {
    "revisions.json" = jsonencode({
      schema = "fs2-serve.nebius.ai/loki-foundation-freshness/v1"
      target = {
        run_id          = var.run_id
        cluster_id      = var.cluster_id
        kube_system_uid = var.kube_system_uid
      }
      revisions = {
        loki_helm         = helm_release.loki.metadata.revision
        otel_gateway_helm = helm_release.otel_gateway.metadata.revision
        grafana_helm      = helm_release.monitoring.metadata.revision
      }
    })
  }
}

# Independent verifiers create immutable stage envelopes only after marker
# checks. Current deployment reads below are instantiated only when a later
# reviewed source pins the exact full-envelope SHA-256.
data "kubernetes_resource" "loki_migration_acknowledgement" {
  for_each    = local.loki_active_authorization_records
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = each.value.binding.config_map_name
    namespace = each.value.binding.namespace
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_payload_safety_inventory" {
  for_each    = local.loki_active_authorization_records
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = each.value.payload_safety_inventory.name
    namespace = each.value.payload_safety_inventory.namespace
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

# The immutable owner projection is deliberately distinct from the caller's
# acknowledgement. Its signer rereads Helm storage, effective Loki config,
# the Grafana datasource Secret, every relevant runtime workload source and
# the admission objects. The apply-time verifier rereads the signed config
# resources through the run-owned kubeconfig and returns only identities and
# content digests; live Secret/config bytes never enter Terraform output.
data "kubernetes_resource" "loki_owner_projection" {
  for_each    = local.loki_active_authorization_records
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = each.value.owner_projection.name
    namespace = each.value.owner_projection.namespace
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_secret_v1" "loki_owner_trust_root" {
  for_each = local.loki_active_authorization_records
  metadata {
    name      = each.value.owner_projection.trust_root.name
    namespace = each.value.owner_projection.trust_root.namespace
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "external" "loki_owner_projection_verification" {
  for_each = local.loki_active_authorization_records
  program  = ["python3", "${path.module}/scripts/verify-observability-owner-projection.py"]
  query = {
    projection_json                   = data.kubernetes_resource.loki_owner_projection[each.key].object.data["projection.json"]
    attestation_json                  = data.kubernetes_resource.loki_owner_projection[each.key].object.data["attestation.json"]
    trusted_attestors_json            = nonsensitive(data.kubernetes_secret_v1.loki_owner_trust_root[each.key].data["attestors.json"])
    validation_time                   = terraform_data.loki_apply_time_authorization[each.key].output.authorization_time
    kubeconfig_path                   = abspath(var.kubeconfig_path)
    kube_context                      = var.kube_context
    expected_stage                    = each.key
    expected_run_id                   = var.run_id
    expected_cluster_id               = var.cluster_id
    expected_kube_system_uid          = var.kube_system_uid
    expected_acknowledgement_sha256   = local.loki_migration_authorization_intent_sha256[each.key]
  }

  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_control_plane" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "fs2-serve-control-plane"
    namespace = "fs2-system"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_otel_gateway" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "fs2-otel-gateway"
    namespace = "fs2-observability"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_grafana" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "fs2-${var.run_id}-monitoring-grafana"
    namespace = "fs2-observability"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_loki" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "apps/v1"
  kind        = "StatefulSet"
  metadata {
    name      = "fs2-loki"
    namespace = "fs2-observability"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_foundation_freshness" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = "fs2-loki-foundation-freshness"
    namespace = "fs2-observability"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_workloads_freshness" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "v1"
  kind        = "ConfigMap"
  metadata {
    name      = "fs2-loki-workloads-freshness"
    namespace = "fs2-system"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_payload_admission_policy" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
  metadata {
    name = "fs2-runtime-log-payload-safety"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

data "kubernetes_resource" "loki_current_payload_admission_binding" {
  for_each    = local.loki_active_acknowledgement_source_accepted ? { active = local.loki_active_acknowledgement } : {}
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
  metadata {
    name = "fs2-runtime-log-payload-safety"
  }
  depends_on = [terraform_data.loki_apply_time_authorization]
}

locals {
  loki_owner_trust_root_bound = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => try(
      data.kubernetes_secret_v1.loki_owner_trust_root[stage].metadata[0].uid == acknowledgement.owner_projection.trust_root.uid &&
      data.kubernetes_secret_v1.loki_owner_trust_root[stage].metadata[0].resource_version == acknowledgement.owner_projection.trust_root.resource_version &&
      sha256(nonsensitive(data.kubernetes_secret_v1.loki_owner_trust_root[stage].data["attestors.json"])) == acknowledgement.owner_projection.trust_root.content_sha256 &&
      acknowledgement.owner_projection.trust_root.content_sha256 == local.accepted_observability_release_attestors_sha256,
      false,
    )
  }
  loki_verified_owner_projections = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => try(
      jsondecode(data.external.loki_owner_projection_verification[stage].result.projection_json),
      null,
    )
  }
  loki_apply_time_live_states = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => try(
      jsondecode(data.external.loki_owner_projection_verification[stage].result.live_state_json),
      null,
    )
  }
  loki_owner_projection_bound = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => try(
      local.loki_owner_trust_root_bound[stage] &&
      data.kubernetes_resource.loki_owner_projection[stage].object.immutable == true &&
      data.kubernetes_resource.loki_owner_projection[stage].object.metadata.uid == acknowledgement.owner_projection.uid &&
      data.kubernetes_resource.loki_owner_projection[stage].object.metadata.resourceVersion == acknowledgement.owner_projection.resource_version &&
      acknowledgement.owner_projection.name == "fs2-loki-${stage}-owner-${substr(acknowledgement.owner_projection.projection_sha256, 0, 12)}" &&
      data.external.loki_owner_projection_verification[stage].result.verified == "true" &&
      data.external.loki_owner_projection_verification[stage].result.projection_sha256 == acknowledgement.owner_projection.projection_sha256 &&
      data.external.loki_owner_projection_verification[stage].result.validated_at == terraform_data.loki_apply_time_authorization[stage].output.authorization_time &&
      sha256(data.kubernetes_resource.loki_owner_projection[stage].object.data["attestation.json"]) == acknowledgement.owner_projection.attestation_sha256 &&
      local.loki_verified_owner_projections[stage].acknowledgement_sha256 == local.loki_migration_authorization_intent_sha256[stage] &&
      local.loki_verified_owner_projections[stage].source == acknowledgement.source,
      false,
    )
  }
  loki_migration_acknowledgement_bound = {
    for stage, acknowledgement in var.loki_migration_acknowledgements : stage => (
      local.loki_migration_acknowledgement_source_accepted[stage] &&
      try(
        acknowledgement.target.run_id == var.run_id &&
        acknowledgement.target.cluster_id == var.cluster_id &&
        acknowledgement.target.kube_system_uid == var.kube_system_uid &&
        acknowledgement.binding.record_sha256 == sha256(local.loki_migration_acknowledgement_record_json[stage]) &&
        acknowledgement.binding.config_map_name == "fs2-loki-${stage}-ack-${substr(acknowledgement.binding.record_sha256, 0, 12)}" &&
        data.kubernetes_resource.loki_migration_acknowledgement[stage].object.immutable == true &&
        data.kubernetes_resource.loki_migration_acknowledgement[stage].object.metadata.uid == acknowledgement.binding.uid &&
        data.kubernetes_resource.loki_migration_acknowledgement[stage].object.metadata.resourceVersion == acknowledgement.binding.resource_version &&
        data.kubernetes_resource.loki_migration_acknowledgement[stage].object.data["acknowledgement.json"] == local.loki_migration_acknowledgement_record_json[stage],
        false,
      )
    )
  }
  loki_migration_proof_semantics_valid = {
    pretransition = try(
      !var.loki_migration_acknowledgements.pretransition.proof.auth_enabled &&
      var.loki_migration_acknowledgements.pretransition.proof.writer_identity == "fs2-otel-gateway" &&
      var.loki_migration_acknowledgements.pretransition.proof.writer_scoped_header_configured &&
      var.loki_migration_acknowledgements.pretransition.proof.writer_marker_ingested &&
      var.loki_migration_acknowledgements.pretransition.proof.marker_storage_tenant == local.loki_legacy_tenant_id &&
      var.loki_migration_acknowledgements.pretransition.proof.grafana_legacy_read &&
      !var.loki_migration_acknowledgements.pretransition.proof.grafana_scoped_read &&
      var.loki_migration_acknowledgements.pretransition.proof.control_plane_legacy_read &&
      !var.loki_migration_acknowledgements.pretransition.proof.control_plane_scoped_read,
      false,
    )
    posttransition = try(
      var.loki_migration_acknowledgements.posttransition.proof.auth_enabled &&
      var.loki_migration_acknowledgements.posttransition.proof.writer_identity == "fs2-otel-gateway" &&
      var.loki_migration_acknowledgements.posttransition.proof.writer_scoped_header_configured &&
      var.loki_migration_acknowledgements.posttransition.proof.writer_marker_ingested &&
      var.loki_migration_acknowledgements.posttransition.proof.marker_storage_tenant == local.loki_write_tenant_id &&
      var.loki_migration_acknowledgements.posttransition.proof.grafana_legacy_read &&
      var.loki_migration_acknowledgements.posttransition.proof.grafana_scoped_read &&
      var.loki_migration_acknowledgements.posttransition.proof.control_plane_legacy_read &&
      var.loki_migration_acknowledgements.posttransition.proof.control_plane_scoped_read,
      false,
    )
  }
  loki_current_deployment_fingerprints = {
    control_plane = try({
      uid                     = data.kubernetes_resource.loki_current_control_plane["active"].object.metadata.uid
      generation              = data.kubernetes_resource.loki_current_control_plane["active"].object.metadata.generation
      resource_version        = data.kubernetes_resource.loki_current_control_plane["active"].object.metadata.resourceVersion
      pod_template_sha256     = sha256(jsonencode(data.kubernetes_resource.loki_current_control_plane["active"].object.spec.template))
      container_images_sha256 = sha256(jsonencode({
        for container in data.kubernetes_resource.loki_current_control_plane["active"].object.spec.template.spec.containers :
        container.name => container.image
      }))
    }, null)
    otel_gateway = try({
      uid                     = data.kubernetes_resource.loki_current_otel_gateway["active"].object.metadata.uid
      generation              = data.kubernetes_resource.loki_current_otel_gateway["active"].object.metadata.generation
      resource_version        = data.kubernetes_resource.loki_current_otel_gateway["active"].object.metadata.resourceVersion
      pod_template_sha256     = sha256(jsonencode(data.kubernetes_resource.loki_current_otel_gateway["active"].object.spec.template))
      container_images_sha256 = sha256(jsonencode({
        for container in data.kubernetes_resource.loki_current_otel_gateway["active"].object.spec.template.spec.containers :
        container.name => container.image
      }))
    }, null)
    grafana = try({
      uid                     = data.kubernetes_resource.loki_current_grafana["active"].object.metadata.uid
      generation              = data.kubernetes_resource.loki_current_grafana["active"].object.metadata.generation
      resource_version        = data.kubernetes_resource.loki_current_grafana["active"].object.metadata.resourceVersion
      pod_template_sha256     = sha256(jsonencode(data.kubernetes_resource.loki_current_grafana["active"].object.spec.template))
      container_images_sha256 = sha256(jsonencode({
        for container in data.kubernetes_resource.loki_current_grafana["active"].object.spec.template.spec.containers :
        container.name => container.image
      }))
    }, null)
    loki = try({
      uid                     = data.kubernetes_resource.loki_current_loki["active"].object.metadata.uid
      generation              = data.kubernetes_resource.loki_current_loki["active"].object.metadata.generation
      resource_version        = data.kubernetes_resource.loki_current_loki["active"].object.metadata.resourceVersion
      pod_template_sha256     = sha256(jsonencode(data.kubernetes_resource.loki_current_loki["active"].object.spec.template))
      container_images_sha256 = sha256(jsonencode({
        for container in data.kubernetes_resource.loki_current_loki["active"].object.spec.template.spec.containers :
        container.name => container.image
      }))
    }, null)
  }
  loki_active_foundation_revisions = local.loki_active_acknowledgement == null ? {} : {
    loki_helm         = local.loki_active_acknowledgement.revisions.loki_helm
    otel_gateway_helm = local.loki_active_acknowledgement.revisions.otel_gateway_helm
    grafana_helm      = local.loki_active_acknowledgement.revisions.grafana_helm
  }
  loki_active_foundation_freshness = {
    schema = "fs2-serve.nebius.ai/loki-foundation-freshness/v1"
    target = {
      run_id          = var.run_id
      cluster_id      = var.cluster_id
      kube_system_uid = var.kube_system_uid
    }
    revisions = local.loki_active_foundation_revisions
  }
  loki_active_owner_projection = (
    local.loki_active_acknowledgement_stage == null ? null :
    try(local.loki_verified_owner_projections[local.loki_active_acknowledgement_stage], null)
  )
  loki_active_apply_time_live_state = (
    local.loki_active_acknowledgement_stage == null ? null :
    try(local.loki_apply_time_live_states[local.loki_active_acknowledgement_stage], null)
  )
  loki_owner_projection_current = try(
    local.loki_owner_projection_bound[local.loki_active_acknowledgement_stage] &&
    local.loki_active_owner_projection.releases.loki.name == "fs2-${var.run_id}-loki" &&
    local.loki_active_owner_projection.releases.loki.namespace == "fs2-observability" &&
    local.loki_active_owner_projection.releases.loki.revision == local.loki_active_acknowledgement.revisions.loki_helm &&
    local.loki_active_owner_projection.releases.otel_gateway.name == "fs2-${var.run_id}-otel-gateway" &&
    local.loki_active_owner_projection.releases.otel_gateway.namespace == "fs2-observability" &&
    local.loki_active_owner_projection.releases.otel_gateway.revision == helm_release.otel_gateway.metadata.revision &&
    local.loki_active_owner_projection.releases.otel_gateway.revision == local.loki_active_acknowledgement.revisions.otel_gateway_helm &&
    local.loki_active_owner_projection.releases.grafana.name == "fs2-${var.run_id}-monitoring" &&
    local.loki_active_owner_projection.releases.grafana.namespace == "fs2-observability" &&
    local.loki_active_owner_projection.releases.grafana.revision == helm_release.monitoring.metadata.revision &&
    local.loki_active_owner_projection.releases.grafana.revision == local.loki_active_acknowledgement.revisions.grafana_helm &&
    local.loki_active_owner_projection.releases.control_plane.name == "fs2-serve-control-plane" &&
    local.loki_active_owner_projection.releases.control_plane.namespace == "fs2-system" &&
    local.loki_active_owner_projection.releases.control_plane.revision == local.loki_active_acknowledgement.revisions.control_plane_helm &&
    local.loki_active_apply_time_live_state.schema == "fs2-serve.nebius.ai/observability-apply-time-live-state/v2" &&
    local.loki_active_apply_time_live_state.validated_at == terraform_data.loki_apply_time_authorization[local.loki_active_acknowledgement_stage].output.authorization_time &&
    local.loki_active_apply_time_live_state.target == local.loki_active_owner_projection.target &&
    local.loki_active_apply_time_live_state.configuration.loki == local.loki_active_owner_projection.live_configuration.loki.resource &&
    local.loki_active_apply_time_live_state.configuration.loki_runtime == local.loki_active_owner_projection.live_configuration.loki.runtime_resource &&
    local.loki_active_apply_time_live_state.configuration.otel_gateway == local.loki_active_owner_projection.live_configuration.otel_gateway.resource &&
    local.loki_active_apply_time_live_state.configuration.grafana_datasource == local.loki_active_owner_projection.live_configuration.grafana_datasource.resource &&
    local.loki_active_apply_time_live_state.configuration.control_plane == local.loki_active_owner_projection.live_configuration.control_plane.resource &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.config_map == local.loki_active_owner_projection.live_configuration.control_plane.resource &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.api_version == local.loki_active_owner_projection.workloads.control_plane.api_version &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.kind == local.loki_active_owner_projection.workloads.control_plane.kind &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.namespace == local.loki_active_owner_projection.workloads.control_plane.namespace &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.name == local.loki_active_owner_projection.workloads.control_plane.name &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.uid == local.loki_active_owner_projection.workloads.control_plane.uid &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.generation == local.loki_active_owner_projection.workloads.control_plane.generation &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.deployment.resource_version == local.loki_active_owner_projection.workloads.control_plane.resource_version &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.container_name == "control-plane" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.config_volume_name == "admin-observability" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.config_key == "config.json" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.config_mount_path == "/etc/fs2-serve/admin-observability" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.config_file == "/etc/fs2-serve/admin-observability/config.json" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.loki_url == "http://fs2-loki.fs2-observability.svc.cluster.local:3100" &&
    local.loki_active_apply_time_live_state.configuration.control_plane_consumption.read_tenant_header == "fake|fs2-platform" &&
    alltrue([
      for name, fingerprint in local.loki_active_acknowledgement.deployments :
      local.loki_active_owner_projection.workloads[name].uid == fingerprint.uid &&
      local.loki_active_owner_projection.workloads[name].generation == fingerprint.generation &&
      local.loki_active_owner_projection.workloads[name].resource_version == fingerprint.resource_version &&
      local.loki_active_owner_projection.workloads[name].pod_template_sha256 == fingerprint.pod_template_sha256 &&
      local.loki_active_owner_projection.workloads[name].container_images_sha256 == fingerprint.container_images_sha256
    ]) &&
    local.loki_active_owner_projection.live_configuration.grafana_datasource.resource.namespace == local.loki_active_acknowledgement.grafana_datasource.namespace &&
    local.loki_active_owner_projection.live_configuration.grafana_datasource.resource.name == local.loki_active_acknowledgement.grafana_datasource.name &&
    local.loki_active_owner_projection.live_configuration.grafana_datasource.resource.uid == local.loki_active_acknowledgement.grafana_datasource.uid &&
    local.loki_active_owner_projection.live_configuration.grafana_datasource.resource.resource_version == local.loki_active_acknowledgement.grafana_datasource.resource_version &&
    local.loki_active_owner_projection.cached_markers.foundation.uid == local.loki_active_acknowledgement.freshness_markers.foundation.uid &&
    local.loki_active_owner_projection.cached_markers.foundation.resource_version == local.loki_active_acknowledgement.freshness_markers.foundation.resource_version &&
    local.loki_active_owner_projection.cached_markers.foundation.content_sha256 == local.loki_active_acknowledgement.freshness_markers.foundation.content_sha256 &&
    local.loki_active_owner_projection.cached_markers.workloads.uid == local.loki_active_acknowledgement.freshness_markers.workloads.uid &&
    local.loki_active_owner_projection.cached_markers.workloads.resource_version == local.loki_active_acknowledgement.freshness_markers.workloads.resource_version &&
    local.loki_active_owner_projection.cached_markers.workloads.content_sha256 == local.loki_active_acknowledgement.freshness_markers.workloads.content_sha256 &&
    local.loki_active_owner_projection.payload_safety.inventory.resource.namespace == local.loki_active_acknowledgement.payload_safety_inventory.namespace &&
    local.loki_active_owner_projection.payload_safety.inventory.resource.name == local.loki_active_acknowledgement.payload_safety_inventory.name &&
    local.loki_active_owner_projection.payload_safety.inventory.resource.uid == local.loki_active_acknowledgement.payload_safety_inventory.uid &&
    local.loki_active_owner_projection.payload_safety.inventory.resource.resource_version == local.loki_active_acknowledgement.payload_safety_inventory.resource_version &&
    local.loki_active_owner_projection.payload_safety.inventory.inventory_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.inventory_sha256 &&
    local.loki_active_owner_projection.payload_safety.inventory.permit_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.permit_sha256 &&
    local.loki_active_owner_projection.payload_safety.inventory.data_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.data_sha256 &&
    local.loki_active_owner_projection.payload_safety.inventory.image_count == local.loki_active_acknowledgement.payload_safety_inventory.image_count &&
    local.loki_active_apply_time_live_state.payload_safety.resource == local.loki_active_owner_projection.payload_safety.inventory.resource &&
    local.loki_active_apply_time_live_state.payload_safety.inventory_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.inventory_sha256 &&
    local.loki_active_apply_time_live_state.payload_safety.permit_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.permit_sha256 &&
    local.loki_active_apply_time_live_state.payload_safety.data_sha256 == local.loki_active_acknowledgement.payload_safety_inventory.data_sha256 &&
    local.loki_active_apply_time_live_state.payload_safety.image_count == local.loki_active_acknowledgement.payload_safety_inventory.image_count &&
    local.loki_active_owner_projection.payload_safety.admission.parameter.uid == local.loki_active_acknowledgement.payload_safety_inventory.uid &&
    local.loki_active_owner_projection.payload_safety.admission.parameter.resource_version == local.loki_active_acknowledgement.payload_safety_inventory.resource_version &&
    local.loki_active_owner_projection.payload_safety.admission.policy.uid == data.kubernetes_resource.loki_current_payload_admission_policy["active"].object.metadata.uid &&
    local.loki_active_owner_projection.payload_safety.admission.policy.resource_version == data.kubernetes_resource.loki_current_payload_admission_policy["active"].object.metadata.resourceVersion &&
    local.loki_active_owner_projection.payload_safety.admission.policy.content_sha256 == sha256(jsonencode(data.kubernetes_resource.loki_current_payload_admission_policy["active"].object.spec)) &&
    local.loki_active_owner_projection.payload_safety.admission.binding.uid == data.kubernetes_resource.loki_current_payload_admission_binding["active"].object.metadata.uid &&
    local.loki_active_owner_projection.payload_safety.admission.binding.resource_version == data.kubernetes_resource.loki_current_payload_admission_binding["active"].object.metadata.resourceVersion &&
    local.loki_active_owner_projection.payload_safety.admission.binding.content_sha256 == sha256(jsonencode(data.kubernetes_resource.loki_current_payload_admission_binding["active"].object.spec)) &&
    local.loki_active_owner_projection.migration_proof.sealed_evidence_sha256 == local.loki_active_acknowledgement.proof.sealed_evidence_sha256 &&
    local.loki_active_owner_projection.migration_proof.marker_sha256 == local.loki_active_acknowledgement.proof.marker_sha256 &&
    local.loki_active_owner_projection.migration_proof.writer_identity == local.loki_active_acknowledgement.proof.writer_identity &&
    local.loki_active_owner_projection.migration_proof.writer_scoped_header_configured == local.loki_active_acknowledgement.proof.writer_scoped_header_configured &&
    local.loki_active_owner_projection.migration_proof.writer_marker_ingested == local.loki_active_acknowledgement.proof.writer_marker_ingested &&
    local.loki_active_owner_projection.migration_proof.marker_storage_tenant == local.loki_active_acknowledgement.proof.marker_storage_tenant &&
    local.loki_active_owner_projection.migration_proof.grafana_legacy_read == local.loki_active_acknowledgement.proof.grafana_legacy_read &&
    local.loki_active_owner_projection.migration_proof.grafana_scoped_read == local.loki_active_acknowledgement.proof.grafana_scoped_read &&
    local.loki_active_owner_projection.migration_proof.control_plane_legacy_read == local.loki_active_acknowledgement.proof.control_plane_legacy_read &&
    local.loki_active_owner_projection.migration_proof.control_plane_scoped_read == local.loki_active_acknowledgement.proof.control_plane_scoped_read &&
    local.loki_active_owner_projection.observed_at == local.loki_active_acknowledgement.proof.observed_at &&
    local.loki_active_owner_projection.valid_until == local.loki_active_acknowledgement.proof.valid_until,
    false,
  )
  loki_current_payload_safety_data = try(
    data.kubernetes_resource.loki_payload_safety_inventory[local.loki_active_acknowledgement_stage].object.data,
    {},
  )
  loki_current_payload_safety_record = try(
    jsondecode(local.loki_current_payload_safety_data["inventory.json"]),
    null,
  )
  loki_current_payload_safety_permits = {
    for key, value in local.loki_current_payload_safety_data : key => value
    if startswith(key, "image-")
  }
  loki_expected_payload_safety_permits = try({
    for key, evidence in local.loki_current_payload_safety_record.images :
    "image-${key}" => evidence.image_reference
  }, {})
  loki_active_deployment_state_current = try(
    alltrue([
      for name, fingerprint in local.loki_active_acknowledgement.deployments :
      local.loki_current_deployment_fingerprints[name] == fingerprint
    ]) &&
    data.kubernetes_resource.loki_current_foundation_freshness["active"].object.metadata.uid == local.loki_active_acknowledgement.freshness_markers.foundation.uid &&
    data.kubernetes_resource.loki_current_foundation_freshness["active"].object.metadata.resourceVersion == local.loki_active_acknowledgement.freshness_markers.foundation.resource_version &&
    sha256(data.kubernetes_resource.loki_current_foundation_freshness["active"].object.data["revisions.json"]) == local.loki_active_acknowledgement.freshness_markers.foundation.content_sha256 &&
    jsondecode(data.kubernetes_resource.loki_current_foundation_freshness["active"].object.data["revisions.json"]) == local.loki_active_foundation_freshness &&
    data.kubernetes_resource.loki_current_workloads_freshness["active"].object.metadata.uid == local.loki_active_acknowledgement.freshness_markers.workloads.uid &&
    data.kubernetes_resource.loki_current_workloads_freshness["active"].object.metadata.resourceVersion == local.loki_active_acknowledgement.freshness_markers.workloads.resource_version &&
    sha256(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]) == local.loki_active_acknowledgement.freshness_markers.workloads.content_sha256 &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).schema == "fs2-serve.nebius.ai/loki-workloads-freshness/v1" &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).target.run_id == var.run_id &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).target.cluster_id == var.cluster_id &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).target.kube_system_uid == var.kube_system_uid &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).control_plane_helm == local.loki_active_acknowledgement.revisions.control_plane_helm &&
    contains(
      [
        for container in data.kubernetes_resource.loki_current_control_plane["active"].object.spec.template.spec.containers :
        container.image
      ],
      jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).control_plane_image,
    ) &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).grafana_datasource.uid == local.loki_active_acknowledgement.grafana_datasource.uid &&
    jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).grafana_datasource.resource_version == local.loki_active_acknowledgement.grafana_datasource.resource_version &&
    jsonencode(jsondecode(data.kubernetes_resource.loki_current_workloads_freshness["active"].object.data["client.json"]).payload_safety_inventory) == jsonencode(local.loki_active_acknowledgement.payload_safety_inventory) &&
    data.kubernetes_resource.loki_payload_safety_inventory[local.loki_active_acknowledgement_stage].object.immutable == true &&
    data.kubernetes_resource.loki_payload_safety_inventory[local.loki_active_acknowledgement_stage].object.metadata.uid == local.loki_active_acknowledgement.payload_safety_inventory.uid &&
    data.kubernetes_resource.loki_payload_safety_inventory[local.loki_active_acknowledgement_stage].object.metadata.resourceVersion == local.loki_active_acknowledgement.payload_safety_inventory.resource_version &&
    local.loki_active_acknowledgement.payload_safety_inventory.name == "fs2-runtime-log-safety-${substr(local.loki_active_acknowledgement.payload_safety_inventory.inventory_sha256, 0, 12)}" &&
    sha256(local.loki_current_payload_safety_data["inventory.json"]) == local.loki_active_acknowledgement.payload_safety_inventory.inventory_sha256 &&
    sha256(jsonencode(local.loki_current_payload_safety_permits)) == local.loki_active_acknowledgement.payload_safety_inventory.permit_sha256 &&
    sha256(jsonencode(local.loki_current_payload_safety_data)) == local.loki_active_acknowledgement.payload_safety_inventory.data_sha256 &&
    local.loki_current_payload_safety_permits == local.loki_expected_payload_safety_permits &&
    toset(keys(local.loki_current_payload_safety_data)) == setunion(toset(["inventory.json"]), toset(keys(local.loki_expected_payload_safety_permits))) &&
    alltrue([
      for key, evidence in local.loki_current_payload_safety_record.images :
      key == sha256(evidence.image_reference)
    ]) &&
    length(local.loki_current_payload_safety_record.images) == local.loki_active_acknowledgement.payload_safety_inventory.image_count &&
    jsonencode(local.loki_current_payload_safety_record.coverage) == jsonencode(local.loki_active_owner_projection.payload_safety.coverage) &&
    local.loki_current_payload_safety_record.target.run_id == var.run_id &&
    local.loki_current_payload_safety_record.target.cluster_id == var.cluster_id &&
    local.loki_current_payload_safety_record.target.kube_system_uid == var.kube_system_uid &&
    terraform_data.loki_apply_time_authorization[local.loki_active_acknowledgement_stage].output.authorization_time == data.external.loki_owner_projection_verification[local.loki_active_acknowledgement_stage].result.validated_at,
    false,
  )
  loki_active_migration_acknowledgement_ready = (
    local.loki_active_acknowledgement_stage != null &&
    try(local.loki_migration_acknowledgement_bound[local.loki_active_acknowledgement_stage], false) &&
    try(local.loki_migration_proof_semantics_valid[local.loki_active_acknowledgement_stage], false) &&
    local.loki_active_deployment_state_current &&
    local.loki_owner_projection_current
  )
  loki_migration_authorized = (
    !local.loki_auth_enforced || local.loki_active_migration_acknowledgement_ready
  )
  loki_active_migration_acknowledgement_sha256 = (
    local.loki_active_acknowledgement_stage == null ? null :
    try(local.loki_migration_acknowledgement_sha256[local.loki_active_acknowledgement_stage], null)
  )
}

# Loki's tenant header is meaningful only behind a network identity boundary.
# Select the single-binary workload and admit the three application consumers,
# its Prometheus health scraper, and Loki's own cluster ports. Model/scientific
# namespaces match none of these peers and therefore cannot query port 3100.
resource "kubernetes_network_policy_v1" "loki_ingress" {
  metadata {
    name      = "fs2-loki-ingress"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels    = local.common_labels
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component" = "single-binary"
        "app.kubernetes.io/instance"  = "fs2-${var.run_id}-loki"
        "app.kubernetes.io/name"      = "loki"
      }
    }
    policy_types = ["Ingress"]

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "fs2-${var.run_id}-monitoring"
            "app.kubernetes.io/name"     = "grafana"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "fs2-${var.run_id}-otel-gateway"
            "app.kubernetes.io/name"     = "opentelemetry-collector"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = "fs2-system"
          }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component" = "gateway"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    # Preserve Loki self-monitoring without admitting any workload namespace.
    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/instance" = "fs2-${var.run_id}-monitoring-prometheus"
            "app.kubernetes.io/name"     = "prometheus"
          }
        }
      }
      ports {
        port     = "3100"
        protocol = "TCP"
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            "app.kubernetes.io/component" = "single-binary"
            "app.kubernetes.io/instance"  = "fs2-${var.run_id}-loki"
            "app.kubernetes.io/name"      = "loki"
          }
        }
      }
      ports {
        port     = "7946"
        protocol = "TCP"
      }
      ports {
        port     = "7946"
        protocol = "UDP"
      }
      ports {
        port     = "9095"
        protocol = "TCP"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.loki_identity_custody_ready
      error_message = "SAI-22 is blocked: pin the independently accepted SAI-03 admission/label-custody receipt in source before applying the Loki label-selected identity boundary."
    }

    precondition {
      condition     = local.loki_prometheus_health_exception_ready
      error_message = "SAI-22 is blocked: replace Prometheus direct access with metrics-only mediation or pin an independently accepted Loki health-scrape exception receipt in source."
    }

    precondition {
      condition = (
        local.loki_phase_rank[var.loki_access_phase] >=
        local.loki_phase_rank[var.loki_rollback_floor]
      )
      error_message = "loki_access_phase cannot move below loki_rollback_floor; after scoped writes begin, retain enforced dual-read so neither the legacy nor scoped cohort is hidden."
    }
  }
}

# Single-binary Tempo is deliberately sized for the cluster-local seven-day
# trace/debug window. Durable accounting remains a workloads-stage database
# concern; Tempo is the raw correlation plane for request and Job attempts.
resource "helm_release" "tempo" {
  name             = "fs2-${var.run_id}-tempo"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "tempo"
  version          = local.chart_versions.tempo
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 1200

  # Chart archive SHA-256 at the pinned repository URL:
  # f1f6e318d5bca3b5097cb676077796cdf8135beb2c1f71c4d14614ccf9b0081b
  values = [
    file("${path.module}/values/tempo.yaml"),
    yamlencode({
      serviceMonitor = {
        additionalLabels = { release = "fs2-${var.run_id}-monitoring" }
      }
    }),
  ]

  depends_on = [helm_release.monitoring]
}

# Kubernetes Events must have one active watcher. Keeping this separate from
# the node log DaemonSet avoids duplicate events while preserving node-local
# container-log collection and checkpoint behavior.
resource "helm_release" "otel_cluster" {
  name             = "fs2-${var.run_id}-otel-cluster"
  namespace        = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-collector"
  version          = local.chart_versions.opentelemetry
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [file("${path.module}/values/otel-cluster.yaml")]

  depends_on = [helm_release.otel_gateway]
}

resource "kubernetes_config_map_v1" "grafana_tempo_datasource" {
  metadata {
    name      = "fs2-tempo-grafana-datasource"
    namespace = kubernetes_namespace_v1.platform["fs2-observability"].metadata[0].name
    labels = merge(local.common_labels, {
      grafana_datasource = "1"
    })
  }

  data = {
    "datasource.yaml" = yamlencode({
      apiVersion = 1
      prune      = false
      datasources = [{
        name      = local.tempo_grafana_datasource
        uid       = local.tempo_grafana_datasource
        type      = "tempo"
        access    = "proxy"
        orgId     = 1
        url       = "http://${local.tempo_service_name}.fs2-observability.svc.cluster.local:3200"
        isDefault = false
        editable  = false
        version   = 1
        jsonData = {
          httpMethod = "GET"
          tracesToLogsV2 = {
            datasourceUid      = "fs2-${var.run_id}-loki"
            filterBySpanID     = true
            filterByTraceID    = true
            spanStartTimeShift = "-1m"
            spanEndTimeShift   = "1m"
          }
          serviceMap = {
            datasourceUid = "prometheus"
          }
        }
      }]
    })
  }

  depends_on = [
    helm_release.monitoring,
    helm_release.tempo,
  ]
}

output "observability_operator_contract" {
  description = "Non-secret installed services and Grafana datasource identities consumed by the workloads admin projection."
  value = {
    schema = "fs2-serve.nebius.ai/observability-operator/v1"
    alertmanager = {
      enabled                = var.alertmanager.enabled
      service_name           = local.alertmanager_service_name
      service_port           = 9093
      grafana_datasource_uid = var.alertmanager.enabled ? local.alertmanager_grafana_datasource : null
      retention              = var.alertmanager.retention
      storage = {
        class_name   = var.alertmanager.storage.storage_class_name
        size_gib     = var.alertmanager.storage.size_gib
        when_deleted = "Retain"
        when_scaled  = "Retain"
      }
    }
    tempo = {
      enabled                = true
      service_name           = local.tempo_service_name
      service_port           = 3200
      grafana_datasource_uid = local.tempo_grafana_datasource
    }
    loki = {
      access_phase                          = var.loki_access_phase
      rollback_floor                        = var.loki_rollback_floor
      auth_enabled                          = local.loki_auth_enforced
      service_name                          = "fs2-loki"
      service_port                          = 3100
      legacy_tenant_id                      = local.loki_legacy_tenant_id
      write_tenant_id                       = local.loki_write_tenant_id
      read_tenant_header                    = local.loki_read_tenant_header
      multi_tenant_queries_enabled          = true
      legacy_retention_hours                = local.loki_legacy_retention_hours
      legacy_read_retirement_boundary       = "not-before-168h-after-auth-enforcement"
      ingress_policy_name                   = kubernetes_network_policy_v1.loki_ingress.metadata[0].name
      identity_custody_ready                = local.loki_identity_custody_ready
      prometheus_health_exception_ready     = local.loki_prometheus_health_exception_ready
      active_acknowledgement_stage          = local.loki_active_acknowledgement_stage
      active_acknowledgement_ready          = local.loki_active_migration_acknowledgement_ready
      current_deployment_state_matches      = local.loki_active_deployment_state_current
      current_owner_projection_matches      = local.loki_owner_projection_current
      owner_projection_max_age_seconds      = 300
      saved_plan_apply_time_revalidation    = true
      live_configuration_content_reread     = ["loki", "loki-runtime", "otel-gateway", "grafana-datasource", "control-plane"]
      control_plane_reader_consumption_bound = true
      payload_permit_full_data_map_bound    = true
      release_attestor_trust_root_accepted  = local.accepted_observability_release_attestors_sha256 != null
      enforcement_authorized                = local.loki_identity_custody_ready && local.loki_prometheus_health_exception_ready && local.loki_migration_authorized
      expected_client_configuration_claim   = local.expected_loki_client_configuration_claim
      caller_reproducible_receipts_accepted = false
      transition_order                      = ["network-policy-auth-off", "header-capable-clients-and-legacy-proof", "auth-enforced-validation", "post-auth-scoped-proof-and-enforced-dual-read"]
    }
    raw_backends_public = false
    operator_surface    = "grafana-native-auth"
  }
}

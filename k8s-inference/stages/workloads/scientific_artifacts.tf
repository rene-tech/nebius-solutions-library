# Scientific result artifact store, workload side.
#
# The infrastructure stage hands over one provider key reference per exact
# tenant prefix. Each independently routable broker Deployment mounts exactly
# one tenant key and performs the authorized object operation itself. The
# gateway receives no provider key and cannot select a tenant from an object
# key; the independently authorized tenant chooses the broker service first.

locals {
  scientific_artifacts_enabled        = var.scientific_artifacts.enabled
  scientific_artifact_authority_keyring = local.scientific_artifacts_enabled ? one(
    data.kubernetes_config_map_v1.scientific_artifact_authority_keyring[*].data
  ) : {}
  scientific_artifact_authority_keyring_sha256 = local.scientific_artifacts_enabled ? sha256(jsonencode(
    local.scientific_artifact_authority_keyring
  )) : ""
  scientific_artifact_broker_name     = "fs2-artifact"
  scientific_artifact_broker_port     = 8443
  scientific_artifact_broker_url_template = "https://${local.scientific_artifact_broker_name}-{tenant_hash}.fs2-system.svc:${local.scientific_artifact_broker_port}/v1"
  # Closing legacy admission enrollment is a one-way database transition: an
  # older gateway restored for rollback cannot create unsigned operations once
  # it is closed. Ordinary infrastructure/workloads applies must therefore
  # never create a closer. A future, separately reviewed activation change may
  # replace this fail-closed constant only after it can verify signed provider,
  # database, fleet-drain, version-inventory and rollback-state receipts.
  scientific_artifact_irreversible_cutover_authorized = false
  scientific_runtime_cache_claim_name = "fs2-scientific-runtime-cache"
  scientific_runtime_cache_mount_path = "/cache"
  scientific_runtime_cache_mounts = flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : [
        for mount in try(stage.mounts, []) : {
          model_id           = try(model.model_id, "")
          stage_id           = try(stage.stage_id, "")
          workload_namespace = try(model.workload_namespace, "")
          name               = try(mount.name, "")
          claim_name         = try(mount.claim_name, null)
          host_path          = try(mount.host_path, null)
          mount_path         = try(mount.mount_path, "")
          sub_path           = try(mount.sub_path, null)
          read_only          = try(mount.read_only, null)
        } if try(mount.kind, "") == "runtime-cache"
      ]
    ]
  ])
  # A runtime-cache mount is model-only, but the RWX claim's provider-owned
  # root is not writable by the unprivileged model UID. Derive each exact
  # first-level cache boundary and its owner from the same execution-map stage
  # that the controller renders. Terraform prepares those boundaries once;
  # kubelet never performs a recursive fsGroup rewrite on every cold start.
  scientific_runtime_cache_consumers = flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : {
        model_id           = try(model.model_id, "")
        stage_id           = try(stage.stage_id, "")
        workload_namespace = try(model.workload_namespace, "")
        workspace_uid      = try(stage.workspace_uid, null)
        workspace_gid      = try(stage.workspace_gid, null)
        cache_paths = sort(distinct([
          for value in values(try(stage.environment, {})) : value
          if try(startswith(value, "${local.scientific_runtime_cache_mount_path}/"), false)
        ]))
        } if length([
          for mount in try(stage.mounts, []) : mount
          if try(mount.kind, "") == "runtime-cache"
      ]) == 1
    ]
  ])
  scientific_runtime_cache_directory_claims = flatten([
    for consumer in local.scientific_runtime_cache_consumers : [
      for name in distinct([
        for path in consumer.cache_paths : split("/", path)[2]
        ]) : {
        name               = name
        uid                = consumer.workspace_uid
        gid                = consumer.workspace_gid
        model_id           = consumer.model_id
        stage_id           = consumer.stage_id
        workload_namespace = consumer.workload_namespace
      }
    ]
  ])
  scientific_runtime_cache_directory_claims_by_name = {
    for claim in local.scientific_runtime_cache_directory_claims : claim.name => claim...
  }
  scientific_runtime_cache_directories = [
    for name in sort(keys(local.scientific_runtime_cache_directory_claims_by_name)) : {
      name = name
      uid  = local.scientific_runtime_cache_directory_claims_by_name[name][0].uid
      gid  = local.scientific_runtime_cache_directory_claims_by_name[name][0].gid
      mode = "2770"
    }
  ]
  scientific_runtime_cache_ownership_contract = {
    schema      = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v1"
    root        = local.scientific_runtime_cache_mount_path
    directories = local.scientific_runtime_cache_directories
  }
  scientific_runtime_cache_ownership_sha256 = sha256(jsonencode(
    local.scientific_runtime_cache_ownership_contract
  ))
  scientific_runtime_cache_bootstrap_sha256 = sha256(jsonencode({
    ownership_sha256  = local.scientific_runtime_cache_ownership_sha256
    program_sha256    = filesha256("${path.module}/scripts/scientific_runtime_cache_bootstrap.py")
    runtime_image_ref = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
  }))
  # A PersistentVolumeClaim is namespaced. Keep the original singleton resource
  # for the configured batch namespace (and therefore its stable Terraform
  # address), then provision one same-named claim for every additional workload
  # namespace that actually renders a runtime-cache mount.
  scientific_runtime_cache_consumer_namespaces = sort(distinct([
    for consumer in local.scientific_runtime_cache_consumers : consumer.workload_namespace
  ]))
  scientific_runtime_cache_additional_namespaces = toset([
    for namespace in local.scientific_runtime_cache_consumer_namespaces : namespace
    if namespace != var.scientific_batch.namespace
  ])
  scientific_runtime_cache_additional_directory_claims = {
    for namespace in local.scientific_runtime_cache_additional_namespaces : namespace => [
      for claim in local.scientific_runtime_cache_directory_claims : claim
      if claim.workload_namespace == namespace
    ]
  }
  scientific_runtime_cache_additional_claims_by_name = {
    for namespace, claims in local.scientific_runtime_cache_additional_directory_claims : namespace => {
      for claim in claims : claim.name => claim...
    }
  }
  scientific_runtime_cache_additional_directories = {
    for namespace, claims_by_name in local.scientific_runtime_cache_additional_claims_by_name : namespace => [
      for name in sort(keys(claims_by_name)) : {
        name = name
        uid  = claims_by_name[name][0].uid
        gid  = claims_by_name[name][0].gid
        mode = "2770"
      }
    ]
  }
  scientific_runtime_cache_additional_ownership_contracts = {
    for namespace, directories in local.scientific_runtime_cache_additional_directories : namespace => {
      schema      = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v1"
      root        = local.scientific_runtime_cache_mount_path
      directories = directories
    }
  }
  scientific_runtime_cache_additional_ownership_sha256 = {
    for namespace, contract in local.scientific_runtime_cache_additional_ownership_contracts :
    namespace => sha256(jsonencode(contract))
  }
  scientific_runtime_cache_additional_bootstrap_sha256 = {
    for namespace, ownership_sha256 in local.scientific_runtime_cache_additional_ownership_sha256 : namespace => sha256(jsonencode({
      namespace         = namespace
      ownership_sha256  = ownership_sha256
      program_sha256    = filesha256("${path.module}/scripts/scientific_runtime_cache_bootstrap.py")
      runtime_image_ref = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
    }))
  }
  scientific_runtime_cache_namespace_claims = merge([
    for _ in range(var.scientific_batch.runtime_cache.enabled ? 1 : 0) : merge(
      {
        (var.scientific_batch.namespace) = {
          claim_name       = local.scientific_runtime_cache_claim_name
          bootstrap_job    = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_bootstrap_sha256, 0, 12)}"
          bootstrap_sha256 = local.scientific_runtime_cache_bootstrap_sha256
          contract_sha256  = local.scientific_runtime_cache_ownership_sha256
          directories      = local.scientific_runtime_cache_directories
          consumers = sort(distinct([
            for mount in local.scientific_runtime_cache_mounts : "${mount.model_id}/${mount.stage_id}"
            if mount.workload_namespace == var.scientific_batch.namespace
          ]))
        }
      },
      {
        for namespace in local.scientific_runtime_cache_additional_namespaces : namespace => {
          claim_name       = local.scientific_runtime_cache_claim_name
          bootstrap_job    = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_additional_bootstrap_sha256[namespace], 0, 12)}"
          bootstrap_sha256 = local.scientific_runtime_cache_additional_bootstrap_sha256[namespace]
          contract_sha256  = local.scientific_runtime_cache_additional_ownership_sha256[namespace]
          directories      = local.scientific_runtime_cache_additional_directories[namespace]
          consumers = sort(distinct([
            for mount in local.scientific_runtime_cache_mounts : "${mount.model_id}/${mount.stage_id}"
            if mount.workload_namespace == namespace
          ]))
        }
      },
    )
  ]...)
  scientific_artifact_tenant_generations = local.scientific_artifacts_enabled ? (
    var.scientific_artifacts.tenant_broker_access.tenants
  ) : {}
  # Preserve the original per-tenant Secret address for generation 1. New
  # generations receive additive resource addresses and immutable Secret
  # names; retired generations remain declared but are no longer mounted.
  scientific_artifact_generation_one_access = {
    for tenant_id, tenant in local.scientific_artifact_tenant_generations : tenant_id => merge(
      tenant.generations["1"],
      { generation = 1 },
    )
  }
  scientific_artifact_additional_generation_access = merge([
    for tenant_id, tenant in local.scientific_artifact_tenant_generations : {
      for generation, access in tenant.generations : "${tenant_id}:${generation}" => merge(access, {
        tenant_id  = tenant_id
        generation = tonumber(generation)
      }) if generation != "1"
    }
  ]...)
  scientific_artifact_all_generation_access = merge(
    {
      for tenant_id, access in local.scientific_artifact_generation_one_access :
      "${tenant_id}:1" => merge(access, { tenant_id = tenant_id, generation = 1 })
    },
    local.scientific_artifact_additional_generation_access,
  )
  scientific_artifact_tenant_access = {
    for tenant_id, tenant in local.scientific_artifact_tenant_generations : tenant_id => merge(
      tenant.generations[tostring(tenant.active_generation)],
      { generation = tenant.active_generation },
    )
  }
  scientific_artifact_active_observation_bindings = {
    for tenant_id, access in local.scientific_artifact_tenant_access : tenant_id => {
      generation = access.generation
      binding_sha256 = sha256(jsonencode({
        tenant_id     = tenant_id
        generation    = access.generation
        access_key_id = access.access_key_id
        service_account_subject = "system:serviceaccount:fs2-system:fs2-artifact-${substr(sha256(tenant_id), 0, 32)}-g${access.generation}"
      }))
    }
  }
  scientific_artifact_observation_bindings_json = local.scientific_artifacts_enabled ? jsonencode({
    schema = "fs2-serve.nebius.ai/artifact-provider-observation-bindings/v1"
    tenants = {
      for tenant_id, tenant in local.scientific_artifact_tenant_generations : tenant_id => {
        active_generation = tenant.active_generation
        authorized_generations = {
          for generation in tenant.authorized_generations : tostring(generation) => {
            access_key_id = tenant.generations[tostring(generation)].access_key_id
            service_account_subject = "system:serviceaccount:fs2-system:fs2-artifact-${substr(sha256(tenant_id), 0, 32)}-g${generation}"
            binding_sha256 = sha256(jsonencode({
              tenant_id     = tenant_id
              generation    = generation
              access_key_id = tenant.generations[tostring(generation)].access_key_id
              service_account_subject = "system:serviceaccount:fs2-system:fs2-artifact-${substr(sha256(tenant_id), 0, 32)}-g${generation}"
            }))
          }
        }
      }
    }
  }) : ""
  scientific_artifact_provider_bindings_json = local.scientific_artifacts_enabled ? jsonencode({
    schema = "fs2-serve.nebius.ai/artifact-tenant-broker-routing/v1"
    tenants = {
      for tenant_id, access in local.scientific_artifact_tenant_access : tenant_id => {
        service_name            = "${local.scientific_artifact_broker_name}-${substr(sha256(tenant_id), 0, 32)}"
        provider_binding_sha256 = sha256(jsonencode(access))
        revision                = access.revision
        generation              = access.generation
      }
    }
  }) : ""
  scientific_artifacts_revision = local.scientific_artifacts_enabled ? parseint(
    substr(sha256(local.scientific_artifact_provider_bindings_json), 0, 12), 16
  ) : 0

  # Zero-or-one comprehension so the disabled case yields an empty map rather
  # than an object Terraform cannot unify with the enabled one.
  scientific_artifacts_overrides = merge([
    for _ in range(local.scientific_artifacts_enabled ? 1 : 0) : {
      scientificArtifacts = {
        enabled          = true
        endpoint         = var.scientific_artifacts.storage_contract.object_storage.endpoint
        bucket           = var.scientific_artifacts.storage_contract.object_storage.name
        region           = var.scientific_artifacts.storage_contract.region
        addressingStyle  = var.scientific_artifacts.storage_contract.object_storage.addressing_style
        verifyTls        = var.scientific_artifacts.storage_contract.object_storage.verify_tls
        handleTtlSeconds = var.scientific_artifacts.handle_ttl_seconds
        uploadHandleTtlSeconds = var.scientific_artifacts.upload_handle_ttl_seconds
        downloadHandleTtlSeconds = var.scientific_artifacts.download_handle_ttl_seconds
        maxBytes         = var.scientific_artifacts.max_artifact_bytes
        retentionSeconds = var.scientific_artifacts.retention_days * 86400
        mediaTypes       = sort(var.scientific_artifacts.media_types)
        egressCidrs      = sort(var.scientific_artifacts.egress_cidrs)
        credentialBroker = {
          urlTemplate                   = local.scientific_artifact_broker_url_template
          audience                      = "fs2-artifact-credential-broker"
          readinessConfigMapName        = kubernetes_config_map_v1.scientific_artifact_observation_bindings[0].metadata[0].name
          tokenExpirationSeconds        = var.scientific_artifacts.broker.kubernetes_token_seconds
          caSecretName                  = var.scientific_artifacts.broker.ca_secret_name
          caKey                         = var.scientific_artifacts.broker.ca_key
          timeoutSeconds                = "5"
          operationCredentialTtlSeconds = 120
        }
        authorityIssuer = {
          url                    = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
          audience               = "fs2-artifact-authority-issuer"
          tokenExpirationSeconds = var.scientific_artifacts.broker.kubernetes_token_seconds
          caSecretName           = var.scientific_artifacts.broker.ca_secret_name
          caKey                  = var.scientific_artifacts.broker.ca_key
          timeoutSeconds         = "5"
        }
      }
      artifactVersionBackfill = {
        enabled               = var.scientific_artifacts.version_backfill.enabled
        manifestSecretName    = var.scientific_artifacts.version_backfill.manifest_secret_name
        manifestKey           = var.scientific_artifacts.version_backfill.manifest_key
        manifestSha256        = var.scientific_artifacts.version_backfill.manifest_sha256
        activeDeadlineSeconds = var.scientific_artifacts.version_backfill.active_deadline_seconds
      }
      networkPolicy = {
        artifactStoreCidrs = sort(var.scientific_artifacts.egress_cidrs)
      }
      podAnnotations = {
        "fs2.nebius.ai/artifact-provider-bindings-revision" = tostring(local.scientific_artifacts_revision)
      }
    }
  ]...)

  scientific_batch_overrides = {
    scientificBatch = {
      enabled                         = var.scientific_batch.enabled
      writesEnabled                   = var.scientific_batch.writes_enabled
      namespace                       = var.scientific_batch.namespace
      kubernetesApiUrl                = "https://kubernetes.default.svc"
      schedulingContractConfigMapName = local.scheduling_contract_ref.config_map_name
      schedulingContractNamespace     = local.scheduling_contract_ref.namespace
      schedulingContractKey           = local.scheduling_contract_ref.key
      schedulingContractSchema        = local.scheduling_contract_ref.schema
      schedulingContractSha256        = local.scheduling_contract_ref.sha256
      executionMapConfigMapName       = "fs2-${var.run_id}-scientific-execution"
      executionMapKey                 = "execution-map.json"
      executionMap = merge(var.scientific_batch.execution_map,
        length(var.scientific_batch.gpu_snapshots.bundles) == 0 ? {} : {
          snapshot_bundles = var.scientific_batch.gpu_snapshots.bundles
      })
      workers                = var.scientific_batch.workers
      pollSeconds            = var.scientific_batch.poll_seconds
      leaseSeconds           = var.scientific_batch.lease_seconds
      apiTimeoutSeconds      = var.scientific_batch.api_timeout_seconds
      tokenExpirationSeconds = var.scientific_batch.token_expiration_seconds
    }
  }

  scientific_chart_overrides = merge(
    local.scientific_artifacts_overrides,
    local.scientific_batch_overrides,
  )
}

data "kubernetes_config_map_v1" "scientific_artifact_authority_keyring" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  metadata {
    name      = var.scientific_artifacts.broker.authority_verification_config_map_name
    namespace = "fs2-system"
  }
}

resource "kubernetes_config_map_v1" "scientific_artifact_provider_bindings" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = "${local.scientific_artifact_broker_name}-bindings"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "artifact-credential-broker" })
  }

  data = { "bindings.json" = local.scientific_artifact_provider_bindings_json }
  immutable = true
  # Compatibility address only: no workload mounts or consumes this object.
  # Active routing is derived from the generation-specific broker Deployment
  # and its digest annotation. Keeping the first immutable payload unchanged
  # prevents a generation switch from planning a delete/recreate outage.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [data]
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_config_map_v1" "scientific_artifact_observation_bindings" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = "fs2-artifact-observation-bindings"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "artifact-authority-issuer"
    })
    annotations = {
      "fs2.nebius.ai/bindings-sha256" = sha256(local.scientific_artifact_observation_bindings_json)
    }
  }

  data = { "bindings.json" = local.scientific_artifact_observation_bindings_json }
  depends_on = [terraform_data.cluster_contract]
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifact_tenant" {
  for_each = local.scientific_artifact_generation_one_access

  secret_id = each.value.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "scientific_artifact_tenant_broker" {
  for_each = local.scientific_artifact_generation_one_access

  metadata {
    name      = "${local.scientific_artifact_broker_name}-${substr(sha256(each.key), 0, 32)}-provider"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "artifact-tenant-broker"
      "fs2.nebius.ai/tenant-hash"   = substr(sha256(each.key), 0, 32)
    })
  }
  type      = "Opaque"
  immutable = true
  data_wo = {
    "access-key-id"     = each.value.access_key_id
    "secret-access-key" = ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifact_tenant[each.key].data.string_value
  }
  data_wo_revision = 1

  lifecycle {
    prevent_destroy = true
  }
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifact_tenant_generation" {
  for_each = local.scientific_artifact_additional_generation_access

  secret_id = each.value.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "scientific_artifact_tenant_broker_generation" {
  for_each = local.scientific_artifact_additional_generation_access

  metadata {
    name      = "${local.scientific_artifact_broker_name}-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}-provider"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"       = "artifact-tenant-broker"
      "fs2.nebius.ai/tenant-hash"         = substr(sha256(each.value.tenant_id), 0, 32)
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    })
  }
  type      = "Opaque"
  immutable = true
  data_wo = {
    "access-key-id"     = each.value.access_key_id
    "secret-access-key" = ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifact_tenant_generation[each.key].data.string_value
  }
  data_wo_revision = 1

  lifecycle {
    prevent_destroy = true
  }
}

# Preserve the pre-SAI-19 Secret at its exact Terraform address. During the
# first legacy-overlap apply the already-running gateway may still mount it and
# its provider group remains narrowly authorized. New gateway replicas do not
# mount it. This candidate deliberately supplies no ordinary-apply path to
# deauthorize it or close compatibility enrollment; that needs a separately
# reviewed signed transition and complete legacy-input proof.
ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  secret_id = var.scientific_artifacts.tenant_broker_access.legacy_quarantine.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "scientific_artifact_store" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = "fs2-serve-artifact-store"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "scientific-artifact-store-quarantined"
    })
    annotations = {
      "fs2.nebius.ai/authorization" = var.scientific_artifacts.tenant_broker_access.legacy_quarantine.authorized ? "legacy-migration-overlap" : "none"
      "fs2.nebius.ai/disposition" = var.scientific_artifacts.tenant_broker_access.legacy_quarantine.authorized ? (
        "retained-for-running-pre-broker-replicas"
      ) : "retained-unmounted-pending-reviewed-retirement"
    }
  }
  type = "Opaque"
  data_wo = {
    "credentials.json" = jsonencode({
      access_key_id     = var.scientific_artifacts.tenant_broker_access.legacy_quarantine.access_key_id
      secret_access_key = ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifacts[0].data.string_value
    })
  }
  data_wo_revision = var.scientific_artifacts.tenant_broker_access.legacy_quarantine.revision

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_service_account_v1" "scientific_artifact_broker" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = local.scientific_artifact_broker_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "artifact-credential-broker" })
    annotations = { "fs2.nebius.ai/provider-credential-scope" = "one-tenant-per-broker-pod" }
  }
  automount_service_account_token = false
}

# The original shared broker ServiceAccount remains quarantined at its stable
# address. Every retained credential generation receives a distinct additive
# identity; active broker Pods select exactly one of these identities.
resource "kubernetes_service_account_v1" "scientific_artifact_tenant_broker" {
  for_each = local.scientific_artifact_all_generation_access

  metadata {
    name      = "fs2-artifact-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"       = "artifact-tenant-broker"
      "fs2.nebius.ai/tenant-hash"         = substr(sha256(each.value.tenant_id), 0, 32)
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    })
    annotations = {
      "fs2.nebius.ai/provider-binding-sha256" = sha256(jsonencode({
        tenant_id     = each.value.tenant_id
        generation    = each.value.generation
        access_key_id = each.value.access_key_id
        service_account_subject = "system:serviceaccount:fs2-system:fs2-artifact-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}"
      }))
    }
  }
  automount_service_account_token = false

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_service_account_v1" "scientific_artifact_authority" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = "fs2-artifact-authority"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "artifact-authority-issuer" })
    annotations = { "fs2.nebius.ai/key-custody" = "ed25519-private-issuer-only" }
  }
  automount_service_account_token = false
}

resource "kubernetes_service_account_v1" "scientific_artifact_authority_cutover" {
  count = local.scientific_artifact_irreversible_cutover_authorized ? 1 : 0

  metadata {
    name      = "fs2-artifact-authority-cutover"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "artifact-authority-cutover"
    })
    annotations = {
      "fs2.nebius.ai/authority" = "close-legacy-enrollment-once"
    }
  }
  automount_service_account_token = false

  lifecycle { prevent_destroy = true }
}

resource "kubernetes_cluster_role_v1" "scientific_artifact_broker_tokenreview" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  metadata { name = "${local.scientific_artifact_broker_name}-${var.run_id}" }
  rule {
    api_groups = ["authentication.k8s.io"]
    resources  = ["tokenreviews"]
    verbs      = ["create"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "scientific_artifact_broker_tokenreview" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  metadata { name = "${local.scientific_artifact_broker_name}-${var.run_id}" }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.scientific_artifact_broker_tokenreview[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.scientific_artifact_broker[0].metadata[0].name
    namespace = "fs2-system"
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.scientific_artifact_authority[0].metadata[0].name
    namespace = "fs2-system"
  }
  dynamic "subject" {
    for_each = kubernetes_service_account_v1.scientific_artifact_tenant_broker
    content {
      kind      = "ServiceAccount"
      name      = subject.value.metadata[0].name
      namespace = "fs2-system"
    }
  }
}

resource "kubernetes_service_v1" "scientific_artifact_authority" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  metadata {
    name      = "fs2-artifact-authority"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "artifact-authority-issuer" })
  }
  spec {
    selector = { "app.kubernetes.io/name" = "fs2-artifact-authority" }
    port {
      name        = "https"
      port        = local.scientific_artifact_broker_port
      target_port = "https"
      protocol    = "TCP"
    }
  }
}

resource "kubernetes_deployment_v1" "scientific_artifact_authority" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  metadata {
    name      = "fs2-artifact-authority"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/name" = "fs2-artifact-authority" })
  }
  spec {
    replicas = 2
    selector { match_labels = { "app.kubernetes.io/name" = "fs2-artifact-authority" } }
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/name"      = "fs2-artifact-authority"
          "app.kubernetes.io/component" = "artifact-authority-issuer"
        })
        annotations = {
          "fs2.nebius.ai/key-custody"         = "ed25519-private-issuer-only"
          "fs2.nebius.ai/verification-keyring" = local.scientific_artifact_authority_keyring_sha256
          "fs2.nebius.ai/provider-observation-bindings" = sha256(local.scientific_artifact_observation_bindings_json)
        }
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.scientific_artifact_authority[0].metadata[0].name
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = "issuer"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["fs2-serve", "artifact-authority-issuer"]
          port {
            name           = "https"
            container_port = local.scientific_artifact_broker_port
            protocol       = "TCP"
          }
          startup_probe {
            http_get {
              path   = "/healthz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 30
            period_seconds    = 2
            timeout_seconds   = 1
          }
          readiness_probe {
            http_get {
              path   = "/readyz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 3
            period_seconds    = 10
            timeout_seconds   = 5
          }
          liveness_probe {
            http_get {
              path   = "/healthz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 3
            period_seconds    = 20
            timeout_seconds   = 2
          }
          env {
            name = "FS2_ARTIFACT_AUTHORITY_DATABASE_URL"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_consumer["artifact_authority"].metadata[0].name
                key  = "url"
              }
            }
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_TOKEN_PEPPER_FILE"
            value = "/var/run/secrets/fs2-serve/token-pepper/keyring.json"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_SIGNING_KEY_FILE"
            value = "/var/run/secrets/fs2-artifact-authority/signing/ed25519-private.pem"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_VERIFICATION_KEY_DIRECTORY"
            value = "/var/run/secrets/fs2-artifact-authority/verification"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_PROVIDER_BINDINGS_FILE"
            value = "/var/run/fs2-artifact-authority/provider-bindings/bindings.json"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_KUBERNETES_REVIEWER_TOKEN_FILE"
            value = "/var/run/secrets/fs2-artifact-authority/kubernetes/token"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_KUBERNETES_CA_FILE"
            value = "/var/run/secrets/fs2-artifact-authority/kubernetes-ca/ca.crt"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_ALLOWED_CUTOVER_SUBJECT"
            value = "system:serviceaccount:fs2-system:fs2-artifact-authority-cutover"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_TLS_CERTIFICATE_FILE"
            value = "/var/run/secrets/fs2-artifact-authority/tls/tls.crt"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_TLS_PRIVATE_KEY_FILE"
            value = "/var/run/secrets/fs2-artifact-authority/tls/tls.key"
          }
          resources {
            requests = { cpu = "100m", memory = "128Mi" }
            limits   = { cpu = "1", memory = "512Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "signing-key"
            mount_path = "/var/run/secrets/fs2-artifact-authority/signing"
            read_only  = true
          }
          volume_mount {
            name       = "verification-key"
            mount_path = "/var/run/secrets/fs2-artifact-authority/verification"
            read_only  = true
          }
          volume_mount {
            name       = "provider-bindings"
            mount_path = "/var/run/fs2-artifact-authority/provider-bindings"
            read_only  = true
          }
          volume_mount {
            name       = "kubernetes-reviewer"
            mount_path = "/var/run/secrets/fs2-artifact-authority/kubernetes"
            read_only  = true
          }
          volume_mount {
            name       = "kubernetes-ca"
            mount_path = "/var/run/secrets/fs2-artifact-authority/kubernetes-ca"
            read_only  = true
          }
          volume_mount {
            name       = "issuer-tls"
            mount_path = "/var/run/secrets/fs2-artifact-authority/tls"
            read_only  = true
          }
          volume_mount {
            name       = "token-pepper"
            mount_path = "/var/run/secrets/fs2-serve/token-pepper"
            read_only  = true
          }
          volume_mount {
            name       = "database-tls"
            mount_path = "/tls"
            read_only  = true
          }
        }
        volume {
          name = "signing-key"
          secret {
            secret_name  = var.scientific_artifacts.broker.authority_signing_secret_name
            default_mode = "0400"
            items {
              key  = var.scientific_artifacts.broker.authority_signing_key
              path = "ed25519-private.pem"
            }
          }
        }
        volume {
          name = "verification-key"
          config_map {
            name         = var.scientific_artifacts.broker.authority_verification_config_map_name
            default_mode = "0444"
          }
        }
        volume {
          name = "provider-bindings"
          config_map {
            name         = kubernetes_config_map_v1.scientific_artifact_observation_bindings[0].metadata[0].name
            default_mode = "0444"
          }
        }
        volume {
          name = "kubernetes-reviewer"
          projected {
            default_mode = "0400"
            sources {
              service_account_token {
                audience           = "https://kubernetes.default.svc"
                expiration_seconds = var.scientific_artifacts.broker.kubernetes_token_seconds
                path               = "token"
              }
            }
          }
        }
        volume {
          name = "kubernetes-ca"
          config_map {
            name = "kube-root-ca.crt"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
        volume {
          name = "issuer-tls"
          secret {
            secret_name  = var.scientific_artifacts.broker.tls_secret_name
            default_mode = "0400"
          }
        }
        volume {
          name = "token-pepper"
          secret {
            secret_name  = kubernetes_secret_v1.token_pepper.metadata[0].name
            default_mode = "0400"
          }
        }
        volume {
          name = "database-tls"
          secret {
            secret_name  = kubernetes_secret_v1.database_consumer["artifact_authority"].metadata[0].name
            default_mode = "0400"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_cluster_role_binding_v1.scientific_artifact_broker_tokenreview]
}

resource "kubernetes_service_v1" "scientific_artifact_broker" {
  for_each = local.scientific_artifact_tenant_access
  metadata {
    name      = "${local.scientific_artifact_broker_name}-${substr(sha256(each.key), 0, 32)}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "artifact-credential-broker" })
  }
  spec {
    selector = {
      "app.kubernetes.io/name" = local.scientific_artifact_broker_name
      "fs2.nebius.ai/tenant-hash" = substr(sha256(each.key), 0, 32)
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    }
    port {
      name        = "https"
      port        = local.scientific_artifact_broker_port
      target_port = "https"
      protocol    = "TCP"
    }
  }

  # On a generation switch the provider waits for every retained, generation-
  # addressed Deployment rollout before changing the stable Service selector.
  # The old generation remains running and addressable only through its Pod
  # identity until a separately observed drain permits provider quarantine.
  depends_on = [kubernetes_deployment_v1.scientific_artifact_broker]
}

# Every retained credential generation has an immutable, generation-addressed
# Service.  Preactivation checks use this address and cannot accidentally hit
# the currently active generation through the stable tenant Service.
resource "kubernetes_service_v1" "scientific_artifact_broker_generation" {
  for_each = local.scientific_artifact_all_generation_access

  metadata {
    name      = "${local.scientific_artifact_broker_name}-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"         = "artifact-credential-broker"
      "fs2.nebius.ai/tenant-hash"           = substr(sha256(each.value.tenant_id), 0, 32)
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    })
  }

  spec {
    selector = {
      "app.kubernetes.io/name"              = local.scientific_artifact_broker_name
      "fs2.nebius.ai/tenant-hash"           = substr(sha256(each.value.tenant_id), 0, 32)
      "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
    }
    port {
      name        = "https"
      port        = local.scientific_artifact_broker_port
      target_port = "https"
      protocol    = "TCP"
    }
  }

  depends_on = [kubernetes_deployment_v1.scientific_artifact_broker]
}

resource "kubernetes_deployment_v1" "scientific_artifact_broker" {
  # Every retained generation has an independently addressable Deployment.
  # A replacement can therefore become Ready while the stable tenant Service
  # still selects the previous generation. Removing bucket authorization later
  # quarantines an old Deployment without deleting its rollback identity.
  for_each = local.scientific_artifact_all_generation_access
  metadata {
    name      = "${local.scientific_artifact_broker_name}-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/name" = local.scientific_artifact_broker_name })
  }
  spec {
    replicas = 2
    selector {
      match_labels = {
        "app.kubernetes.io/name" = local.scientific_artifact_broker_name
        "fs2.nebius.ai/tenant-hash" = substr(sha256(each.value.tenant_id), 0, 32)
        "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
      }
    }
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/name"      = local.scientific_artifact_broker_name
          "app.kubernetes.io/component" = "artifact-tenant-broker"
          "fs2.nebius.ai/tenant-hash"   = substr(sha256(each.value.tenant_id), 0, 32)
          "fs2.nebius.ai/credential-generation" = tostring(each.value.generation)
        })
        annotations = {
          "fs2.nebius.ai/provider-binding-sha256" = sha256(jsonencode({
            tenant_id     = each.value.tenant_id
            access_key_id = each.value.access_key_id
            revision      = each.value.revision
            generation    = each.value.generation
          }))
          "fs2.nebius.ai/verification-keyring" = local.scientific_artifact_authority_keyring_sha256
        }
      }
      spec {
        service_account_name = kubernetes_service_account_v1.scientific_artifact_tenant_broker[
          each.key
        ].metadata[0].name
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile {
            type = "RuntimeDefault"
          }
        }
        container {
          name    = "broker"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["fs2-serve", "artifact-credential-broker"]
          port {
            name           = "https"
            container_port = local.scientific_artifact_broker_port
            protocol       = "TCP"
          }
          startup_probe {
            http_get {
              path   = "/healthz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 30
            period_seconds    = 2
            timeout_seconds   = 1
          }
          readiness_probe {
            http_get {
              path   = "/readyz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 3
            period_seconds    = 10
            timeout_seconds   = 5
          }
          liveness_probe {
            http_get {
              path   = "/healthz"
              port   = "https"
              scheme = "HTTPS"
            }
            failure_threshold = 3
            period_seconds    = 20
            timeout_seconds   = 2
          }
          env {
            name = "FS2_ARTIFACT_BROKER_DATABASE_URL"
            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.database_consumer["artifact_broker"].metadata[0].name
                key  = "url"
              }
            }
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_TOKEN_PEPPER_FILE"
            value = "/var/run/secrets/fs2-serve/token-pepper/keyring.json"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_ALLOWED_TENANT_ID"
            value = each.value.tenant_id
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_ENDPOINT_URL"
            value = var.scientific_artifacts.storage_contract.object_storage.endpoint
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_BUCKET"
            value = var.scientific_artifacts.storage_contract.object_storage.name
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_REGION"
            value = var.scientific_artifacts.storage_contract.region
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_ADDRESSING_STYLE"
            value = var.scientific_artifacts.storage_contract.object_storage.addressing_style
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_ACCESS_KEY_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/provider/access-key-id"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_SECRET_KEY_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/provider/secret-access-key"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_GENERATION"
            value = tostring(each.value.generation)
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_PROVIDER_BINDING_SHA256"
            value = sha256(jsonencode({
              tenant_id     = each.value.tenant_id
              generation    = each.value.generation
              access_key_id = each.value.access_key_id
              service_account_subject = "system:serviceaccount:fs2-system:fs2-artifact-${substr(sha256(each.value.tenant_id), 0, 32)}-g${each.value.generation}"
            }))
          }
          env {
            name = "FS2_ARTIFACT_BROKER_BROKER_POD_UID"
            value_from {
              field_ref { field_path = "metadata.uid" }
            }
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_OBSERVATION_ISSUER_URL"
            value = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_OBSERVATION_ISSUER_AUDIENCE"
            value = "fs2-artifact-authority-issuer"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_AUTHORITY_VERIFICATION_KEY_DIRECTORY"
            value = "/var/run/secrets/fs2-artifact-broker/authority"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_KUBERNETES_REVIEWER_TOKEN_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/kubernetes/token"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_KUBERNETES_CA_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/kubernetes-ca/ca.crt"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_TLS_CERTIFICATE_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/tls/tls.crt"
          }
          env {
            name  = "FS2_ARTIFACT_BROKER_TLS_PRIVATE_KEY_FILE"
            value = "/var/run/secrets/fs2-artifact-broker/tls/tls.key"
          }
          resources {
            requests = { cpu = "100m", memory = "256Mi" }
            limits   = { cpu = "1", memory = "1Gi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
          }
          volume_mount {
            name       = "provider-identity"
            mount_path = "/var/run/secrets/fs2-artifact-broker/provider"
            read_only  = true
          }
          volume_mount {
            name       = "authority-verification"
            mount_path = "/var/run/secrets/fs2-artifact-broker/authority"
            read_only  = true
          }
          volume_mount {
            name       = "kubernetes-reviewer"
            mount_path = "/var/run/secrets/fs2-artifact-broker/kubernetes"
            read_only  = true
          }
          volume_mount {
            name       = "kubernetes-ca"
            mount_path = "/var/run/secrets/fs2-artifact-broker/kubernetes-ca"
            read_only  = true
          }
          volume_mount {
            name       = "observation-issuer"
            mount_path = "/var/run/secrets/fs2-artifact-broker/observation-issuer"
            read_only  = true
          }
          volume_mount {
            name       = "broker-tls"
            mount_path = "/var/run/secrets/fs2-artifact-broker/tls"
            read_only  = true
          }
          volume_mount {
            name       = "token-pepper"
            mount_path = "/var/run/secrets/fs2-serve/token-pepper"
            read_only  = true
          }
          volume_mount {
            name       = "database-tls"
            mount_path = "/tls"
            read_only  = true
          }
        }
        volume {
          name = "broker-tls"
          secret {
            secret_name  = var.scientific_artifacts.broker.tls_secret_name
            default_mode = "0400"
          }
        }
        volume {
          name = "token-pepper"
          secret {
            secret_name  = kubernetes_secret_v1.token_pepper.metadata[0].name
            default_mode = "0400"
          }
        }
        volume {
          name = "database-tls"
          secret {
            secret_name  = kubernetes_secret_v1.database_consumer["artifact_broker"].metadata[0].name
            default_mode = "0400"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
        volume {
          name = "provider-identity"
          secret {
            secret_name = each.value.generation == 1 ? (
              kubernetes_secret_v1.scientific_artifact_tenant_broker[each.value.tenant_id].metadata[0].name
            ) : kubernetes_secret_v1.scientific_artifact_tenant_broker_generation[each.key].metadata[0].name
            default_mode = "0400"
          }
        }
        volume {
          name = "authority-verification"
          config_map {
            name         = var.scientific_artifacts.broker.authority_verification_config_map_name
            default_mode = "0444"
          }
        }
        volume {
          name = "kubernetes-reviewer"
          projected {
            default_mode = "0400"
            sources {
              service_account_token {
                audience           = "https://kubernetes.default.svc"
                expiration_seconds = var.scientific_artifacts.broker.kubernetes_token_seconds
                path               = "token"
              }
            }
          }
        }
        volume {
          name = "kubernetes-ca"
          config_map {
            name = "kube-root-ca.crt"
            items {
              key  = "ca.crt"
              path = "ca.crt"
            }
          }
        }
        volume {
          name = "observation-issuer"
          projected {
            default_mode = "0400"
            sources {
              service_account_token {
                audience           = "fs2-artifact-authority-issuer"
                expiration_seconds = var.scientific_artifacts.broker.kubernetes_token_seconds
                path               = "token"
              }
            }
            sources {
              secret {
                name = var.scientific_artifacts.broker.ca_secret_name
                items {
                  key  = var.scientific_artifacts.broker.ca_key
                  path = "ca.crt"
                }
              }
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_cluster_role_binding_v1.scientific_artifact_broker_tokenreview]
}

resource "kubernetes_manifest" "scientific_artifact_broker_network_policy" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = local.scientific_artifact_broker_name
      namespace = "fs2-system"
      labels    = local.common_labels
    }
    spec = {
      podSelector = {
        matchLabels = { "app.kubernetes.io/name" = local.scientific_artifact_broker_name }
      }
      policyTypes = ["Ingress", "Egress"]
      ingress = [{
        from = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-system" } }
          podSelector = {
            matchExpressions = [{
              key      = "app.kubernetes.io/component"
              operator = "In"
              values   = ["gateway", "artifact-orphan-cleanup", "artifact-version-backfill"]
            }]
          }
        }]
        ports = [{ port = local.scientific_artifact_broker_port, protocol = "TCP" }]
      }]
      egress = concat(
        [{
          to = [{
            namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
            podSelector       = { matchLabels = { "k8s-app" = "kube-dns" } }
          }]
          ports = [
            { port = 53, protocol = "UDP" },
            { port = 53, protocol = "TCP" },
          ]
        }, {
          to = [{
            namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-data" } }
            podSelector       = { matchLabels = { "cnpg.io/cluster" = "fs2-control-db" } }
          }]
          ports = [{ port = 5432, protocol = "TCP" }]
        }, {
          to = [{
            namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-system" } }
            podSelector       = { matchLabels = { "app.kubernetes.io/name" = "fs2-artifact-authority" } }
          }]
          ports = [{ port = local.scientific_artifact_broker_port, protocol = "TCP" }]
        }],
        [{
          to = [for cidr in local.kubernetes_api_egress_cidrs : { ipBlock = { cidr = cidr } }]
          ports = [{ port = 443, protocol = "TCP" }]
        }],
        [{
          to = [for cidr in var.scientific_artifacts.egress_cidrs : { ipBlock = { cidr = cidr } }]
          ports = [{ port = 443, protocol = "TCP" }]
        }],
      )
    }
  }
  depends_on = [kubernetes_deployment_v1.scientific_artifact_broker]
}

resource "kubernetes_manifest" "scientific_artifact_authority_network_policy" {
  count = local.scientific_artifacts_enabled ? 1 : 0
  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = "fs2-artifact-authority"
      namespace = "fs2-system"
      labels    = local.common_labels
    }
    spec = {
      podSelector = { matchLabels = { "app.kubernetes.io/name" = "fs2-artifact-authority" } }
      policyTypes = ["Ingress", "Egress"]
      ingress = [{
        from = [{
          podSelector = {
            matchExpressions = [{
              key      = "app.kubernetes.io/component"
              operator = "In"
              values   = ["gateway", "artifact-authority-cutover", "artifact-tenant-broker"]
            }]
          }
        }]
        ports = [{ port = local.scientific_artifact_broker_port, protocol = "TCP" }]
      }]
      egress = [{
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
          podSelector       = { matchLabels = { "k8s-app" = "kube-dns" } }
        }]
        ports = [
          { port = 53, protocol = "UDP" },
          { port = 53, protocol = "TCP" },
        ]
        }, {
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "fs2-data" } }
          podSelector       = { matchLabels = { "cnpg.io/cluster" = "fs2-control-db" } }
        }]
        ports = [{ port = 5432, protocol = "TCP" }]
        }, {
        to = [for cidr in local.kubernetes_api_egress_cidrs : { ipBlock = { cidr = cidr } }]
        ports = [{ port = 443, protocol = "TCP" }]
      }]
    }
  }
  depends_on = [kubernetes_deployment_v1.scientific_artifact_authority]
}

resource "kubernetes_manifest" "scientific_artifact_authority_cutover_network_policy" {
  count = local.scientific_artifact_irreversible_cutover_authorized ? 1 : 0
  manifest = {
    apiVersion = "networking.k8s.io/v1"
    kind       = "NetworkPolicy"
    metadata = {
      name      = "fs2-artifact-authority-cutover"
      namespace = "fs2-system"
      labels    = local.common_labels
    }
    spec = {
      podSelector = {
        matchLabels = { "app.kubernetes.io/component" = "artifact-authority-cutover" }
      }
      policyTypes = ["Ingress", "Egress"]
      ingress     = []
      egress = [{
        to = [{
          namespaceSelector = { matchLabels = { "kubernetes.io/metadata.name" = "kube-system" } }
          podSelector       = { matchLabels = { "k8s-app" = "kube-dns" } }
        }]
        ports = [
          { port = 53, protocol = "UDP" },
          { port = 53, protocol = "TCP" },
        ]
        }, {
        to = [{
          podSelector = { matchLabels = { "app.kubernetes.io/name" = "fs2-artifact-authority" } }
        }]
        ports = [{ port = local.scientific_artifact_broker_port, protocol = "TCP" }]
      }]
    }
  }
  depends_on = [kubernetes_manifest.scientific_artifact_authority_network_policy]
}

# Dormant source for the separately authorized one-way transition. It is never
# created by this candidate: a Helm success or ordinary Terraform apply is not
# an authoritative cutover receipt and must remain reversible.
resource "kubernetes_job_v1" "scientific_artifact_authority_cutover" {
  count               = local.scientific_artifact_irreversible_cutover_authorized ? 1 : 0
  wait_for_completion = true

  metadata {
    name      = "fs2-artifact-authority-cutover"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "artifact-authority-cutover"
    })
  }
  spec {
    backoff_limit              = 0
    active_deadline_seconds    = 300
    ttl_seconds_after_finished = null
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "artifact-authority-cutover"
        })
      }
      spec {
        restart_policy                   = "Never"
        service_account_name             = kubernetes_service_account_v1.scientific_artifact_authority_cutover[0].metadata[0].name
        automount_service_account_token  = false
        termination_grace_period_seconds = 10
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = "cutover"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["fs2-serve", "artifact-authority-cutover"]
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_CUTOVER_ISSUER_URL"
            value = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_CUTOVER_AUDIENCE"
            value = "fs2-artifact-authority-issuer"
          }
          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "identity"
            mount_path = "/var/run/secrets/fs2-artifact-authority-cutover"
            read_only  = true
          }
        }
        volume {
          name = "identity"
          projected {
            default_mode = "0400"
            sources {
              service_account_token {
                audience           = "fs2-artifact-authority-issuer"
                expiration_seconds = 600
                path               = "token"
              }
            }
            sources {
              secret {
                name = var.scientific_artifacts.broker.ca_secret_name
                items {
                  key  = var.scientific_artifacts.broker.ca_key
                  path = "ca.crt"
                }
              }
            }
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
    # The retained completed Job is the Kubernetes-side receipt. Future image
    # releases must not attempt to replace its immutable pod template.
    ignore_changes = [spec]
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.scientific_artifact_authority_cutover_network_policy,
  ]
}

# Dormant repair controller for the same separately reviewed activation. It is
# deliberately absent from ordinary deployments; no forever-retrying workload
# may convert a reversible rollout into an irreversible database transition.
resource "kubernetes_deployment_v1" "scientific_artifact_authority_cutover" {
  count = local.scientific_artifact_irreversible_cutover_authorized ? 1 : 0

  metadata {
    name      = "fs2-artifact-authority-cutover-controller"
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "artifact-authority-cutover"
    })
  }
  spec {
    replicas = 1
    selector {
      match_labels = {
        "app.kubernetes.io/name"      = "fs2-artifact-authority-cutover-controller"
        "app.kubernetes.io/component" = "artifact-authority-cutover"
      }
    }
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/name"      = "fs2-artifact-authority-cutover-controller"
          "app.kubernetes.io/component" = "artifact-authority-cutover"
        })
      }
      spec {
        service_account_name            = kubernetes_service_account_v1.scientific_artifact_authority_cutover[0].metadata[0].name
        automount_service_account_token = false
        security_context {
          run_as_non_root = true
          run_as_user     = 65532
          run_as_group    = 65532
          fs_group        = 65532
          seccomp_profile { type = "RuntimeDefault" }
        }
        container {
          name    = "controller"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["fs2-serve", "artifact-authority-cutover-controller"]
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_CUTOVER_ISSUER_URL"
            value = "https://fs2-artifact-authority.fs2-system.svc:8443/v1"
          }
          env {
            name  = "FS2_ARTIFACT_AUTHORITY_CUTOVER_AUDIENCE"
            value = "fs2-artifact-authority-issuer"
          }
          resources {
            requests = { cpu = "10m", memory = "32Mi" }
            limits   = { cpu = "100m", memory = "128Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "identity"
            mount_path = "/var/run/secrets/fs2-artifact-authority-cutover"
            read_only  = true
          }
        }
        volume {
          name = "identity"
          projected {
            default_mode = "0400"
            sources {
              service_account_token {
                audience           = "fs2-artifact-authority-issuer"
                expiration_seconds = 600
                path               = "token"
              }
            }
            sources {
              secret {
                name = var.scientific_artifacts.broker.ca_secret_name
                items {
                  key  = var.scientific_artifacts.broker.ca_key
                  path = "ca.crt"
                }
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.scientific_artifact_authority_cutover_network_policy,
  ]
}

# Disposable derived runtime state only: compiled kernels and framework cache
# entries. Immutable model artifacts and tenant inputs never use this claim.
# The stable name is part of the reviewed execution-map contract, while size
# and storage class remain ordinary terraform.tfvars settings.
resource "kubernetes_persistent_volume_claim_v1" "scientific_runtime_cache" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  wait_until_bound = false

  metadata {
    name      = local.scientific_runtime_cache_claim_name
    namespace = var.scientific_batch.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"        = "scientific-runtime-cache"
      "fast-start.fs2.nebius/storage-role" = "compile-cache"
    })
    annotations = {
      "fs2.nebius.ai/data-classification" = "disposable-derived-cache"
      "fs2.nebius.ai/mount-path"          = local.scientific_runtime_cache_mount_path
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.scientific_batch.runtime_cache.storage_class_name
    volume_mode        = "Filesystem"

    resources {
      requests = {
        storage = "${var.scientific_batch.runtime_cache.size_gib}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      spec[0].volume_name,
    ]
  }

  depends_on = [terraform_data.cluster_contract]
}

# Preserve the original PVC address above for existing states, while creating
# the same stable claim name in every other execution-map namespace that mounts
# it. Kubernetes cannot mount a claim across namespace boundaries.
resource "kubernetes_persistent_volume_claim_v1" "scientific_runtime_cache_additional" {
  for_each = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_additional_namespaces : toset([])

  wait_until_bound = false

  metadata {
    name      = local.scientific_runtime_cache_claim_name
    namespace = each.key
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"        = "scientific-runtime-cache"
      "fast-start.fs2.nebius/storage-role" = "compile-cache"
    })
    annotations = {
      "fs2.nebius.ai/data-classification" = "disposable-derived-cache"
      "fs2.nebius.ai/mount-path"          = local.scientific_runtime_cache_mount_path
    }
  }

  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = var.scientific_batch.runtime_cache.storage_class_name
    volume_mode        = "Filesystem"

    resources {
      requests = {
        storage = "${var.scientific_batch.runtime_cache.size_gib}Gi"
      }
    }
  }

  lifecycle {
    ignore_changes = [
      metadata[0].annotations,
      spec[0].volume_name,
    ]
  }

  depends_on = [
    terraform_data.cluster_contract,
    module.academic_assets,
    module.reference_data,
    kubernetes_manifest.additional_local_queue,
  ]
}

# Prepare only the model-owned boundaries declared above. The root-capable
# container sees no credential, service-account token, network requirement or
# other writable volume. Its checked-in program refuses nested/traversing names
# and applies ownership non-recursively, so existing compiled entries remain
# untouched across Terraform updates.
resource "kubernetes_job_v1" "scientific_runtime_cache_bootstrap" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  metadata {
    name      = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_bootstrap_sha256, 0, 12)}"
    namespace = var.scientific_batch.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
    })
    annotations = {
      "fs2.nebius.ai/runtime-cache-ownership-sha256" = local.scientific_runtime_cache_ownership_sha256
      "fs2.nebius.ai/runtime-cache-bootstrap-sha256" = local.scientific_runtime_cache_bootstrap_sha256
    }
  }

  spec {
    backoff_limit           = 3
    active_deadline_seconds = 600

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
        })
      }

      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector = {
          "workload.fs2.nebius/system"      = "true"
          "capacity.fs2.nebius/pool"        = "system"
          "storage.fs2.nebius/shared-cache" = "true"
        }

        security_context {
          run_as_non_root = false
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name  = "prepare"
          image = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = [
            "python",
            "-c",
            file("${path.module}/scripts/scientific_runtime_cache_bootstrap.py"),
          ]

          env {
            name  = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
            value = jsonencode(local.scientific_runtime_cache_ownership_contract)
          }

          volume_mount {
            name       = "runtime-cache"
            mount_path = local.scientific_runtime_cache_mount_path
            read_only  = false
          }

          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = false
            run_as_user                = 0
            run_as_group               = 0
            capabilities {
              drop = ["ALL"]
              # After chowning a boundary to its model GID, Linux requires
              # CAP_FSETID to retain setgid when the process is not a member
              # of that GID. Without it chmod(02770) silently becomes 0770.
              add = [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
              ]
            }
          }
        }

        volume {
          name = "runtime-cache"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.scientific_runtime_cache[0].metadata[0].name
            read_only  = false
          }
        }
      }
    }
  }

  wait_for_completion = true
  timeouts { create = "15m" }

  lifecycle { create_before_destroy = true }

  depends_on = [
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache,
    terraform_data.scientific_artifacts_contract,
  ]
}

# Additional namespace-local claims receive the same bounded, non-recursive
# ownership bootstrap as the original claim. Each contract contains only the
# model-owned first-level boundaries consumed in that namespace.
resource "kubernetes_job_v1" "scientific_runtime_cache_bootstrap_additional" {
  for_each = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_additional_namespaces : toset([])

  metadata {
    name      = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_additional_bootstrap_sha256[each.key], 0, 12)}"
    namespace = each.key
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
    })
    annotations = {
      "fs2.nebius.ai/runtime-cache-ownership-sha256" = local.scientific_runtime_cache_additional_ownership_sha256[each.key]
      "fs2.nebius.ai/runtime-cache-bootstrap-sha256" = local.scientific_runtime_cache_additional_bootstrap_sha256[each.key]
    }
  }

  spec {
    backoff_limit           = 3
    active_deadline_seconds = 600

    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
        })
      }

      spec {
        restart_policy                  = "Never"
        automount_service_account_token = false
        enable_service_links            = false
        node_selector = {
          "workload.fs2.nebius/system"      = "true"
          "capacity.fs2.nebius/pool"        = "system"
          "storage.fs2.nebius/shared-cache" = "true"
        }

        security_context {
          run_as_non_root = false
          seccomp_profile { type = "RuntimeDefault" }
        }

        container {
          name  = "prepare"
          image = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = [
            "python",
            "-c",
            file("${path.module}/scripts/scientific_runtime_cache_bootstrap.py"),
          ]

          env {
            name  = "FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON"
            value = jsonencode(local.scientific_runtime_cache_additional_ownership_contracts[each.key])
          }

          volume_mount {
            name       = "runtime-cache"
            mount_path = local.scientific_runtime_cache_mount_path
            read_only  = false
          }

          resources {
            requests = { cpu = "25m", memory = "32Mi" }
            limits   = { cpu = "250m", memory = "128Mi" }
          }

          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = false
            run_as_user                = 0
            run_as_group               = 0
            capabilities {
              drop = ["ALL"]
              # CAP_FSETID retains the exact 02770 boundary after chown changes
              # the directory group away from the bootstrap process' group.
              add = [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
              ]
            }
          }
        }

        volume {
          name = "runtime-cache"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.scientific_runtime_cache_additional[each.key].metadata[0].name
            read_only  = false
          }
        }
      }
    }
  }

  wait_for_completion = true
  timeouts { create = "15m" }

  lifecycle { create_before_destroy = true }

  depends_on = [
    kubernetes_persistent_volume_claim_v1.scientific_runtime_cache_additional,
    terraform_data.scientific_artifacts_contract,
  ]
}

# Publishes exactly what the control-plane chart receives, so the projection is
# assertable without standing up the whole stage. Everything here is non-secret.
resource "terraform_data" "scientific_artifacts_contract" {
  input = {
    enabled                  = local.scientific_artifacts_enabled
    namespace                = "fs2-system"
    bucket_name              = try(var.scientific_artifacts.storage_contract.object_storage.name, null)
    object_key               = try(var.scientific_artifacts.storage_contract.layout.object_key, null)
    broker_service_template  = local.scientific_artifacts_enabled ? "${local.scientific_artifact_broker_name}-<sha256(tenant)[0:32]>" : null
    broker_url_template      = local.scientific_artifacts_enabled ? local.scientific_artifact_broker_url_template : null
    provider_bindings_sha256 = local.scientific_artifacts_enabled ? sha256(local.scientific_artifact_provider_bindings_json) : null
    tenant_broker_key_count  = local.scientific_artifacts_enabled ? length(local.scientific_artifact_tenant_access) : 0
    provider_keys_in_gateway = false
    chart_values             = local.scientific_chart_overrides
    batch = {
      enabled        = var.scientific_batch.enabled
      writes_enabled = var.scientific_batch.writes_enabled
      namespace      = var.scientific_batch.namespace
      runtime_cache = {
        enabled            = var.scientific_batch.runtime_cache.enabled
        claim_name         = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_claim_name : null
        mount_path         = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_mount_path : null
        storage_class_name = var.scientific_batch.runtime_cache.storage_class_name
        size_gib           = var.scientific_batch.runtime_cache.size_gib
        ownership = var.scientific_batch.runtime_cache.enabled ? {
          bootstrap_job    = "fs2-scientific-cache-${substr(local.scientific_runtime_cache_bootstrap_sha256, 0, 12)}"
          bootstrap_sha256 = local.scientific_runtime_cache_bootstrap_sha256
          contract_sha256  = local.scientific_runtime_cache_ownership_sha256
          directories      = local.scientific_runtime_cache_directories
        } : null
        namespace_claims = local.scientific_runtime_cache_namespace_claims
        consumers = sort([
          for mount in local.scientific_runtime_cache_mounts : "${mount.model_id}/${mount.stage_id}"
        ])
      }
    }
  }

  lifecycle {
    precondition {
      condition = !local.scientific_artifacts_enabled || length(distinct([
        for tenant_id in keys(local.scientific_artifact_tenant_access) : substr(sha256(tenant_id), 0, 32)
      ])) == length(local.scientific_artifact_tenant_access)
      error_message = "Scientific artifact tenant IDs collide in the broker resource-name hash namespace; routing is refused until every 32-hex suffix is unique."
    }
    precondition {
      condition = !local.scientific_artifacts_enabled || (
        length(local.scientific_artifact_authority_keyring) >= 1 &&
        contains(keys(local.scientific_artifact_authority_keyring), var.scientific_artifacts.broker.authority_verification_key) &&
        alltrue([
          for name, value in local.scientific_artifact_authority_keyring :
          endswith(name, ".pem") && length(trimspace(value)) >= 80
        ])
      )
      error_message = "The artifact authority verification ConfigMap must contain a non-empty PEM key ring including the configured compatibility key; its content digest drives every issuer/broker rollout."
    }
    precondition {
      condition     = !var.scientific_batch.enabled || var.scientific_artifacts.enabled
      error_message = "staged scientific batch execution requires the dedicated artifact store; a batch cannot commit an immutable result manifest without it."
    }
    precondition {
      condition     = !var.scientific_batch.writes_enabled || var.scientific_batch.enabled
      error_message = "scientific batch Kubernetes writes require the batch controller gate."
    }
    precondition {
      condition = (
        !var.scientific_batch.enabled || (
          (length(local.scientific_runtime_cache_mounts) == 0 || var.scientific_batch.runtime_cache.enabled) &&
          (!var.scientific_batch.runtime_cache.enabled || length(local.scientific_runtime_cache_mounts) > 0) &&
          alltrue([
            for mount in local.scientific_runtime_cache_mounts :
            mount.claim_name == local.scientific_runtime_cache_claim_name &&
            mount.host_path == null &&
            mount.mount_path == local.scientific_runtime_cache_mount_path &&
            mount.sub_path == null &&
            mount.read_only == false
          ])
        )
      )
      error_message = "A scientific runtime cache must be enabled exactly when the execution map consumes it, and every consumer must use the Terraform-owned writable fs2-scientific-runtime-cache claim at /cache."
    }
    precondition {
      condition = (
        !var.scientific_batch.runtime_cache.enabled || (
          length(local.scientific_runtime_cache_consumers) > 0 &&
          alltrue([
            for consumer in local.scientific_runtime_cache_consumers :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", consumer.workload_namespace)) &&
            length(consumer.cache_paths) > 0 &&
            length(distinct([
              for path in consumer.cache_paths : split("/", path)[2]
            ])) == 1
          ]) &&
          alltrue([
            for claim in local.scientific_runtime_cache_directory_claims :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.name)) &&
            try(claim.uid >= 1 && claim.uid <= 2147483647, false) &&
            try(claim.gid >= 1 && claim.gid <= 2147483647, false)
          ]) &&
          alltrue([
            for claims in values(local.scientific_runtime_cache_directory_claims_by_name) :
            length(distinct([for claim in claims : claim.uid])) == 1 &&
            length(distinct([for claim in claims : claim.gid])) == 1
          ])
        )
      )
      error_message = "Every runtime-cache stage must declare one safe first-level /cache directory whose exact non-root UID/GID agrees across consumers."
    }
    precondition {
      condition = (
        !local.scientific_artifacts_enabled ||
        var.scientific_artifacts.storage_contract.object_storage.name != try(var.reference_data.storage_contract.object_storage.name, null)
      )
      error_message = "the scientific result store must be a bucket distinct from the reference-data plane."
    }
    precondition {
      condition = (
        !local.scientific_artifacts_enabled ||
        var.scientific_artifacts.storage_contract.region == var.target_contract.region
      )
      error_message = "the scientific artifact bucket must be in the cluster region; finalize streams every stored object back to verify its digest."
    }
  }
}

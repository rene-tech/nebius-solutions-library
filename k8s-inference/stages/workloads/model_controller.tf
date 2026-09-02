locals {
  # Benchmark receipts are projected by models/cold-start/project_fast_start_evidence.py.
  # Keeping this optional file outside terraform.tfvars avoids embedding a large,
  # machine-generated evidence cohort in the human-authored cluster settings.
  model_controller_fast_start_evidence = (
    var.model_controller.fast_start_evidence_file == null ? {} :
    jsondecode(file(pathexpand(var.model_controller.fast_start_evidence_file)))
  )
  model_controller_fast_start_evidence_valid = try(alltrue([
    for model_id, evidence in local.model_controller_fast_start_evidence :
    contains(local.model_controller_dynamic_model_ids, model_id) &&
    length(evidence) <= 256 &&
    alltrue([
      for item in evidence :
      length(keys(item)) == 15 && length(setsubtract(toset(keys(item)), toset([
        "receiptDigest",
        "mechanism",
        "compatibilityTupleDigest",
        "compatibilityTupleComplete",
        "measurementBasis",
        "acceleratorClass",
        "poolRef",
        "acceleratorsPerReplica",
        "artifactManifestDigest",
        "runtimeImage",
        "templateDigest",
        "cacheTier",
        "snapshotDigest",
        "samples",
        "validUntil",
      ]))) == 0 &&
      can(regex("^sha256:[a-f0-9]{64}$", item.receiptDigest)) &&
      can(regex("^[a-z][a-z0-9-]{0,63}$", item.mechanism)) &&
      can(regex("^sha256:[a-f0-9]{64}$", item.compatibilityTupleDigest)) &&
      (item.compatibilityTupleComplete == true || item.compatibilityTupleComplete == false) &&
      item.measurementBasis == "CapacityAvailableToSemanticReady" &&
      length(trimspace(item.acceleratorClass)) >= 1 && length(item.acceleratorClass) <= 128 &&
      (
        item.poolRef == null ||
        can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", item.poolRef)) && length(item.poolRef) <= 128
      ) &&
      floor(item.acceleratorsPerReplica) == item.acceleratorsPerReplica &&
      item.acceleratorsPerReplica >= 1 && item.acceleratorsPerReplica <= 64 &&
      can(regex("^sha256:[a-f0-9]{64}$", item.artifactManifestDigest)) &&
      can(regex("^[^[:space:]@]+@sha256:[a-f0-9]{64}$", item.runtimeImage)) && length(item.runtimeImage) <= 768 &&
      can(regex("^sha256:[a-f0-9]{64}$", item.templateDigest)) &&
      contains(["Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"], item.cacheTier) &&
      (item.snapshotDigest == null || can(regex("^sha256:[a-f0-9]{64}$", item.snapshotDigest))) &&
      length(item.samples) >= 1 && length(item.samples) <= 256 &&
      alltrue([
        for sample in item.samples :
        length(keys(sample)) == 4 && length(setsubtract(toset(keys(sample)), toset([
          "observedAt",
          "modelStartSeconds",
          "capacityWaitSeconds",
          "endToEndSeconds",
        ]))) == 0 &&
        can(timecmp(sample.observedAt, sample.observedAt)) &&
        (
          sample.modelStartSeconds == null ||
          sample.modelStartSeconds >= 0 && sample.modelStartSeconds <= 86400
        ) &&
        (
          sample.capacityWaitSeconds == null ||
          sample.capacityWaitSeconds >= 0 && sample.capacityWaitSeconds <= 604800
        ) &&
        (
          sample.endToEndSeconds == null ||
          sample.endToEndSeconds >= 0 && sample.endToEndSeconds <= 604800
        ) && (
          sample.endToEndSeconds == null ||
          (sample.modelStartSeconds == null || sample.modelStartSeconds <= sample.endToEndSeconds) &&
          (sample.capacityWaitSeconds == null || sample.capacityWaitSeconds <= sample.endToEndSeconds)
        )
      ]) &&
      (item.validUntil == null || can(timecmp(item.validUntil, item.validUntil)))
    ])
  ]), false)
  model_controller_supported_template_gvks = toset([
    "v1/ConfigMap",
    "v1/Service",
    "v1/ServiceAccount",
    "apps/v1/Deployment",
  ])

  # Reuse the exact rendered, image-rewritten and placement-checked documents
  # from the legacy Terraform path. A bundle is only promoted into the live
  # controller contract after its artifact, runtime and accelerator evidence
  # joins successfully below.
  model_controller_candidate_bundle_resources = {
    for model_id in local.selected_model_ids : model_id => [
      for document in local.model_documents : document.manifest
      if document.model_id == model_id && contains(
        local.model_controller_supported_template_gvks,
        "${document.manifest.apiVersion}/${document.manifest.kind}",
      )
    ]
  }
  model_controller_candidate_template_digests = {
    for model_id, resources in local.model_controller_candidate_bundle_resources :
    model_id => "sha256:${sha256(jsonencode(resources))}"
  }
  model_controller_bundle_requires_shared_cache = {
    # Cache PVCs remain Terraform-owned across the explicit serving-resource
    # handoff. This preserves already-localized model bytes and gives the live
    # controller a stable infrastructure dependency instead of deleting and
    # recreating the shared-cache directory during adoption.
    for model_id in local.selected_model_ids : model_id => anytrue([
      for document in local.model_documents :
      document.manifest.kind == "PersistentVolumeClaim"
      if document.model_id == model_id
    ])
  }
  model_controller_primary_deployments = {
    for model_id in local.selected_model_ids : model_id => one([
      for document in local.model_documents : document.manifest
      if document.model_id == model_id &&
      document.manifest.kind == "Deployment" &&
      document.manifest.metadata.name == local.profile_contract.model_autoscaling_targets[model_id].deployment
    ])
  }
  model_controller_runtime_container_names = {
    for model_id, deployment in local.model_controller_primary_deployments : model_id => one([
      for container in deployment.spec.template.spec.containers : container.name
      if anytrue([
        for resource_name in setunion(
          toset(keys(try(container.resources.requests, {}))),
          toset(keys(try(container.resources.limits, {}))),
        ) : endswith(resource_name, "/gpu") || resource_name == "nvidia.com/gpu"
      ])
    ])
  }
  # Static bootstrap needs one exact pool so Terraform can own a deterministic
  # Pod selector. Once the controller is the sole serving-resource writer it
  # may place the same qualified runtime on every selected compatible pool.
  # This is what lets one ModelDeployment prefer always-on capacity while also
  # retaining preemptible burst pools without duplicating model manifests.
  model_controller_pool_ids = {
    for model_id in local.selected_model_ids : model_id => sort([
      for pool_id, pool in local.selected_queue_pools : pool_id
      if !local.model_controller_bundle_requires_shared_cache[model_id] || pool.features.shared_filesystem
    ])
  }

  # A catalog entry is not qualification evidence by itself. Join the real
  # platform-verified artifact manifest to the retained runtime observation and
  # the enabled hardware compatibility tuple. Never synthesize missing digests
  # or turn a declaration-only accelerator candidate into a live capability.
  model_controller_qualification_rows = {
    for row in local.qualification_projection.rows : row.model_id => row
  }
  model_controller_accelerator_compatibility = jsondecode(file(
    "${local.fs2_root}/catalog/profiles/model-accelerator-compatibility.json"
  ))
  model_controller_required_runtime_states = toset([
    "registered",
    "route_active",
    "runtime_ready",
    "semantic_qualified",
    "http_mcp_qualified",
  ])
  model_controller_hardware_qualified_accelerator_classes = {
    for model_id in local.selected_model_ids : model_id => sort(distinct([
      for binding in try(
        local.model_controller_accelerator_compatibility.models[model_id].runtimes["catalog-canonical"].bindings,
        [],
      ) : binding.accelerator_class
      if try(binding.enabled && binding.state == "hardware-validated", false)
    ]))
  }
  model_controller_qualified_pool_ids = {
    for model_id in local.selected_model_ids : model_id => sort([
      for pool_id in local.model_controller_pool_ids[model_id] : pool_id
      if contains(
        local.model_controller_hardware_qualified_accelerator_classes[model_id],
        local.selected_queue_pools[pool_id].accelerator_class,
        ) && local.selected_queue_pools[pool_id].node.gpus_per_node >= local.profile_contract.model_autoscaling_targets[model_id].gpu_count && length(setintersection(
          toset(local.selected_queue_pools[pool_id].node.host_architectures),
          toset(try(
            local.model_controller_accelerator_compatibility.models[model_id].runtimes["catalog-canonical"].requirements.host_architectures,
            [],
          )),
      )) > 0
    ])
  }
  model_controller_qualification_checks = {
    for model_id in local.selected_model_ids : model_id => {
      artifact_manifest = try(
        local.catalog_models[model_id].cache.artifact.state == "platform-verified" &&
        can(regex("^[0-9a-f]{64}$", local.catalog_models[model_id].cache.artifact.manifest_digest)),
        false,
      )
      base_catalog = try(
        local.catalog_models[model_id].support.state == "qualified" &&
        local.catalog_models[model_id].model.source.license.state == "verified" &&
        contains(
          ["not-required", "verified"],
          local.catalog_models[model_id].model.source.entitlement.state,
        ) &&
        length(trimspace(local.catalog_models[model_id].model.source.revision)) > 0 &&
        local.catalog_models[model_id].runtime.image.state == "resolved" &&
        can(regex("^sha256:[0-9a-f]{64}$", local.catalog_models[model_id].runtime.image.digest)) &&
        endswith(
          local.catalog_models[model_id].runtime.image.reference,
          "@${local.catalog_models[model_id].runtime.image.digest}",
        ) &&
        local.catalog_models[model_id].interface.execution_mode == "http" &&
        length(local.catalog_models[model_id].interface.protocols) > 0 &&
        length(local.catalog_models[model_id].interface.policy.operations) > 0 &&
        toset(local.catalog_models[model_id].interface.protocols) == toset(keys(local.catalog_models[model_id].interface.endpoints)) &&
        alltrue([
          for endpoint in values(local.catalog_models[model_id].interface.endpoints) :
          startswith(endpoint, "/")
        ]) &&
        local.inventory.routes[model_id].protocols == local.catalog_models[model_id].interface.endpoints &&
        local.inventory.routes[model_id].operations == local.catalog_models[model_id].interface.policy.operations &&
        (!local.inventory.routes[model_id].mcp.enabled || local.catalog_models[model_id].interface.mcp.discoverable),
        false,
      )
      retained_runtime = try(
        local.model_controller_qualification_rows[model_id].variant_id == null &&
        local.model_controller_qualification_rows[model_id].active_runtime.model_revision == local.catalog_models[model_id].model.source.revision &&
        local.model_controller_qualification_rows[model_id].active_runtime.runtime_image_digest == local.catalog_models[model_id].runtime.image.digest &&
        endswith(
          var.model_image_overrides[model_id],
          "@${local.model_controller_qualification_rows[model_id].active_runtime.runtime_image_digest}",
        ) &&
        local.model_controller_qualification_rows[model_id].active_runtime.service == {
          namespace = local.inventory.namespace
          name      = local.inventory.routes[model_id].service.name
          port      = local.inventory.routes[model_id].service.port
        } &&
        alltrue([
          for state in local.model_controller_required_runtime_states :
          local.model_controller_qualification_rows[model_id].states[state]
        ]),
        false,
      )
      accelerator_tuple = try(
        local.model_controller_accelerator_compatibility.models[model_id].runtimes["catalog-canonical"].requirements.gpu_count == local.profile_contract.model_autoscaling_targets[model_id].gpu_count &&
        length(local.model_controller_qualified_pool_ids[model_id]) > 0,
        false,
      )
      renderer_bundle = try(
        one([
          for container in local.model_controller_primary_deployments[model_id].spec.template.spec.containers : container.image
          if container.name == local.model_controller_runtime_container_names[model_id]
        ]) == var.model_image_overrides[model_id] &&
        length(local.model_controller_candidate_bundle_resources[model_id]) > 0,
        false,
      )
    }
  }
  model_controller_retained_tuple_qualified = {
    for model_id, checks in local.model_controller_qualification_checks :
    model_id => alltrue(values(checks))
  }
  model_controller_ineligible_reasons = {
    for model_id, checks in local.model_controller_qualification_checks : model_id => sort([
      for check, passed in checks : check
      if !passed
    ]) if !alltrue(values(checks))
  }
  model_controller_dynamic_model_ids = sort([
    for model_id, qualified in local.model_controller_retained_tuple_qualified : model_id
    if qualified
  ])
  model_controller_bundle_resources = {
    for model_id in local.model_controller_dynamic_model_ids :
    model_id => local.model_controller_candidate_bundle_resources[model_id]
  }
  model_controller_template_digests = {
    for model_id in local.model_controller_dynamic_model_ids :
    model_id => local.model_controller_candidate_template_digests[model_id]
  }
  model_controller_artifact_manifest_digests = {
    for model_id in local.model_controller_dynamic_model_ids :
    model_id => "sha256:${local.catalog_models[model_id].cache.artifact.manifest_digest}"
  }
  model_controller_bundles = [
    for model_id in local.model_controller_dynamic_model_ids : {
      modelRef             = model_id
      runtimeProfile       = local.catalog_models[model_id].runtime.kind
      templateDigest       = local.model_controller_template_digests[model_id]
      primaryWorkloadName  = local.profile_contract.model_autoscaling_targets[model_id].deployment
      runtimeContainerName = local.model_controller_runtime_container_names[model_id]
      primaryServiceName   = local.inventory.routes[model_id].service.name
      primaryServicePort   = local.inventory.routes[model_id].service.port
      resources            = local.model_controller_bundle_resources[model_id]
    }
  ]
  model_controller_qualifications = {
    for model_id in local.model_controller_dynamic_model_ids : model_id => {
      modelRef                = model_id
      runtimeProfile          = local.catalog_models[model_id].runtime.kind
      artifactManifestDigests = [local.model_controller_artifact_manifest_digests[model_id]]
      artifactRevisions = {
        (local.catalog_models[model_id].model.source.revision) = local.model_controller_artifact_manifest_digests[model_id]
      }
      runtimeImages             = [var.model_image_overrides[model_id]]
      acceleratorClasses        = sort(distinct([for pool_id in local.model_controller_qualified_pool_ids[model_id] : local.selected_queue_pools[pool_id].accelerator_class]))
      maxAcceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
      scaleToZeroQualified      = try(local.model_controller_qualification_rows[model_id].states.elasticity_qualified, false)
      templateDigests           = [local.model_controller_template_digests[model_id]]
      templateRefs = {
        "${model_id}.legacy-v1" = local.model_controller_template_digests[model_id]
      }
      templateCacheTiers = {
        (local.model_controller_template_digests[model_id]) = local.model_controller_bundle_requires_shared_cache[model_id] ? "SharedFilesystem" : "NodeLocal"
      }
      openAIQualified = anytrue([
        for protocol in keys(local.inventory.routes[model_id].protocols) :
        startswith(protocol, "openai")
      ])
      mcpToolName = (
        local.inventory.routes[model_id].mcp.enabled ?
        local.inventory.routes[model_id].mcp.tool_name :
        null
      )
      snapshotDigests = []
      # Fast-start levels (L1..L4) are qualified only by retained benchmark
      # evidence measured from GPU capacity being available until semantic
      # endpoint readiness for the exact artifact, image, template, cache tier
      # and accelerator tuple. The retained elasticity receipts measure
      # activation-to-ready, which includes capacity wait, so they are not
      # compatible evidence. Until a fast-start benchmark receipt is retained
      # and projected here, every level above Off stays unqualified and the
      # controller reports that truthfully.
      fastStartEvidence = try(local.model_controller_fast_start_evidence[model_id], [])
    }
  }
  model_controller_pool_envelope = {
    for pool_id, pool in local.selected_queue_pools : pool_id => {
      poolId              = pool_id
      acceleratorClass    = pool.accelerator_class
      resourceName        = pool.resource_api.resource_name
      capacityType        = pool.capacity.type
      acceleratorsPerNode = pool.node.gpus_per_node
      minNodes            = pool.capacity.min_nodes
      maxNodes            = pool.capacity.max_nodes
      nodeSelector        = pool.scheduling.stable_node_labels
      tolerations         = pool.scheduling.tolerations
    }
  }
  model_controller_envelope_without_revision = {
    pools                         = local.model_controller_pool_envelope
    qualifications                = local.model_controller_qualifications
    localQueues                   = [local.selected_accelerator_pool_profile.queue.local_queue_name]
    priorityClasses               = sort(keys(var.model_controller.priority_classes))
    tenantIds                     = [local.selected_target.tenant_id]
    maxAcceleratorsPerModel       = sum([for pool in values(local.selected_queue_pools) : pool.node.gpus_per_node * pool.capacity.max_nodes])
    fastStartWaitSecondValue      = var.model_controller.fast_start_wait_second_value
    fastStartMechanismHourlyCosts = var.model_controller.fast_start_mechanism_hourly_costs
  }
  model_controller_envelope = merge(local.model_controller_envelope_without_revision, {
    revision = "sha256:${sha256(jsonencode(local.model_controller_envelope_without_revision))}"
  })
  model_controller_envelope_json = var.model_controller.enabled ? jsonencode(local.model_controller_envelope) : ""
  model_controller_bundles_json  = var.model_controller.enabled ? jsonencode(local.model_controller_bundles) : ""
  model_controller_envelope_name = var.model_controller.enabled ? format(
    "fs2-model-envelope-%s",
    substr(sha256(local.model_controller_envelope_json), 0, 16),
  ) : ""
  model_controller_bundles_name = var.model_controller.enabled ? format(
    "fs2-model-bundles-%s",
    substr(sha256(local.model_controller_bundles_json), 0, 16),
  ) : ""

  # An existing deployment uses two explicit applies. `released` removes only
  # controller-supported legacy objects with writes disabled and emits this
  # receipt. The following `controller` apply must present that exact receipt.
  model_controller_handoff_payload = {
    schema          = "fs2-serve.nebius.ai/terraform-model-handoff/v1"
    runId           = var.run_id
    modelIds        = local.model_controller_dynamic_model_ids
    templateDigests = local.model_controller_template_digests
    resourceIdentities = sort(flatten([
      for model_id, resources in local.model_controller_bundle_resources : [
        for resource in resources : "${resource.apiVersion}/${resource.kind}/${resource.metadata.namespace}/${resource.metadata.name}"
      ]
    ]))
  }
  model_controller_expected_handoff_receipt = "sha256:${sha256(jsonencode(local.model_controller_handoff_payload))}"

  # One identity has one writer in every mode. Security and other unsupported
  # GVKs remain Terraform-owned after serving resources are released.
  terraform_owned_model_manifests = {
    for key, document in local.model_manifests : key => document
    if(
      var.model_controller.workload_owner == "terraform" ||
      !contains(local.model_controller_dynamic_model_ids, document.model_id) ||
      !contains(
        local.model_controller_supported_template_gvks,
        "${document.manifest.apiVersion}/${document.manifest.kind}",
      )
    )
  }
  terraform_owned_model_scalers = {
    for model_id, scaler in local.model_scalers : model_id => scaler
    if var.model_controller.workload_owner == "terraform" || !contains(local.model_controller_dynamic_model_ids, model_id)
  }

  model_controller_bootstrap_proposals = {
    for model_id in sort(tolist(setintersection(
      var.model_controller.bootstrap_model_ids,
      toset(local.model_controller_dynamic_model_ids),
      ))) : model_id => {
      name      = model_id
      namespace = "fs2-models"
      base_etag = null
      spec = {
        modelRef  = model_id
        tenantId  = local.selected_target.tenant_id
        lifecycle = { desiredState = "Enabled" }
        artifact = {
          revision       = local.catalog_models[model_id].model.source.revision
          manifestDigest = local.model_controller_artifact_manifest_digests[model_id]
        }
        runtime = {
          profile = local.catalog_models[model_id].runtime.kind
          image   = var.model_image_overrides[model_id]
          templateRef = {
            name   = "${model_id}.legacy-v1"
            digest = local.model_controller_template_digests[model_id]
          }
        }
        placement = {
          poolRefs               = local.model_controller_qualified_pool_ids[model_id]
          acceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
          topologyPolicy         = "SingleNode"
        }
        availability = {
          minReplicas            = local.model_scalers[model_id].min_replicas
          maxReplicas            = local.model_scalers[model_id].max_replicas
          idleSeconds            = local.model_scalers[model_id].cooldown_seconds
          targetQueueDepth       = local.model_scalers[model_id].target_queue_depth
          pollingIntervalSeconds = local.model_scalers[model_id].polling_interval_seconds
          cooldownSeconds        = local.model_scalers[model_id].cooldown_seconds
          warmWindows            = []
        }
        cache = {
          tier = anytrue([
            for pool_id in local.model_controller_pool_ids[model_id] : local.selected_queue_pools[pool_id].features.shared_filesystem
          ]) ? "SharedFilesystem" : "NodeLocal"
          snapshotPreference = "Never"
        }
        queue = {
          localQueue      = local.selected_accelerator_pool_profile.queue.local_queue_name
          priorityClass   = "standard"
          maxQueueSeconds = 7200
        }
        rollout = {
          strategy                = "Recreate"
          maxUnavailable          = 1
          maxSurge                = 0
          progressDeadlineSeconds = 7200
        }
        exposure = {
          openAI = anytrue([
            for protocol in keys(local.inventory.routes[model_id].protocols) : startswith(protocol, "openai")
          ])
          openAIAliases = []
          mcp           = local.inventory.routes[model_id].mcp.enabled
          mcpToolName   = local.inventory.routes[model_id].mcp.enabled ? local.inventory.routes[model_id].mcp.tool_name : null
        }
        policy = {
          visibility          = "Tenant"
          policyRef           = "tenant-default.v1"
          allowedPrincipalIds = []
        }
        adoption = { mode = "None" }
      }
    }
  }
  model_controller_bootstrap_payload = {
    schema    = "fs2-serve.nebius.ai/model-bootstrap/v1"
    proposals = values(local.model_controller_bootstrap_proposals)
  }
  # The bootstrap Job and immutable ConfigMap must roll when either desired
  # seeds, this bounded implementation, or its exact runtime image changes.
  model_controller_bootstrap_identity = {
    payload_sha256        = sha256(jsonencode(local.model_controller_bootstrap_payload))
    implementation_sha256 = filesha256("${path.module}/model_controller.tf")
    runtime_image         = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
  }
  model_controller_bootstrap_digest = sha256(jsonencode(local.model_controller_bootstrap_identity))
  model_controller_bootstrap_enabled = (
    var.model_controller.workload_owner == "controller" &&
    length(var.model_controller.bootstrap_model_ids) > 0
  )
}

resource "terraform_data" "model_controller_contract" {
  input = {
    enabled                  = var.model_controller.enabled
    writes_enabled           = var.model_controller.writes_enabled
    workload_owner           = var.model_controller.workload_owner
    envelope_sha256          = sha256(local.model_controller_envelope_json)
    renderer_bundles_sha256  = sha256(local.model_controller_bundles_json)
    bootstrap_model_ids      = sort(tolist(var.model_controller.bootstrap_model_ids))
    expected_handoff_receipt = local.model_controller_expected_handoff_receipt
  }

  lifecycle {
    precondition {
      condition     = local.model_controller_fast_start_evidence_valid
      error_message = "Fast-start evidence must map only controller-qualified model IDs to the exact bounded wire shape emitted by project_fast_start_evidence.py."
    }

    precondition {
      condition = !var.model_controller.enabled || (
        length(local.model_controller_dynamic_model_ids) > 0 &&
        length(local.model_controller_envelope_json) <= 900000 &&
        length(local.model_controller_bundles_json) <= 900000 &&
        alltrue([for resources in values(local.model_controller_bundle_resources) : length(resources) > 0]) &&
        alltrue([for model_id in local.model_controller_dynamic_model_ids :
          length(local.model_controller_qualified_pool_ids[model_id]) > 0 &&
          can(regex("@sha256:[0-9a-f]{64}$", var.model_image_overrides[model_id]))
        ])
      )
      error_message = "The dynamic controller requires at least one selected model with an exact platform-verified artifact manifest, qualified base catalog source, retained ready runtime identity, hardware-qualified accelerator tuple, and immutable renderer bundle. Ineligible checks: ${jsonencode(local.model_controller_ineligible_reasons)}."
    }

    precondition {
      condition = length(setsubtract(
        var.model_controller.bootstrap_model_ids,
        toset(local.model_controller_dynamic_model_ids),
      )) == 0
      error_message = "Every bootstrap model must pass the retained artifact/runtime/accelerator/template qualification join. Ineligible bootstrap IDs: ${jsonencode(sort(tolist(setsubtract(var.model_controller.bootstrap_model_ids, toset(local.model_controller_dynamic_model_ids)))))}; failed checks: ${jsonencode(local.model_controller_ineligible_reasons)}."
    }

    precondition {
      condition = alltrue([
        for model_id in var.model_controller.bootstrap_model_ids :
        local.model_scalers[model_id].min_replicas > 0 ||
        try(local.model_controller_qualification_rows[model_id].states.elasticity_qualified, false)
      ])
      error_message = "A bootstrap model may scale to zero only when retained evidence marks elasticity_qualified=true; otherwise include it in models.scaling.hot or set a positive min_replicas override."
    }

    precondition {
      condition = (
        var.model_controller.workload_owner != "controller" ||
        var.model_controller.fresh_install ||
        var.model_controller.handoff_receipt == local.model_controller_expected_handoff_receipt
      )
      error_message = "An existing static deployment must first apply workload_owner=released and then copy its dynamic_model_handoff_receipt output into deployment.dynamic_models.handoff_receipt."
    }
  }
}

resource "kubernetes_config_map_v1" "model_controller_envelope" {
  count = var.model_controller.enabled ? 1 : 0

  metadata {
    name      = local.model_controller_envelope_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-controller-contract" })
  }
  immutable = true
  data      = { "infrastructure-envelope.json" = local.model_controller_envelope_json }

  lifecycle { create_before_destroy = true }
  depends_on = [terraform_data.cluster_contract, terraform_data.model_controller_contract]
}

resource "kubernetes_config_map_v1" "model_controller_bundles" {
  count = var.model_controller.enabled ? 1 : 0

  metadata {
    name      = local.model_controller_bundles_name
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-controller-contract" })
  }
  immutable = true
  data      = { "renderer-bundles.json" = local.model_controller_bundles_json }

  lifecycle { create_before_destroy = true }
  depends_on = [terraform_data.cluster_contract, terraform_data.model_controller_contract]
}

resource "kubernetes_config_map_v1" "model_controller_bootstrap" {
  count = local.model_controller_bootstrap_enabled ? 1 : 0

  metadata {
    name      = "fs2-model-bootstrap-${substr(local.model_controller_bootstrap_digest, 0, 16)}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap" })
  }
  immutable = true
  data = {
    "bootstrap-identity.json" = jsonencode(local.model_controller_bootstrap_identity)
    "bootstrap.json"          = jsonencode(local.model_controller_bootstrap_payload)
    "bootstrap.py"            = <<-PY
      import json
      import os
      import time
      import urllib.error
      import urllib.parse
      import urllib.request
      from http.cookies import SimpleCookie
      from pathlib import Path

      base = os.environ["FS2_BOOTSTRAP_BASE_URL"].rstrip("/")
      public_origin = os.environ["FS2_BOOTSTRAP_PUBLIC_ORIGIN"].rstrip("/")
      public_authority = urllib.parse.urlsplit(public_origin).netloc
      token = Path("/var/run/fs2-admin/token").read_text(encoding="utf-8").strip()
      payload = json.loads(Path("/bootstrap/bootstrap.json").read_text(encoding="utf-8"))

      def call(path, *, method="GET", body=None, cookie=None, accepted=(200,)):
          headers = {"Accept": "application/json", "Host": public_authority, "Origin": public_origin}
          if body is not None:
              headers["Content-Type"] = "application/json"
          if cookie is not None:
              headers["Cookie"] = cookie
          request = urllib.request.Request(
              base + path,
              data=None if body is None else json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
              headers=headers,
              method=method,
          )
          try:
              response = urllib.request.urlopen(request, timeout=15)
              document = json.loads(response.read()) if response.status != 204 else None
              if response.status not in accepted:
                  raise RuntimeError(f"unexpected HTTP {response.status} for {path}")
              return response.status, document, response.headers
          except urllib.error.HTTPError as error:
              raw = error.read()
              document = json.loads(raw) if raw else None
              if error.code in accepted:
                  return error.code, document, error.headers
              code = document.get("code", "unknown") if isinstance(document, dict) else "unknown"
              raise RuntimeError(f"HTTP {error.code} ({code}) for {path}") from None

      session = None
      for attempt in range(12):
          try:
              request = urllib.request.Request(
                  base + "/admin/api/v1/session",
                  data=b"{}",
                  headers={
                      "Authorization": "Bearer " + token,
                      "Content-Type": "application/json",
                      "Host": public_authority,
                      "Origin": public_origin,
                  },
                  method="POST",
              )
              response = urllib.request.urlopen(request, timeout=15)
              cookies = SimpleCookie()
              cookies.load(response.headers["Set-Cookie"])
              session = "__Host-fs2_admin_session=" + cookies["__Host-fs2_admin_session"].value
              break
          except (OSError, urllib.error.URLError, KeyError):
              if attempt == 11:
                  raise
              time.sleep(5)
      if session is None:
          raise RuntimeError("admin session bootstrap did not become ready")

      for proposal in payload["proposals"]:
          name = proposal["name"]
          status, current, _ = call(
              "/admin/api/v1/model-deployments/" + urllib.parse.quote(name, safe="") + "?namespace=fs2-models",
              cookie=session,
              accepted=(200, 404),
          )
          if status == 200:
              spec = current["data"]["spec"]
              if spec["modelRef"] != proposal["spec"]["modelRef"] or spec["tenantId"] != proposal["spec"]["tenantId"]:
                  raise RuntimeError(f"existing bootstrap identity differs for {name}")
              print(f"model bootstrap preserved existing desired revision: {name}")
              continue

          _, planned, _ = call(
              "/admin/api/v1/model-deployments:plan-preview",
              method="POST",
              body=proposal,
              cookie=session,
          )
          preview = planned["data"]
          if preview["decision"]["disposition"] != "accepted" or not preview["mutation_supported"]:
              raise RuntimeError(f"model bootstrap was not admitted: {name}")
          apply_body = {
              "preview_id": preview["preview_id"],
              "proposed_etag": preview["proposed_etag"],
              "proposal": proposal,
              "idempotency_key": "terraform-bootstrap-" + preview["proposed_etag"].removeprefix("sha256:"),
          }
          _, applied, _ = call(
              "/admin/api/v1/model-deployments:apply",
              method="POST",
              body=apply_body,
              cookie=session,
          )
          projection = applied["data"]["projection"]
          if projection not in ("applied", "pending"):
              raise RuntimeError(f"model bootstrap returned an invalid projection for {name}")
          print(f"model bootstrap stored desired revision: {name} ({projection})")
    PY
  }

  lifecycle { create_before_destroy = true }
  depends_on = [terraform_data.model_controller_contract]
}

resource "kubernetes_network_policy_v1" "model_controller_bootstrap" {
  count = local.model_controller_bootstrap_enabled ? 1 : 0

  metadata {
    name      = "fs2-model-bootstrap-to-control-plane"
    namespace = "fs2-system"
    labels    = local.common_labels
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/name"      = "fs2-serve-control-plane"
        "app.kubernetes.io/component" = "gateway"
      }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector {
          match_labels = { "app.kubernetes.io/component" = "model-bootstrap" }
        }
      }
      ports {
        port     = "8080"
        protocol = "TCP"
      }
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_job_v1" "model_controller_bootstrap" {
  count = local.model_controller_bootstrap_enabled ? 1 : 0

  metadata {
    name      = "fs2-model-bootstrap-${substr(local.model_controller_bootstrap_digest, 0, 16)}"
    namespace = "fs2-system"
    labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap" })
  }
  wait_for_completion = true
  timeouts { create = "15m" }

  spec {
    backoff_limit           = 4
    active_deadline_seconds = 600
    template {
      metadata {
        labels = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap" })
      }
      spec {
        automount_service_account_token = false
        restart_policy                  = "Never"
        container {
          name    = "bootstrap"
          image   = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
          command = ["python", "/bootstrap/bootstrap.py"]
          env {
            name  = "FS2_BOOTSTRAP_BASE_URL"
            value = "http://fs2-serve-control-plane.fs2-system.svc.cluster.local:8080"
          }
          env {
            name  = "FS2_BOOTSTRAP_PUBLIC_ORIGIN"
            value = local.public_base_url
          }
          resources {
            requests = { cpu = "25m", memory = "64Mi" }
            limits   = { cpu = "250m", memory = "256Mi" }
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            run_as_non_root            = true
            run_as_user                = 65532
            capabilities { drop = ["ALL"] }
          }
          volume_mount {
            name       = "bootstrap"
            mount_path = "/bootstrap"
            read_only  = true
          }
          volume_mount {
            name       = "admin-token"
            mount_path = "/var/run/fs2-admin"
            read_only  = true
          }
        }
        volume {
          name = "bootstrap"
          config_map { name = kubernetes_config_map_v1.model_controller_bootstrap[0].metadata[0].name }
        }
        volume {
          name = "admin-token"
          secret {
            secret_name = kubernetes_secret_v1.admin.metadata[0].name
            items {
              key  = "token"
              path = "token"
            }
          }
        }
      }
    }
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_network_policy_v1.model_controller_bootstrap,
  ]
}

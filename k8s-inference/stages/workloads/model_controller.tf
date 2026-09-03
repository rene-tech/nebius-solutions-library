locals {
  # Benchmark receipts are projected by models/cold-start/project_fast_start_evidence.py.
  # Keeping this optional file outside terraform.tfvars avoids embedding a large,
  # machine-generated evidence cohort in the human-authored cluster settings.
  model_controller_fast_start_evidence = (
    var.model_controller.fast_start_evidence_file == null ? {} :
    jsondecode(file(pathexpand(var.model_controller.fast_start_evidence_file)))
  )
  # Environment qualifications are generated from an observed node/runtime
  # probe and measurement contracts are generated from the exact benchmark
  # payload/client. They are explicit files because Terraform must never invent
  # a driver, CUDA, GPU product, storage path, or semantic readiness contract.
  model_controller_fast_start_environment_qualifications = (
    var.model_controller.fast_start_environment_qualifications_file == null ? {
      schema   = "fs2-serve.nebius.ai/runtime-environment-qualification-set/v1"
      bindings = []
      } : jsondecode(file(pathexpand(
        var.model_controller.fast_start_environment_qualifications_file
    )))
  )
  model_controller_fast_start_measurement_contracts = (
    var.model_controller.fast_start_measurement_contracts_file == null ? {
      schema = "fs2-serve.nebius.ai/fast-start-measurement-contract-set/v1"
      models = {}
      } : jsondecode(file(pathexpand(
        var.model_controller.fast_start_measurement_contracts_file
    )))
  )
  model_controller_fast_start_mechanisms = (
    var.model_controller.fast_start_mechanisms_file == null ? {
      schema = "fs2-serve.nebius.ai/fast-start-mechanism-set/v1"
      models = {}
      } : jsondecode(file(pathexpand(
        var.model_controller.fast_start_mechanisms_file
    )))
  )
  # One reviewed document declares every model's cold-start mechanisms, so
  # onboarding the two hundredth model is another entry here rather than new
  # Terraform. Each declaration carries its own configDigest and this gate
  # recomputes it, so a hand-edited declaration cannot slip into the envelope
  # and silently inherit an existing benchmark cohort.
  model_controller_fast_start_mechanism_declarations = {
    for model_id, declarations in try(local.model_controller_fast_start_mechanisms.models, {}) :
    model_id => {
      for mechanism, declaration in declarations : mechanism => declaration
    }
  }
  model_controller_fast_start_mechanism_names_valid = try(
    local.model_controller_fast_start_mechanisms.schema == "fs2-serve.nebius.ai/fast-start-mechanism-set/v1" &&
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        contains(["regionalCache", "hostMemoryResidency", "gpuResident"], mechanism)
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanism_digests_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        can(regex("^sha256:[a-f0-9]{64}$", declaration.configDigest)) &&
        declaration.configDigest == "sha256:${sha256(jsonencode({
          for key, value in declaration : key => value if key != "configDigest"
        }))}"
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanism_pools_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        length(declaration.poolRefs) > 0 &&
        alltrue([
          for pool_ref in declaration.poolRefs : contains(keys(local.selected_queue_pools), pool_ref)
        ])
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanisms_valid = (
    local.model_controller_fast_start_mechanism_names_valid &&
    local.model_controller_fast_start_mechanism_digests_valid &&
    local.model_controller_fast_start_mechanism_pools_valid
  )
  model_controller_fast_start_environment_qualifications_valid = try(
    local.model_controller_fast_start_environment_qualifications.schema == "fs2-serve.nebius.ai/runtime-environment-qualification-set/v1" &&
    length(keys(local.model_controller_fast_start_environment_qualifications)) == 2 &&
    length(local.model_controller_fast_start_environment_qualifications.bindings) <= 256 &&
    alltrue([
      for binding in local.model_controller_fast_start_environment_qualifications.bindings :
      length(keys(binding)) == 10 &&
      length(setsubtract(toset(keys(binding)), toset([
        "scope",
        "accelerator",
        "driverCuda",
        "storageRuntime",
        "hostRuntimeDigest",
        "environment",
        "members",
        "cacheTier",
        "startupScenario",
        "validUntil",
      ]))) == 0 &&
      binding.scope == {
        projectId      = nonsensitive(var.project_id)
        region         = local.selected_target.region
        clusterContext = var.kube_context
      } &&
      length(binding.members) >= 1 && length(binding.members) <= 32 &&
      length(distinct([for member in binding.members : "${member.poolRef}/${member.capacityType}"])) == length(binding.members) &&
      alltrue([
        for member in binding.members :
        contains(keys(local.selected_queue_pools), member.poolRef) &&
        contains(["regular", "preemptible"], member.capacityType) &&
        local.selected_queue_pools[member.poolRef].capacity.type == member.capacityType &&
        local.selected_queue_pools[member.poolRef].accelerator_class == binding.accelerator.acceleratorClass
      ]) &&
      contains(["Disabled", "ObjectStore", "SharedFilesystem", "NodeLocal"], binding.cacheTier) &&
      contains([
        "prepared-node-zero-pod",
        "fresh-node-zero-pod",
        "preemption-replacement",
        "durable-cache-loss-fallback",
      ], binding.startupScenario) &&
      can(timecmp(binding.validUntil, binding.validUntil)) && endswith(binding.validUntil, "Z") &&
      can(regex("^sha256:[a-f0-9]{64}$", binding.hostRuntimeDigest)) &&
      binding.environment == {
        schema = "fs2-serve.nebius.ai/runtime-environment-qualification/v1"
        qualificationDigest = "sha256:${sha256(jsonencode({
          schema               = "fs2-serve.nebius.ai/runtime-environment-qualification/v1"
          scopeDigest          = "sha256:${sha256(jsonencode(binding.scope))}"
          acceleratorDigest    = "sha256:${sha256(jsonencode(binding.accelerator))}"
          driverCudaDigest     = "sha256:${sha256(jsonencode(binding.driverCuda))}"
          hostRuntimeDigest    = binding.hostRuntimeDigest
          storageRuntimeDigest = "sha256:${sha256(jsonencode(binding.storageRuntime))}"
        }))}"
        scopeDigest          = "sha256:${sha256(jsonencode(binding.scope))}"
        acceleratorDigest    = "sha256:${sha256(jsonencode(binding.accelerator))}"
        driverCudaDigest     = "sha256:${sha256(jsonencode(binding.driverCuda))}"
        hostRuntimeDigest    = binding.hostRuntimeDigest
        storageRuntimeDigest = "sha256:${sha256(jsonencode(binding.storageRuntime))}"
      }
    ]),
    false,
  )
  model_controller_fast_start_measurement_contracts_valid = try(
    local.model_controller_fast_start_measurement_contracts.schema == "fs2-serve.nebius.ai/fast-start-measurement-contract-set/v1" &&
    length(keys(local.model_controller_fast_start_measurement_contracts)) == 2 &&
    length(local.model_controller_fast_start_measurement_contracts.models) <= 512 &&
    alltrue([
      for model_id, contract in local.model_controller_fast_start_measurement_contracts.models :
      contains(local.selected_model_ids, model_id) &&
      length(keys(contract)) == 10 &&
      contract.schema == "fs2-serve.nebius.ai/fast-start-measurement-contract/v1" &&
      contract.basis == "CapacityAvailableToSemanticReady" &&
      startswith(contract.endpointPath, "/") &&
      contains(["same-pod", "same-node", "in-cluster", "same-region", "cross-region", "external"], contract.clientPlacement) &&
      alltrue([
        for digest in [
          contract.payloadDigest,
          contract.semanticValidatorDigest,
          contract.benchmarkClientDigest,
          contract.contractDigest,
        ] : can(regex("^sha256:[a-f0-9]{64}$", digest))
      ]) &&
      contract.contractDigest == "sha256:${sha256(jsonencode({
        schema                  = contract.schema
        basis                   = contract.basis
        payloadDigest           = contract.payloadDigest
        protocol                = contract.protocol
        endpointPath            = contract.endpointPath
        streaming               = contract.streaming
        semanticValidatorDigest = contract.semanticValidatorDigest
        benchmarkClientDigest   = contract.benchmarkClientDigest
        clientPlacement         = contract.clientPlacement
      }))}"
    ]),
    false,
  )
  model_controller_fast_start_evidence_valid = try(alltrue([
    for model_id, evidence in local.model_controller_fast_start_evidence :
    contains(local.model_controller_dynamic_model_ids, model_id) &&
    length(evidence) <= 256 &&
    alltrue([
      for item in evidence :
      contains([15, 16, 17, 20], length(keys(item))) && length(setsubtract(toset(keys(item)), toset([
        "receiptDigest",
        "identityState",
        "identityDigest",
        "identity",
        "mechanism",
        "mechanismConfigDigest",
        "compatibilityTupleDigest",
        "compatibilityTupleComplete",
        "measurementBasis",
        "acceleratorClass",
        "poolRef",
        "capacityType",
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
      contains(["LegacyUnbound", "Bound"], try(item.identityState, "LegacyUnbound")) &&
      can(regex("^[a-z][a-z0-9-]{0,63}$", item.mechanism)) &&
      (
        try(item.identityState, "LegacyUnbound") == "Bound" ?
        length(keys(item)) == 20 &&
        item.poolRef != null &&
        contains(["regular", "preemptible"], try(item.capacityType, "")) &&
        can(regex("^sha256:[a-f0-9]{64}$", try(item.identityDigest, ""))) &&
        try(item.identity.schema, "") == "fs2-serve.nebius.ai/runtime-evidence-identity/v2" &&
        try(item.identityDigest, "") == "sha256:${sha256(jsonencode(try(item.identity, {})))}" &&
        try(item.identity.runtime.artifactManifestDigest, "") == item.artifactManifestDigest &&
        try(item.identity.runtime.runtimeImage, "") == item.runtimeImage &&
        try(item.identity.runtime.templateDigest, "") == item.templateDigest &&
        try(item.identity.placement.acceleratorClass, "") == item.acceleratorClass &&
        try(item.identity.placement.acceleratorsPerReplica, 0) == item.acceleratorsPerReplica &&
        try(item.identity.cache.tier, "") == item.cacheTier &&
        try(item.identity.cache.snapshotDigest, null) == item.snapshotDigest &&
        try(item.identity.cache.mechanism, "") == item.mechanism &&
        try(item.identity.cache.mechanismConfigDigest, "") == item.mechanismConfigDigest &&
        can(regex("^sha256:[a-f0-9]{64}$", item.mechanismConfigDigest)) :
        (
          contains(keys(item), "identityState") ?
          length(keys(item)) == 17 && item.identityState == "LegacyUnbound" :
          contains([15, 16], length(keys(item)))
        ) &&
        (
          contains(["modelexpress", "regional-cache", "host-memory-residency", "gpu-resident"], item.mechanism) ?
          can(regex("^sha256:[a-f0-9]{64}$", try(item.mechanismConfigDigest, ""))) :
          try(item.mechanismConfigDigest, null) == null
        )
      ) &&
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
  model_controller_modelexpress_pool_transports = {
    for model_id, config in var.model_express.models : model_id => {
      for pool_id in local.model_controller_qualified_pool_ids[model_id] : pool_id => lookup(
        config.pool_transports,
        pool_id,
        config.transport,
      )
    }
    if var.model_express.enabled && contains(local.model_controller_dynamic_model_ids, model_id)
  }
  model_controller_modelexpress_coordinator_network = (
    var.model_express.deployment_mode == "managed" ? {
      type      = "pod-selector"
      namespace = var.model_express.namespace
      podLabels = { "fs2-serve.nebius.ai/component" = "modelexpress-server" }
      cidrs     = []
      } : length(var.model_express.external_network.coordinator_cidrs) > 0 ? {
      type      = "ip-blocks"
      namespace = null
      podLabels = {}
      cidrs     = sort(tolist(var.model_express.external_network.coordinator_cidrs))
      } : {
      type      = "pod-selector"
      namespace = var.model_express.external_network.coordinator_namespace
      podLabels = var.model_express.external_network.coordinator_pod_labels
      cidrs     = []
    }
  )
  model_controller_modelexpress_payloads = {
    for model_id, config in var.model_express.models : model_id => {
      schema          = "fs2-serve.nebius.ai/modelexpress-client-binding/v1"
      upstreamVersion = "0.5.1"
      deploymentMode  = var.model_express.deployment_mode
      endpoint        = var.model_express.endpoint
      coordinatorImage = var.model_express.deployment_mode == "managed" ? format(
        "%s@%s",
        var.model_express.server_image.repository,
        var.model_express.server_image.digest,
      ) : null
      metadataBackend        = var.model_express.metadata_backend
      coordinatorNetworkType = local.model_controller_modelexpress_coordinator_network.type
      coordinatorNamespace   = local.model_controller_modelexpress_coordinator_network.namespace
      coordinatorPodLabels   = local.model_controller_modelexpress_coordinator_network.podLabels
      coordinatorCidrs       = local.model_controller_modelexpress_coordinator_network.cidrs
      runtimeAdapter         = config.runtime_adapter
      clientPackageVersion   = config.client_package_version
      artifactRevision       = local.catalog_models[model_id].model.source.revision
      artifactDigest         = local.model_controller_artifact_manifest_digests[model_id]
      runtimeImage           = var.model_image_overrides[model_id]
      templateDigest         = local.model_controller_template_digests[model_id]
      acceleratorCount       = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
      pools = [
        for pool_id in local.model_controller_qualified_pool_ids[model_id] : {
          poolRef          = pool_id
          acceleratorClass = local.selected_queue_pools[pool_id].accelerator_class
          resourceName     = local.selected_queue_pools[pool_id].resource_api.resource_name
          transport = {
            mode                 = local.model_controller_modelexpress_pool_transports[model_id][pool_id].mode
            rdmaResourceName     = local.model_controller_modelexpress_pool_transports[model_id][pool_id].rdma_resource_name
            rdmaResourceQuantity = local.model_controller_modelexpress_pool_transports[model_id][pool_id].rdma_resource_quantity
            nixlBackend          = local.model_controller_modelexpress_pool_transports[model_id][pool_id].nixl_backend
            rdmaNicPin           = local.model_controller_modelexpress_pool_transports[model_id][pool_id].nic_pin
          }
        }
      ]
      strategyOrder = ["p2p-nixl", "modelstreamer", "gds", "native"]
    }
    if var.model_express.enabled && contains(local.model_controller_dynamic_model_ids, model_id)
  }
  model_controller_modelexpress_bindings = {
    for model_id, payload in local.model_controller_modelexpress_payloads : model_id => {
      configDigest           = "sha256:${sha256(jsonencode(payload))}"
      endpoint               = payload.endpoint
      deploymentMode         = payload.deploymentMode
      metadataBackend        = payload.metadataBackend
      coordinatorNetworkType = payload.coordinatorNetworkType
      coordinatorNamespace   = payload.coordinatorNamespace
      coordinatorPodLabels   = payload.coordinatorPodLabels
      coordinatorCidrs       = payload.coordinatorCidrs
      runtimeAdapter         = payload.runtimeAdapter
      clientPackageVersion   = payload.clientPackageVersion
      poolRefs               = [for pool in payload.pools : pool.poolRef]
      poolTransports = {
        for pool in payload.pools : pool.poolRef => pool.transport
      }
    }
  }
  model_controller_fast_start_runtime_keys = flatten([
    for model_id in local.model_controller_dynamic_model_ids : [
      for pool_id in local.model_controller_qualified_pool_ids[model_id] : {
        key      = "${model_id}/${pool_id}"
        model_id = model_id
        pool_id  = pool_id
      }
    ]
  ])
  model_controller_fast_start_runtime_containers = {
    for model_id, deployment in local.model_controller_primary_deployments : model_id => one([
      for container in deployment.spec.template.spec.containers : container
      if container.name == local.model_controller_runtime_container_names[model_id]
    ])
  }
  model_controller_fast_start_modelexpress_env = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => concat(
      [
        { name = "MODEL_EXPRESS_URL", value = local.model_controller_modelexpress_bindings[row.model_id].endpoint },
        { name = "MX_SERVER_ADDRESS", value = local.model_controller_modelexpress_bindings[row.model_id].endpoint },
        { name = "MX_METADATA_BACKEND", value = local.model_controller_modelexpress_bindings[row.model_id].metadataBackend },
        { name = "VLLM_PLUGINS", value = "modelexpress" },
        { name = "MX_NIXL_BACKEND", value = local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].nixlBackend },
        { name = "MX_METADATA_PORT", value = "5555" },
        { name = "MX_WORKER_GRPC_PORT", value = "6555" },
        { name = "MX_P2P_METADATA", value = "1" },
        {
          name = "MX_MODEL_REVISION"
          value = "fs2:sha256:${sha256(jsonencode({
            configDigest     = local.model_controller_modelexpress_bindings[row.model_id].configDigest
            acceleratorClass = local.selected_queue_pools[row.pool_id].accelerator_class
            nixlBackend      = local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].nixlBackend
          }))}"
        },
      ],
      local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].mode == "nixl-rdma" ? [
        {
          name  = "MX_RDMA_NIC_PIN"
          value = local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].rdmaNicPin
        }
      ] : [],
      local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].mode == "nixl-rdma" &&
      local.model_controller_modelexpress_bindings[row.model_id].poolTransports[row.pool_id].nixlBackend == "UCX" ? [
        { name = "UCX_RNDV_SCHEME", value = "get_zcopy" },
        { name = "UCX_RNDV_THRESH", value = "0" },
      ] : [],
      [
        { name = "POD_IP", valueFrom = { fieldRef = { fieldPath = "status.podIP" } } },
        { name = "NODE_NAME", valueFrom = { fieldRef = { fieldPath = "spec.nodeName" } } },
        { name = "POD_NAMESPACE", valueFrom = { fieldRef = { fieldPath = "metadata.namespace" } } },
        { name = "POD_NAME", valueFrom = { fieldRef = { fieldPath = "metadata.name" } } },
        { name = "POD_UID", valueFrom = { fieldRef = { fieldPath = "metadata.uid" } } },
      ],
    )
    if contains(keys(local.model_controller_modelexpress_bindings), row.model_id)
  }
  model_controller_fast_start_modelexpress_managed_env_names = {
    for key, environment in local.model_controller_fast_start_modelexpress_env :
    key => toset([for item in environment : item.name])
  }
  model_controller_fast_start_runtime_args = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => (
      contains(keys(local.model_controller_modelexpress_bindings), row.model_id) ? concat([
        for index, argument in try(local.model_controller_fast_start_runtime_containers[row.model_id].args, []) : argument
        if argument != "--load-format" &&
        !startswith(argument, "--load-format=") &&
        !(index > 0 && try(local.model_controller_fast_start_runtime_containers[row.model_id].args[index - 1], null) == "--load-format")
      ], ["--load-format", "modelexpress"]) : try(local.model_controller_fast_start_runtime_containers[row.model_id].args, [])
    )
  }
  model_controller_fast_start_runtime_base_env = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => try(
      local.model_controller_fast_start_runtime_containers[row.model_id].env,
      [],
    )
  }
  model_controller_fast_start_runtime_modelexpress_env = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => concat([
      for item in try(local.model_controller_fast_start_runtime_containers[row.model_id].env, []) : item
      if !contains(local.model_controller_fast_start_modelexpress_managed_env_names[row.key], item.name)
      ], local.model_controller_fast_start_modelexpress_env[row.key]
    )
    if contains(keys(local.model_controller_modelexpress_bindings), row.model_id)
  }
  model_controller_fast_start_runtime_env = merge(
    local.model_controller_fast_start_runtime_base_env,
    local.model_controller_fast_start_runtime_modelexpress_env,
  )
  model_controller_fast_start_container_env = merge(
    merge([
      for row in local.model_controller_fast_start_runtime_keys : {
        for container in local.model_controller_primary_deployments[row.model_id].spec.template.spec.containers :
        "${row.key}/${container.name}" => try(container.env, [])
      }
    ]...),
    {
      for row in local.model_controller_fast_start_runtime_keys :
      "${row.key}/${local.model_controller_runtime_container_names[row.model_id]}" => local.model_controller_fast_start_runtime_env[row.key]
    },
  )
  model_controller_fast_start_environment_rows = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => flatten([
      for container in local.model_controller_primary_deployments[row.model_id].spec.template.spec.containers : [
        for item in local.model_controller_fast_start_container_env["${row.key}/${container.name}"] : {
          container    = container.name
          name         = item.name
          value_sha256 = contains(keys(item), "value") ? sha256(tostring(item.value)) : null
          value_from   = try(item.valueFrom, null)
        }
      ]
    ])
  }
  model_controller_fast_start_storage_contracts = {
    for model_id in local.model_controller_dynamic_model_ids : model_id => {
      schema = "fs2-serve.nebius.ai/fast-start-storage-contract/v1"
      storageClass = local.model_controller_bundle_requires_shared_cache[model_id] ? one(distinct([
        for document in local.model_documents : try(document.manifest.spec.storageClassName, null)
        if document.model_id == model_id && document.manifest.kind == "PersistentVolumeClaim"
      ])) : null
      storageMode = local.model_controller_bundle_requires_shared_cache[model_id] ? (
        contains(flatten([
          for document in local.model_documents : try(document.manifest.spec.accessModes, [])
          if document.model_id == model_id && document.manifest.kind == "PersistentVolumeClaim"
        ]), "ReadWriteMany") ? "rwx-filesystem" : "rwo-filesystem"
      ) : "ephemeral"
    }
  }
  model_controller_fast_start_runtime_payloads = {
    for row in local.model_controller_fast_start_runtime_keys : row.key => {
      schema                 = "fs2-serve.nebius.ai/runtime-contract/v1"
      modelRef               = row.model_id
      sourceRevision         = local.catalog_models[row.model_id].model.source.revision
      modelContentDigest     = local.model_controller_artifact_manifest_digests[row.model_id]
      artifactManifestDigest = local.model_controller_artifact_manifest_digests[row.model_id]
      runtimeProfile         = local.catalog_models[row.model_id].runtime.kind
      runtimeImage           = var.model_image_overrides[row.model_id]
      templateDigest         = local.model_controller_template_digests[row.model_id]
      renderContractDigest = "sha256:${sha256(jsonencode({
        schema         = "fs2-serve.nebius.ai/runtime-render-contract/v1"
        runtimeImage   = var.model_image_overrides[row.model_id]
        templateDigest = local.model_controller_template_digests[row.model_id]
        argvDigest = "sha256:${sha256(jsonencode({
          command = try(local.model_controller_fast_start_runtime_containers[row.model_id].command, [])
          args    = local.model_controller_fast_start_runtime_args[row.key]
        }))}"
        environmentDigest = "sha256:${sha256(jsonencode(local.model_controller_fast_start_environment_rows[row.key]))}"
      }))}"
      argvDigest = "sha256:${sha256(jsonencode({
        command = try(local.model_controller_fast_start_runtime_containers[row.model_id].command, [])
        args    = local.model_controller_fast_start_runtime_args[row.key]
      }))}"
      environmentDigest = "sha256:${sha256(jsonencode(local.model_controller_fast_start_environment_rows[row.key]))}"
    }
  }
  model_controller_fast_start_runtime_contracts = {
    for model_id in local.model_controller_dynamic_model_ids : model_id => [
      for row in local.model_controller_fast_start_runtime_keys : {
        poolRef = row.pool_id
        runtime = merge(local.model_controller_fast_start_runtime_payloads[row.key], {
          runtimeContractDigest = "sha256:${sha256(jsonencode(local.model_controller_fast_start_runtime_payloads[row.key]))}"
        })
        storageContractDigest = "sha256:${sha256(jsonencode(local.model_controller_fast_start_storage_contracts[model_id]))}"
        measurement           = local.model_controller_fast_start_measurement_contracts.models[model_id]
      }
      if row.model_id == model_id && contains(keys(local.model_controller_fast_start_measurement_contracts.models), model_id)
    ]
  }
  model_controller_fast_start_pool_bindings = {
    for pool_id in keys(local.selected_queue_pools) : pool_id => [
      for binding in local.model_controller_fast_start_environment_qualifications.bindings : {
        environment     = binding.environment
        members         = binding.members
        cacheTier       = binding.cacheTier
        startupScenario = binding.startupScenario
        validUntil      = binding.validUntil
      }
      if contains(binding.members, {
        poolRef      = pool_id
        capacityType = local.selected_queue_pools[pool_id].capacity.type
      })
    ]
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
    for model_id in local.model_controller_dynamic_model_ids : model_id => merge({
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
      snapshotDigests           = []
      fastStartRuntimeContracts = local.model_controller_fast_start_runtime_contracts[model_id]
      # Fast-start levels (L1..L4) are qualified only by retained benchmark
      # evidence measured from GPU capacity being available until semantic
      # endpoint readiness for the exact artifact, image, template, cache tier
      # and accelerator tuple. The retained elasticity receipts measure
      # activation-to-ready, which includes capacity wait, so they are not
      # compatible evidence. Until a fast-start benchmark receipt is retained
      # and projected here, every level above Off stays unqualified and the
      # controller reports that truthfully.
      fastStartEvidence = try(local.model_controller_fast_start_evidence[model_id], [])
      },
      contains(keys(local.model_controller_modelexpress_bindings), model_id) ? {
        modelExpress = local.model_controller_modelexpress_bindings[model_id]
      } : {},
      try(local.model_controller_fast_start_mechanism_declarations[model_id], {}),
    )
  }
  model_controller_pool_envelope = {
    for pool_id, pool in local.selected_queue_pools : pool_id => {
      poolId                       = pool_id
      acceleratorClass             = pool.accelerator_class
      resourceName                 = pool.resource_api.resource_name
      capacityType                 = pool.capacity.type
      acceleratorsPerNode          = pool.node.gpus_per_node
      minNodes                     = pool.capacity.min_nodes
      maxNodes                     = pool.capacity.max_nodes
      nodeSelector                 = pool.scheduling.stable_node_labels
      tolerations                  = pool.scheduling.tolerations
      startupScenario              = pool.capacity.min_nodes > 0 ? "prepared-node-zero-pod" : "fresh-node-zero-pod"
      fastStartEnvironmentBindings = local.model_controller_fast_start_pool_bindings[pool_id]
    }
  }
  model_controller_envelope_without_revision = {
    pools                         = local.model_controller_pool_envelope
    qualifications                = local.model_controller_qualifications
    localQueues                   = sort(keys(module.kueue_scheduling.contract.local_queues))
    priorityClasses               = sort(keys(module.kueue_scheduling.contract.workload_priority_classes))
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
    modelexpress_resources   = local.modelexpress_resource_counts
  }

  lifecycle {
    precondition {
      condition     = local.model_controller_fast_start_evidence_valid
      error_message = "Fast-start evidence must map only controller-qualified model IDs to the exact bounded wire shape emitted by project_fast_start_evidence.py."
    }

    precondition {
      condition     = local.model_controller_fast_start_mechanisms_valid
      error_message = "Each fast-start mechanism declaration must carry a matching configDigest and only reference selected accelerator pools."
    }
    precondition {
      condition     = local.model_controller_fast_start_environment_qualifications_valid
      error_message = "Fast-start environment qualifications must be an exact, self-digested v1 document for this project, region, cluster context, accelerator class, pool, and capacity type."
    }

    precondition {
      condition     = local.model_controller_fast_start_measurement_contracts_valid
      error_message = "Fast-start measurement contracts must be exact, self-digested v1 contracts for selected models."
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

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
  fast_start_mechanism_set_keys = toset(["schema", "models"])
  fast_start_regional_cache_required_keys = toset([
    "schema",
    "configDigest",
    "imageMirrorRegistry",
    "payloadClaimName",
    "payloadContentPath",
    "payloadBytes",
    "compileCache",
    "poolRefs",
  ])
  fast_start_regional_cache_allowed_keys = setunion(
    local.fast_start_regional_cache_required_keys,
    toset(["warmPageCache"]),
  )
  fast_start_compile_cache_keys = toset([
    "claimName",
    "subPath",
    "abi",
    "mountPath",
    "sizeLimitBytes",
  ])
  fast_start_warm_page_cache_keys = toset([
    "workers",
    "readBytesLimit",
    "timeoutSeconds",
  ])
  fast_start_host_memory_keys = toset([
    "schema",
    "configDigest",
    "residencyMode",
    "payloadClaimName",
    "payloadContentPath",
    "payloadDigest",
    "payloadBytes",
    "reservedBytes",
    "nodeAllocatableBytes",
    "holder",
    "receiptMaxAgeSeconds",
    "poolRefs",
  ])
  fast_start_residency_holder_keys = toset([
    "name",
    "namespace",
    "receiptClaimName",
    "receiptMountPath",
  ])
  fast_start_gpu_resident_keys = toset([
    "schema",
    "configDigest",
    "residencyMode",
    "standbyReplicas",
    "acceleratorsPerStandbyReplica",
    "minimumHotReplicas",
    "promotionProbePeriodSeconds",
    "poolRefs",
  ])
  fast_start_sha256_digest_pattern = "^sha256:[a-f0-9]{64}$"
  fast_start_dns_subdomain_pattern = (
    "^[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,251}[a-z0-9])?)*$"
  )
  fast_start_content_path_pattern             = "^/(?:[A-Za-z0-9._-]+/?)+$"
  fast_start_json_nonnegative_integer_pattern = "^(0|[1-9][0-9]*)$"

  model_controller_fast_start_mechanism_declarations = {
    for model_id, declarations in try(local.model_controller_fast_start_mechanisms.models, {}) :
    model_id => {
      for mechanism, declaration in declarations : mechanism => declaration
    }
  }
  model_controller_fast_start_mechanism_names_valid = try(
    local.model_controller_fast_start_mechanisms.schema == "fs2-serve.nebius.ai/fast-start-mechanism-set/v1" &&
    toset(keys(local.model_controller_fast_start_mechanisms)) == local.fast_start_mechanism_set_keys &&
    length(local.model_controller_fast_start_mechanism_declarations) <= 512 &&
    alltrue([
      for model_id in keys(local.model_controller_fast_start_mechanism_declarations) :
      contains(local.selected_model_ids, model_id) &&
      contains(local.model_controller_dynamic_model_ids, model_id)
    ]) &&
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        contains(["regionalCache", "hostMemoryResidency", "gpuResident"], mechanism)
      ]
    ])),
    false,
  )
  # Terraform publishes these objects verbatim into the controller envelope.
  # Validate the same closed wire shapes as the strict Pydantic runtime models,
  # including nested objects, so a successfully planned envelope cannot crash
  # ControllerFiles.load during API or controller startup.
  model_controller_fast_start_mechanism_shapes_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        mechanism == "regionalCache" ? (
          length(setsubtract(
            local.fast_start_regional_cache_required_keys,
            toset(keys(declaration)),
          )) == 0 &&
          length(setsubtract(
            toset(keys(declaration)),
            local.fast_start_regional_cache_allowed_keys,
          )) == 0 &&
          declaration.schema == "fs2-serve.nebius.ai/fast-start-regional-cache/v1" &&
          length(declaration.imageMirrorRegistry) >= 3 &&
          length(declaration.imageMirrorRegistry) <= 253 &&
          can(regex("^[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?$", declaration.imageMirrorRegistry)) &&
          length(declaration.payloadClaimName) >= 1 &&
          length(declaration.payloadClaimName) <= 253 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.payloadClaimName)) &&
          length(declaration.payloadContentPath) >= 2 &&
          length(declaration.payloadContentPath) <= 512 &&
          can(regex(local.fast_start_content_path_pattern, declaration.payloadContentPath)) &&
          can(regex(local.fast_start_json_nonnegative_integer_pattern, jsonencode(declaration.payloadBytes))) &&
          declaration.payloadBytes >= 1 && declaration.payloadBytes <= 70368744177664 &&
          toset(keys(declaration.compileCache)) == local.fast_start_compile_cache_keys &&
          length(declaration.compileCache.claimName) >= 1 &&
          length(declaration.compileCache.claimName) <= 253 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.compileCache.claimName)) &&
          length(declaration.compileCache.subPath) >= 1 &&
          length(declaration.compileCache.subPath) <= 200 &&
          can(regex("^[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9])?$", declaration.compileCache.subPath)) &&
          !startswith(declaration.compileCache.subPath, "/") &&
          !strcontains(declaration.compileCache.subPath, "..") &&
          length(declaration.compileCache.abi) >= 3 &&
          length(declaration.compileCache.abi) <= 128 &&
          can(regex("^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$", declaration.compileCache.abi)) &&
          strcontains(declaration.compileCache.subPath, declaration.compileCache.abi) &&
          length(declaration.compileCache.mountPath) >= 2 &&
          length(declaration.compileCache.mountPath) <= 200 &&
          can(regex(local.fast_start_content_path_pattern, declaration.compileCache.mountPath)) &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.compileCache.sizeLimitBytes),
          )) &&
          declaration.compileCache.sizeLimitBytes >= 1048576 &&
          declaration.compileCache.sizeLimitBytes <= 4398046511104 &&
          (
            try(declaration.warmPageCache, null) == null ? true : (
              toset(keys(declaration.warmPageCache)) == local.fast_start_warm_page_cache_keys &&
              can(regex(
                local.fast_start_json_nonnegative_integer_pattern,
                jsonencode(declaration.warmPageCache.workers),
              )) &&
              declaration.warmPageCache.workers >= 1 && declaration.warmPageCache.workers <= 64 &&
              can(regex(
                local.fast_start_json_nonnegative_integer_pattern,
                jsonencode(declaration.warmPageCache.readBytesLimit),
              )) &&
              declaration.warmPageCache.readBytesLimit >= 1048576 &&
              declaration.warmPageCache.readBytesLimit <= 4398046511104 &&
              declaration.warmPageCache.readBytesLimit <= declaration.payloadBytes &&
              can(regex(
                local.fast_start_json_nonnegative_integer_pattern,
                jsonencode(declaration.warmPageCache.timeoutSeconds),
              )) &&
              declaration.warmPageCache.timeoutSeconds >= 1 &&
              declaration.warmPageCache.timeoutSeconds <= 1800
            )
          )
          ) : mechanism == "hostMemoryResidency" ? (
          toset(keys(declaration)) == local.fast_start_host_memory_keys &&
          declaration.schema == "fs2-serve.nebius.ai/fast-start-host-memory-residency/v1" &&
          contains([
            "locked-payload-residency",
            "mapped-payload-residency",
            "runtime-sleep-offload",
          ], declaration.residencyMode) &&
          length(declaration.payloadClaimName) >= 1 &&
          length(declaration.payloadClaimName) <= 253 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.payloadClaimName)) &&
          length(declaration.payloadContentPath) >= 2 &&
          length(declaration.payloadContentPath) <= 512 &&
          can(regex(local.fast_start_content_path_pattern, declaration.payloadContentPath)) &&
          can(regex(local.fast_start_sha256_digest_pattern, declaration.payloadDigest)) &&
          can(regex(local.fast_start_json_nonnegative_integer_pattern, jsonencode(declaration.payloadBytes))) &&
          declaration.payloadBytes >= 1 && declaration.payloadBytes <= 70368744177664 &&
          can(regex(local.fast_start_json_nonnegative_integer_pattern, jsonencode(declaration.reservedBytes))) &&
          declaration.reservedBytes >= 1 && declaration.reservedBytes <= 70368744177664 &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.nodeAllocatableBytes),
          )) &&
          declaration.nodeAllocatableBytes >= 1 && declaration.nodeAllocatableBytes <= 281474976710656 &&
          declaration.reservedBytes <= declaration.nodeAllocatableBytes &&
          (
            declaration.residencyMode == "runtime-sleep-offload" ?
            declaration.reservedBytes >= declaration.payloadBytes :
            declaration.reservedBytes >= declaration.payloadBytes + 268435456
          ) &&
          toset(keys(declaration.holder)) == local.fast_start_residency_holder_keys &&
          length(declaration.holder.name) >= 1 && length(declaration.holder.name) <= 253 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.holder.name)) &&
          length(declaration.holder.namespace) >= 1 && length(declaration.holder.namespace) <= 63 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.holder.namespace)) &&
          length(declaration.holder.receiptClaimName) >= 1 &&
          length(declaration.holder.receiptClaimName) <= 253 &&
          can(regex(local.fast_start_dns_subdomain_pattern, declaration.holder.receiptClaimName)) &&
          length(declaration.holder.receiptMountPath) >= 2 &&
          length(declaration.holder.receiptMountPath) <= 200 &&
          can(regex(local.fast_start_content_path_pattern, declaration.holder.receiptMountPath)) &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.receiptMaxAgeSeconds),
          )) &&
          declaration.receiptMaxAgeSeconds >= 30 && declaration.receiptMaxAgeSeconds <= 86400
          ) : mechanism == "gpuResident" ? (
          toset(keys(declaration)) == local.fast_start_gpu_resident_keys &&
          declaration.schema == "fs2-serve.nebius.ai/fast-start-gpu-resident/v1" &&
          contains(["standby-engine", "warm-engine-hot-floor"], declaration.residencyMode) &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.standbyReplicas),
          )) &&
          declaration.standbyReplicas >= 1 && declaration.standbyReplicas <= 64 &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.acceleratorsPerStandbyReplica),
          )) &&
          declaration.acceleratorsPerStandbyReplica >= 1 &&
          declaration.acceleratorsPerStandbyReplica <= 64 &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.minimumHotReplicas),
          )) &&
          declaration.minimumHotReplicas >= 0 && declaration.minimumHotReplicas <= 10000 &&
          (
            declaration.residencyMode != "warm-engine-hot-floor" || declaration.minimumHotReplicas >= 1
          ) &&
          can(regex(
            local.fast_start_json_nonnegative_integer_pattern,
            jsonencode(declaration.promotionProbePeriodSeconds),
          )) &&
          declaration.promotionProbePeriodSeconds >= 1 &&
          declaration.promotionProbePeriodSeconds <= 60
        ) : false
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanism_digests_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        can(regex(local.fast_start_sha256_digest_pattern, declaration.configDigest)) &&
        declaration.configDigest == (
          mechanism == "regionalCache" ? "sha256:${sha256(jsonencode({
            schema              = declaration.schema
            imageMirrorRegistry = declaration.imageMirrorRegistry
            payloadClaimName    = declaration.payloadClaimName
            payloadContentPath  = declaration.payloadContentPath
            payloadBytes        = declaration.payloadBytes
            compileCache        = declaration.compileCache
            warmPageCache       = try(declaration.warmPageCache, null)
            poolRefs            = declaration.poolRefs
            }))}" : mechanism == "hostMemoryResidency" ? "sha256:${sha256(jsonencode({
            schema               = declaration.schema
            residencyMode        = declaration.residencyMode
            payloadClaimName     = declaration.payloadClaimName
            payloadContentPath   = declaration.payloadContentPath
            payloadDigest        = declaration.payloadDigest
            payloadBytes         = declaration.payloadBytes
            reservedBytes        = declaration.reservedBytes
            nodeAllocatableBytes = declaration.nodeAllocatableBytes
            holder               = declaration.holder
            receiptMaxAgeSeconds = declaration.receiptMaxAgeSeconds
            poolRefs             = declaration.poolRefs
            }))}" : mechanism == "gpuResident" ? "sha256:${sha256(jsonencode({
            schema                        = declaration.schema
            residencyMode                 = declaration.residencyMode
            standbyReplicas               = declaration.standbyReplicas
            acceleratorsPerStandbyReplica = declaration.acceleratorsPerStandbyReplica
            minimumHotReplicas            = declaration.minimumHotReplicas
            promotionProbePeriodSeconds   = declaration.promotionProbePeriodSeconds
            poolRefs                      = declaration.poolRefs
          }))}" : ""
        )
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanism_pools_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        can(tolist(declaration.poolRefs)) &&
        length(declaration.poolRefs) > 0 && length(declaration.poolRefs) <= 32 &&
        length(distinct(declaration.poolRefs)) == length(declaration.poolRefs) &&
        alltrue([
          for pool_ref in declaration.poolRefs : contains(keys(local.selected_queue_pools), pool_ref)
        ])
      ]
    ])),
    false,
  )
  model_controller_fast_start_host_memory_valid = try(
    alltrue(flatten([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations : [
        for mechanism, declaration in declarations :
        mechanism != "hostMemoryResidency" || alltrue([
          for pool_ref in declaration.poolRefs :
          contains(keys(var.accelerator_node_schedulable_capacity), pool_ref) &&
          declaration.reservedBytes <= var.accelerator_node_schedulable_capacity[pool_ref].memory_mib * 1048576
        ])
      ]
    ])),
    false,
  )
  model_controller_fast_start_mechanism_cross_fields_valid = try(
    alltrue([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations :
      (
        contains(keys(declarations), "regionalCache") &&
        contains(keys(declarations), "hostMemoryResidency") &&
        try(declarations.hostMemoryResidency.residencyMode, "") != "runtime-sleep-offload"
        ) ? (
        declarations.regionalCache.payloadContentPath == declarations.hostMemoryResidency.payloadContentPath
      ) : true
    ]) &&
    alltrue([
      for model_id, declarations in local.model_controller_fast_start_mechanism_declarations :
      contains(keys(declarations), "gpuResident") ? (
        declarations.gpuResident.acceleratorsPerStandbyReplica <=
        local.profile_contract.model_autoscaling_targets[model_id].gpu_count
      ) : true
    ]),
    false,
  )
  model_controller_fast_start_mechanisms_valid = (
    local.model_controller_fast_start_mechanism_names_valid &&
    local.model_controller_fast_start_mechanism_shapes_valid &&
    local.model_controller_fast_start_mechanism_digests_valid &&
    local.model_controller_fast_start_mechanism_pools_valid &&
    local.model_controller_fast_start_host_memory_valid &&
    local.model_controller_fast_start_mechanism_cross_fields_valid
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
    for model_id in setunion(toset(local.accelerator_model_ids), local.managed_cpu_model_ids) : model_id => one([
      for document in local.model_documents : document.manifest
      if document.model_id == model_id &&
      document.manifest.kind == "Deployment" &&
      document.manifest.metadata.name == local.profile_contract.model_autoscaling_targets[model_id].deployment
    ])
  }
  model_controller_runtime_container_names = {
    for model_id, deployment in local.model_controller_primary_deployments : model_id => one([
      for container in deployment.spec.template.spec.containers : container.name
      if contains(local.managed_cpu_model_ids, model_id) ? (
        container.image == var.model_image_overrides[model_id]
        ) : anytrue([
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
    for model_id in local.selected_model_ids : model_id => contains(local.managed_cpu_model_ids, model_id) ? sort(keys(local.model_controller_cpu_pool_envelope)) : sort([
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
  # A portable runtime may consume a different GPU count than the archival
  # NIM. Prefer its exact variant observation without rewriting the old lane.
  model_controller_hardware_runtimes = {
    for model_id in local.selected_model_ids : model_id => try(
      local.model_controller_accelerator_compatibility.models[model_id].runtimes[local.deployment_runtime_records[model_id].variant_id],
      local.model_controller_accelerator_compatibility.models[model_id].runtimes["catalog-canonical"],
      {},
    )
  }
  model_controller_required_runtime_states = toset([
    "registered",
    "runtime_ready",
    "semantic_qualified",
  ])
  # Public HTTP/MCP acceptance is measured after this deployment creates the
  # route. Requiring it here prevents onboarding a newly validated runtime
  # unless somebody incorrectly reuses an older image's public receipt. Keep
  # that separate flag visible in the qualification API; it is not a deployment
  # prerequisite. Artifact, semantic and exact hardware checks still apply.
  model_controller_hardware_qualified_accelerator_classes = {
    for model_id in local.selected_model_ids : model_id => sort(distinct([
      for binding in try(
        local.model_controller_hardware_runtimes[model_id].bindings,
        [],
      ) : binding.accelerator_class
      if try(
        binding.enabled && binding.state == "hardware-validated" &&
        coalesce(try(binding.gpu_count, null), local.model_controller_hardware_runtimes[model_id].requirements.gpu_count) == local.profile_contract.model_autoscaling_targets[model_id].gpu_count &&
        (local.model_controller_hardware_runtimes[model_id].runtime_ref_kind != "container-image-digest" ||
        endswith(local.model_controller_hardware_runtimes[model_id].runtime_ref, local.catalog_models[model_id].runtime.image.digest)),
        false,
      )
    ]))
  }
  model_controller_qualified_pool_ids = {
    for model_id in local.selected_model_ids : model_id => contains(local.managed_cpu_model_ids, model_id) ? sort([
      for pool_id in local.model_controller_pool_ids[model_id] : pool_id
      if local.model_controller_cpu_pool_envelope[pool_id].allocatableCpuMillis >= local.catalog_models[model_id].resources.cpu_millis &&
      local.model_controller_cpu_pool_envelope[pool_id].allocatableMemoryBytes >= local.catalog_models[model_id].resources.memory_bytes
      ]) : sort([
      for pool_id in local.model_controller_pool_ids[model_id] : pool_id
      if contains(
        local.model_controller_hardware_qualified_accelerator_classes[model_id],
        local.selected_queue_pools[pool_id].accelerator_class,
        ) && local.selected_queue_pools[pool_id].node.gpus_per_node >= local.profile_contract.model_autoscaling_targets[model_id].gpu_count && length(setintersection(
          toset(local.selected_queue_pools[pool_id].node.host_architectures),
          toset(try(
            local.model_controller_hardware_runtimes[model_id].requirements.host_architectures,
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
        # An explicitly named upstream variant can become the canonical runtime.
        # Compare its actual source/image/service identities, not a null label;
        # keep the variant and runtime_origin visible in the qualification API.
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
  model_controller_cpu_configuration = {
    for model_id in local.managed_cpu_model_ids : model_id => {
      cpuResources = {
        cpuMillis   = local.catalog_models[model_id].resources.cpu_millis
        memoryBytes = local.catalog_models[model_id].resources.memory_bytes
      }
      localQueue = var.general_cpu_lane.local_queue
    }
  }
  model_controller_cache_tiers = {
    for model_id in local.model_controller_dynamic_model_ids : model_id => (
      contains(local.managed_native_model_ids, model_id) &&
      local.catalog_models[model_id].cache.owner == "runtime-image" ? "Disabled" :
      local.model_controller_bundle_requires_shared_cache[model_id] ? "SharedFilesystem" : "NodeLocal"
    )
  }
  model_controller_qualifications = {
    for model_id in local.model_controller_dynamic_model_ids : model_id => merge({
      modelRef                = model_id
      runtimeProfile          = local.catalog_models[model_id].runtime.kind
      artifactManifestDigests = [local.model_controller_artifact_manifest_digests[model_id]]
      artifactRevisions = {
        (local.catalog_models[model_id].model.source.revision) = local.model_controller_artifact_manifest_digests[model_id]
      }
      runtimeImages             = [var.model_image_overrides[model_id]]
      acceleratorClasses        = sort(distinct([for pool_id in local.model_controller_qualified_pool_ids[model_id] : local.model_controller_pool_envelope[pool_id].acceleratorClass]))
      maxAcceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
      scaleToZeroQualified      = try(local.model_controller_qualification_rows[model_id].states.elasticity_qualified, false)
      templateDigests           = [local.model_controller_template_digests[model_id]]
      templateRefs = {
        "${model_id}.legacy-v1" = local.model_controller_template_digests[model_id]
      }
      templateCacheTiers = {
        (local.model_controller_template_digests[model_id]) = local.model_controller_cache_tiers[model_id]
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
      gpuSnapshotBundles = {
        for id, bundle in local.serving_snapshot_bundles : id => bundle
        if bundle.model_ref == model_id
      }
      fastStartRuntimeContracts = contains(local.managed_cpu_model_ids, model_id) ? [] : local.model_controller_fast_start_runtime_contracts[model_id]
      # Fast-start levels (L1..L4) are qualified only by retained benchmark
      # evidence measured from GPU capacity being available until semantic
      # endpoint readiness for the exact artifact, image, template, cache tier
      # and accelerator tuple. The retained elasticity receipts measure
      # activation-to-ready, which includes capacity wait, so they are not
      # compatible evidence. Until a fast-start benchmark receipt is retained
      # and projected here, every level above Off stays unqualified and the
      # controller reports that truthfully.
      fastStartEvidence = contains(local.managed_cpu_model_ids, model_id) ? [] : try(local.model_controller_fast_start_evidence[model_id], [])
      },
      contains(keys(local.model_controller_modelexpress_bindings), model_id) ? {
        modelExpress = local.model_controller_modelexpress_bindings[model_id]
      } : {},
      try(local.model_controller_fast_start_mechanism_declarations[model_id], {}),
      try(local.model_controller_cpu_configuration[model_id], {}),
    )
  }
  model_controller_accelerator_pool_envelope = {
    for pool_id, pool in local.selected_queue_pools : pool_id => {
      poolId                       = pool_id
      acceleratorClass             = pool.accelerator_class
      resourceName                 = pool.resource_api.resource_name
      capacityType                 = pool.capacity.type
      acceleratorsPerNode          = pool.node.gpus_per_node
      allocatableMemoryBytes       = try(var.accelerator_node_schedulable_capacity[pool_id].memory_mib * 1048576, null)
      minNodes                     = pool.capacity.min_nodes
      maxNodes                     = pool.capacity.max_nodes
      nodeSelector                 = pool.scheduling.stable_node_labels
      tolerations                  = pool.scheduling.tolerations
      startupScenario              = pool.capacity.min_nodes > 0 ? "prepared-node-zero-pod" : "fresh-node-zero-pod"
      fastStartEnvironmentBindings = local.model_controller_fast_start_pool_bindings[pool_id]
    }
  }
  # CPU Apps use the existing general-compute lane. Its declared schedulable
  # CPU/RAM envelope, not an invented GPU token, bounds replica capacity. Keep
  # these optional fields absent from unchanged accelerator pool identities.
  model_controller_cpu_pool_envelope = {
    for pool_id, pool in try(var.general_cpu_pools.pools, {}) : pool_id => {
      poolId                 = pool_id
      acceleratorClass       = "CPU"
      resourceName           = "cpu"
      capacityType           = pool.capacity_type
      acceleratorsPerNode    = 0
      allocatableCpuMillis   = pool.schedulable_capacity.cpu_millicores
      allocatableMemoryBytes = pool.schedulable_capacity.memory_mib * 1048576
      minNodes               = pool.min_nodes
      maxNodes               = pool.max_nodes
      nodeSelector = merge(var.general_cpu_pools.node_selector, {
        "capacity.fs2.nebius/pool-id" = pool_id
        "capacity.fs2.nebius/type"    = pool.capacity_type
      })
      tolerations                  = [merge(var.general_cpu_pools.taint, { operator = "Equal" })]
      startupScenario              = pool.min_nodes > 0 ? "prepared-node-zero-pod" : "fresh-node-zero-pod"
      fastStartEnvironmentBindings = []
    }
    if length(local.managed_cpu_model_ids) > 0 && local.general_cpu_enabled &&
    var.general_cpu_lane.namespace == local.inventory.namespace
  }
  model_controller_pool_envelope = merge(
    local.model_controller_accelerator_pool_envelope,
    local.model_controller_cpu_pool_envelope,
  )
  model_controller_envelope_without_revision = {
    pools          = local.model_controller_pool_envelope
    qualifications = local.model_controller_qualifications
    localQueues = sort(distinct(concat(
      keys(module.kueue_scheduling.contract.local_queues),
      length(local.model_controller_cpu_pool_envelope) > 0 ? [var.general_cpu_lane.local_queue] : [],
    )))
    priorityClasses               = sort(keys(module.kueue_scheduling.contract.workload_priority_classes))
    tenantIds                     = [local.selected_target.tenant_id]
    maxAcceleratorsPerModel       = sum([for pool in values(local.selected_queue_pools) : pool.node.gpus_per_node * pool.capacity.max_nodes])
    residencyHolderImage          = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
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
        placement = merge({
          poolRefs               = local.model_controller_qualified_pool_ids[model_id]
          acceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
          topologyPolicy         = "SingleNode"
          }, contains(local.managed_cpu_model_ids, model_id) ? {
          cpuResources = local.model_controller_qualifications[model_id].cpuResources
        } : {})
        availability = merge({
          minReplicas            = local.model_scalers[model_id].min_replicas
          maxReplicas            = local.model_scalers[model_id].max_replicas
          idleSeconds            = local.model_scalers[model_id].cooldown_seconds
          targetQueueDepth       = local.model_scalers[model_id].target_queue_depth
          pollingIntervalSeconds = local.model_scalers[model_id].polling_interval_seconds
          cooldownSeconds        = local.model_scalers[model_id].cooldown_seconds
          warmWindows            = []
          }, contains(keys(var.model_startup_timeout_overrides), model_id) ? {
          startupTimeoutSeconds = var.model_startup_timeout_overrides[model_id]
        } : {})
        cache = {
          tier               = local.model_controller_cache_tiers[model_id]
          snapshotPreference = "Never"
        }
        queue = {
          localQueue      = contains(local.managed_cpu_model_ids, model_id) ? var.general_cpu_lane.local_queue : local.selected_accelerator_pool_profile.queue.local_queue_name
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
    schema     = "fs2-serve.nebius.ai/model-bootstrap/v1"
    generation = var.release_identity_model_bootstrap_assertion_generation
    proposals  = values(local.model_controller_bootstrap_proposals)
  }
  model_controller_bootstrap_enabled = (
    var.model_controller.workload_owner == "controller" &&
    length(var.model_controller.bootstrap_model_ids) > 0
  )
  model_controller_bootstrap_security_enabled = (
    var.release_identity_model_bootstrap_authority.username != "" ||
    var.release_identity_model_bootstrap_authority.uid != "" ||
    var.release_identity_model_bootstrap_authority.credential_id != "" ||
    length(var.release_identity_model_bootstrap_retained_authorities) > 0 ||
    var.release_identity_model_bootstrap_assertion_generation != "" ||
    var.release_identity_model_bootstrap_trust_binding.enabled ||
    length(local.model_controller_bootstrap_inventory_configmaps) > 0 ||
    length(local.model_controller_bootstrap_inventory_jobs) > 0 ||
    length(local.model_controller_bootstrap_inventory_receipts) > 0 ||
    length(local.model_controller_bootstrap_inventory_verification_jobs) > 0
  )
  model_controller_bootstrap_authority_bound = (
    var.release_identity_model_bootstrap_authority.username != "" &&
    var.release_identity_model_bootstrap_authority.uid != "" &&
    var.release_identity_model_bootstrap_authority.credential_id != ""
  )
  model_controller_bootstrap_authorities_by_generation = merge(
    var.release_identity_model_bootstrap_retained_authorities,
    local.model_controller_bootstrap_authority_bound &&
    var.release_identity_model_bootstrap_assertion_generation != "" ? {
      (var.release_identity_model_bootstrap_assertion_generation) = var.release_identity_model_bootstrap_authority
    } : {},
  )
  model_controller_bootstrap_authority_generations_by_epoch = {
    for generation, authority in local.model_controller_bootstrap_authorities_by_generation :
    "epoch-${substr(sha256(generation), 0, 20)}" => generation
  }
  model_controller_bootstrap_authority_epochs = {
    for generation, authority in local.model_controller_bootstrap_authorities_by_generation :
    "epoch-${substr(sha256(generation), 0, 20)}" => authority
  }
  model_controller_bootstrap_current_authority_epoch = (
    var.release_identity_model_bootstrap_assertion_generation == "" ? "" :
    "epoch-${substr(sha256(var.release_identity_model_bootstrap_assertion_generation), 0, 20)}"
  )
  model_controller_bootstrap_current_authority_consistent = (
    !contains(
      keys(var.release_identity_model_bootstrap_retained_authorities),
      var.release_identity_model_bootstrap_assertion_generation,
    ) || try(
      var.release_identity_model_bootstrap_retained_authorities[
        var.release_identity_model_bootstrap_assertion_generation
      ] == var.release_identity_model_bootstrap_authority,
      false,
    )
  )
  model_controller_bootstrap_authority_epoch_names_consistent = alltrue([
    for authority_epoch, authority in local.model_controller_bootstrap_authority_epochs :
    authority.username == "system:serviceaccount:fs2-system:fs2-release-identity-${authority_epoch}"
  ])
  model_controller_bootstrap_authority_cel = {
    for generation, authority in local.model_controller_bootstrap_authority_epochs : generation => join(" && ", [
      "request.userInfo.username == ${jsonencode(authority.username)}",
      "request.userInfo.uid == ${jsonencode(authority.uid)}",
      "has(request.userInfo.extra)",
      "'authentication.kubernetes.io/credential-id' in request.userInfo.extra",
      "request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1",
      "request.userInfo.extra['authentication.kubernetes.io/credential-id'][0] == ${jsonencode(authority.credential_id)}",
    ])
  }
  model_controller_bootstrap_rotatable_authority_cel = join(" && ", [
    "has(object.metadata.labels)",
    "'fs2.nebius.ai/authority-epoch' in object.metadata.labels",
    "object.metadata.labels['fs2.nebius.ai/authority-epoch'].matches('^epoch-[a-f0-9]{20}$')",
    "request.userInfo.username == 'system:serviceaccount:fs2-system:fs2-release-identity-' + object.metadata.labels['fs2.nebius.ai/authority-epoch']",
    "request.userInfo.uid.matches('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')",
    "has(request.userInfo.extra)",
    "'authentication.kubernetes.io/credential-id' in request.userInfo.extra",
    "request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1",
    "request.userInfo.extra['authentication.kubernetes.io/credential-id'][0].size() >= 8",
  ])
  # These four fixed router objects and the two schema-compatibility objects,
  # plus the external security webhook, are bound by one signed bundle. Unlike the
  # generation router, their one-time CREATE authority is the exact
  # independently custodied token tuple captured by that bundle.
  model_controller_bootstrap_initial_router_authority_cel = join(" && ", [
    "request.userInfo.username == ${jsonencode(var.release_identity_admission_authority.username)}",
    "request.userInfo.uid == ${jsonencode(var.release_identity_admission_authority.uid)}",
    "has(request.userInfo.extra)",
    "'authentication.kubernetes.io/credential-id' in request.userInfo.extra",
    "request.userInfo.extra['authentication.kubernetes.io/credential-id'].size() == 1",
    "request.userInfo.extra['authentication.kubernetes.io/credential-id'][0] == ${jsonencode(var.release_identity_admission_authority.credential_id)}",
  ])
  model_controller_bootstrap_router_policy_names = {
    lifecycle = "fs2-bootstrap-epoch-router-lifecycle"
    router    = "fs2-bootstrap-epoch-router"
  }
  model_controller_bootstrap_router_admission_keys = toset(flatten([
    for name in values(local.model_controller_bootstrap_router_policy_names) : [
      "validatingadmissionpolicy/${name}",
      "validatingadmissionpolicybinding/${name}",
    ]
  ]))
  release_identity_admission_preexisting = merge(
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      "validatingadmissionpolicy/${try(item.metadata.name, "")}" => {
        uid           = try(item.metadata.uid, "")
        object_sha256 = sha256(jsonencode(item))
      }
      if startswith(try(item.metadata.name, ""), "fs2-bootstrap-") ||
      try(item.metadata.name, "") == "fs2-control-plane-schema-compatibility"
    },
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      "validatingadmissionpolicybinding/${try(item.metadata.name, "")}" => {
        uid           = try(item.metadata.uid, "")
        object_sha256 = sha256(jsonencode(item))
      }
      if startswith(try(item.metadata.name, ""), "fs2-bootstrap-") ||
      try(item.metadata.name, "") == "fs2-control-plane-schema-compatibility"
    },
  )
  release_identity_admission_adoption_authorized = (
    length(local.release_identity_admission_preexisting) == 0 || try(
      (
        toset(keys(var.release_identity_admission_authority.adopted_objects)) ==
        toset(keys(local.release_identity_admission_preexisting)) &&
        alltrue([
          for object_key, observed in local.release_identity_admission_preexisting :
          var.release_identity_admission_authority.adopted_objects[object_key].uid == observed.uid &&
          var.release_identity_admission_authority.adopted_objects[object_key].object_sha256 == observed.object_sha256
        ])
      ) || (
        var.release_identity_admission_bundle.enabled &&
        var.release_identity_admission_bundle.receipt_sha256 == var.release_identity_admission_authority.approved_receipt_sha256 &&
        var.release_identity_admission_bundle.receipt_jws_sha256 == var.release_identity_admission_authority.approved_receipt_jws_sha256 &&
        var.release_identity_admission_bundle.signer_key_sha256 == var.release_identity_admission_authority.signer_key_sha256 &&
        alltrue([
          for object_key, observed in local.release_identity_admission_preexisting :
          contains(keys(var.release_identity_admission_bundle.objects), object_key) &&
          var.release_identity_admission_bundle.objects[object_key].uid == observed.uid &&
          var.release_identity_admission_bundle.objects[object_key].object_sha256 == observed.object_sha256
        ])
      ),
      false,
    )
  )
  release_identity_external_boundary_objects = [
    for item in data.kubernetes_resources.release_identity_security_webhooks.objects : item
    if try(item.metadata.name == "fs2-security-release-admission", false)
  ]
  release_identity_external_boundary_valid = try(
    length(local.release_identity_external_boundary_objects) == 1 &&
    one(local.release_identity_external_boundary_objects).metadata.uid ==
    var.release_identity_admission_authority.external_boundary_uid &&
    sha256(jsonencode(one(local.release_identity_external_boundary_objects))) ==
    var.release_identity_admission_authority.external_boundary_object_sha256,
    false,
  )
  model_controller_bootstrap_policy_names = {
    for authority_epoch, authority in local.model_controller_bootstrap_authority_epochs : authority_epoch => {
      lifecycle = "fs2-bootstrap-policy-${authority_epoch}"
      history   = "fs2-bootstrap-history-${authority_epoch}"
      receipts  = "fs2-bootstrap-receipts-${authority_epoch}"
      trust     = "fs2-bootstrap-trust-${authority_epoch}"
      secrets   = "fs2-bootstrap-secrets-${authority_epoch}"
      verify    = "fs2-bootstrap-verify-${authority_epoch}"
    }
  }
  model_controller_bootstrap_admission_keys_by_epoch = {
    for generation, names in local.model_controller_bootstrap_policy_names : generation => toset(flatten([
      for name in values(names) : [
        "validatingadmissionpolicy/${name}",
        "validatingadmissionpolicybinding/${name}",
      ]
    ]))
  }
  model_controller_bootstrap_all_policy_names = setunion(
    toset(values(local.model_controller_bootstrap_router_policy_names)),
    toset(flatten([
      for names in values(local.model_controller_bootstrap_policy_names) : values(names)
    ])),
  )
  release_identity_admission_expected_manifests = merge(
    {
      "validatingadmissionpolicy/fs2-control-plane-schema-compatibility" = kubernetes_manifest.control_plane_schema_compatibility_policy.manifest
      "validatingadmissionpolicybinding/fs2-control-plane-schema-compatibility" = kubernetes_manifest.control_plane_schema_compatibility_policy_binding.manifest
      "validatingadmissionpolicy/${local.model_controller_bootstrap_router_policy_names.lifecycle}" = kubernetes_manifest.model_controller_bootstrap_epoch_router_lifecycle[0].manifest
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.lifecycle}" = kubernetes_manifest.model_controller_bootstrap_epoch_router_lifecycle_binding[0].manifest
      "validatingadmissionpolicy/${local.model_controller_bootstrap_router_policy_names.router}" = kubernetes_manifest.model_controller_bootstrap_epoch_router[0].manifest
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.router}" = kubernetes_manifest.model_controller_bootstrap_epoch_router_binding[0].manifest
    },
    local.model_controller_bootstrap_security_enabled ?
      merge([
        for authority_epoch, names in local.model_controller_bootstrap_policy_names : {
          "validatingadmissionpolicy/${names.lifecycle}" = kubernetes_manifest.model_controller_bootstrap_policy_lifecycle[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.lifecycle}" = kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding[authority_epoch].manifest
          "validatingadmissionpolicy/${names.history}" = kubernetes_manifest.model_controller_bootstrap_history_policy[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.history}" = kubernetes_manifest.model_controller_bootstrap_history_policy_binding[authority_epoch].manifest
          "validatingadmissionpolicy/${names.receipts}" = kubernetes_manifest.model_controller_bootstrap_receipt_policy[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.receipts}" = kubernetes_manifest.model_controller_bootstrap_receipt_policy_binding[authority_epoch].manifest
          "validatingadmissionpolicy/${names.trust}" = kubernetes_manifest.model_controller_bootstrap_trust_policy[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.trust}" = kubernetes_manifest.model_controller_bootstrap_trust_policy_binding[authority_epoch].manifest
          "validatingadmissionpolicy/${names.secrets}" = kubernetes_manifest.model_controller_bootstrap_secret_policy[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.secrets}" = kubernetes_manifest.model_controller_bootstrap_secret_policy_binding[authority_epoch].manifest
          "validatingadmissionpolicy/${names.verify}" = kubernetes_manifest.model_controller_bootstrap_verification_policy[authority_epoch].manifest
          "validatingadmissionpolicybinding/${names.verify}" = kubernetes_manifest.model_controller_bootstrap_verification_policy_binding[authority_epoch].manifest
        }
      ]...) : {},
  )
  release_identity_admission_expected_manifest_sha256s = merge(
    {
      for object_key, manifest in local.release_identity_admission_expected_manifests :
      object_key => sha256(jsonencode(manifest))
    },
    {
      "validatingwebhookconfiguration/fs2-security-release-admission" = var.release_identity_admission_authority.external_boundary_manifest_sha256
    },
  )
  model_controller_bootstrap_bound_authority_epochs = toset([
    for generation, expected_keys in local.model_controller_bootstrap_admission_keys_by_epoch : generation
    if setintersection(
      expected_keys,
      toset(keys(var.release_identity_model_bootstrap_trust_binding.admission_object_uids)),
    ) == expected_keys
  ])
  model_controller_bootstrap_bound_admission_keys = setunion(
    local.model_controller_bootstrap_router_admission_keys,
    toset(flatten([
      for generation in local.model_controller_bootstrap_bound_authority_epochs :
      tolist(local.model_controller_bootstrap_admission_keys_by_epoch[generation])
    ])),
  )
  model_controller_bootstrap_script = <<-PY
    import hashlib
    import json
    import os
    import urllib.error
    import urllib.parse
    import urllib.request
    from pathlib import Path

    base = os.environ["FS2_BOOTSTRAP_BASE_URL"].rstrip("/")
    public_origin = os.environ["FS2_BOOTSTRAP_PUBLIC_ORIGIN"].rstrip("/")
    public_authority = urllib.parse.urlsplit(public_origin).netloc
    assertion = Path("/var/run/fs2-release/assertion").read_text(encoding="utf-8").strip()
    payload_raw = Path("/bootstrap/bootstrap.json").read_bytes()
    payload = json.loads(payload_raw)

    def call(path, *, method="GET", body=None, accepted=(200,)):
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + assertion,
            "Host": public_authority,
            "Origin": public_origin,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
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

    _, response, _ = call(
        "/admin/api/v1/release/model-bootstrap",
        method="POST",
        body=payload,
    )
    data = response.get("data") if isinstance(response, dict) else None
    models = data.get("models") if isinstance(data, dict) else None
    if (
        not isinstance(models, list)
        or len(models) != len(payload["proposals"])
        or data.get("payload_sha256") != hashlib.sha256(payload_raw).hexdigest()
        or data.get("generation") != payload["generation"]
    ):
        raise RuntimeError("release model bootstrap returned an invalid receipt")
    for model in models:
        if not isinstance(model, dict) or model.get("projection") not in ("preserved", "applied", "pending"):
            raise RuntimeError("release model bootstrap returned an invalid model projection")
        print(f"model bootstrap projection: {model['name']} ({model['projection']})")
  PY
  # The identity includes every immutable Job input which is not derived from
  # its own digest key. Historical Jobs are reconstructed from this cluster-
  # retained object, never from the current image/origin or a copied tfvars map.
  model_controller_bootstrap_current_job_contract = {
    schema                      = "fs2-serve.nebius.ai/model-bootstrap-job/v1"
    bootstrap_base_url          = "http://fs2-serve-control-plane.fs2-system.svc.cluster.local:8080"
    bootstrap_public_origin     = local.public_base_url
    command                     = ["python", "/bootstrap/bootstrap.py"]
    backoff_limit               = 0
    active_deadline_seconds     = 600
    automount_service_account   = false
    restart_policy              = "Never"
    cpu_request                 = "25m"
    memory_request              = "64Mi"
    cpu_limit                   = "250m"
    memory_limit                = "256Mi"
    allow_privilege_escalation  = false
    read_only_root_filesystem   = true
    run_as_non_root             = true
    run_as_user                 = 65532
    capabilities_drop           = ["ALL"]
    bootstrap_mount_path        = "/bootstrap"
    assertion_mount_path        = "/var/run/fs2-release"
    base_labels = merge(local.common_labels, {
      "fs2.nebius.ai/authority-epoch" = local.model_controller_bootstrap_current_authority_epoch
    })
  }
  model_controller_bootstrap_current_spec = {
    assertion_generation = var.release_identity_model_bootstrap_assertion_generation
    secret_name          = var.release_identity_model_bootstrap_assertion_secret_name
    payload_json         = jsonencode(local.model_controller_bootstrap_payload)
    bootstrap_script     = local.model_controller_bootstrap_script
    runtime_image        = "${var.control_plane_image.repository}@${var.control_plane_image.digest}"
  }
  model_controller_bootstrap_current_identity = {
    schema                = "fs2-serve.nebius.ai/model-bootstrap-identity/v2"
    payload_sha256        = sha256(local.model_controller_bootstrap_current_spec.payload_json)
    implementation_sha256 = sha256(local.model_controller_bootstrap_current_spec.bootstrap_script)
    runtime_image         = local.model_controller_bootstrap_current_spec.runtime_image
    assertion_generation  = local.model_controller_bootstrap_current_spec.assertion_generation
    assertion_secret_name = local.model_controller_bootstrap_current_spec.secret_name
    job_contract          = local.model_controller_bootstrap_current_job_contract
  }
  model_controller_bootstrap_current_generation = substr(
    sha256(jsonencode(local.model_controller_bootstrap_current_identity)),
    0,
    32,
  )
  model_controller_bootstrap_inventory_configmaps = [
    for item in data.kubernetes_resources.model_controller_bootstrap_configmaps.objects : item
    if try(
      startswith(item.metadata.name, "fs2-model-bootstrap-") &&
      !startswith(item.metadata.name, "fs2-model-bootstrap-receipt-"),
      false,
    )
  ]
  model_controller_bootstrap_inventory_jobs = [
    for item in data.kubernetes_resources.model_controller_bootstrap_jobs.objects : item
    if try(startswith(item.metadata.name, "fs2-model-bootstrap-"), false)
  ]
  model_controller_bootstrap_inventory_verification_jobs = [
    for item in data.kubernetes_resources.model_controller_bootstrap_jobs.objects : item
    if try(startswith(item.metadata.name, "fs2-bootstrap-verification-"), false)
  ]
  model_controller_bootstrap_discovered_specs = {
    for item in local.model_controller_bootstrap_inventory_configmaps :
    trimprefix(item.metadata.name, "fs2-model-bootstrap-") => {
      assertion_generation = try(jsondecode(item.data["bootstrap-identity.json"]).assertion_generation, "")
      authority_epoch      = try(jsondecode(item.data["bootstrap-identity.json"]).job_contract.base_labels["fs2.nebius.ai/authority-epoch"], "")
      secret_name          = try(jsondecode(item.data["bootstrap-identity.json"]).assertion_secret_name, "")
      payload_json         = try(item.data["bootstrap.json"], "")
      bootstrap_script     = try(item.data["bootstrap.py"], "")
      runtime_image        = try(jsondecode(item.data["bootstrap-identity.json"]).runtime_image, "")
      identity             = try(jsondecode(item.data["bootstrap-identity.json"]), {})
      observed_name        = try(item.metadata.name, "")
      observed_labels      = try(item.metadata.labels, {})
      observed_generation  = try(item.metadata.labels["fs2.nebius.ai/generation"], "")
      observed_component   = try(item.metadata.labels["app.kubernetes.io/component"], "")
      observed_immutable   = try(item.immutable, false)
      observed_uid         = try(item.metadata.uid, "")
      observed_created_at  = try(item.metadata.creationTimestamp, "")
      observed_namespace   = try(item.metadata.namespace, "")
      observed_object_sha256 = sha256(jsonencode(item))
    }
  }
  model_controller_bootstrap_observed_jobs = {
    for item in local.model_controller_bootstrap_inventory_jobs :
    trimprefix(item.metadata.name, "fs2-model-bootstrap-") => {
      observed_name       = try(item.metadata.name, "")
      observed_labels     = try(item.metadata.labels, {})
      observed_generation = try(item.metadata.labels["fs2.nebius.ai/generation"], "")
      observed_component  = try(item.metadata.labels["app.kubernetes.io/component"], "")
      observed_uid        = try(item.metadata.uid, "")
      observed_created_at = try(item.metadata.creationTimestamp, "")
      observed_template_labels = try({
        for label, value in item.spec.template.metadata.labels : label => value
        if !contains([
          "batch.kubernetes.io/controller-uid",
          "batch.kubernetes.io/job-name",
          "controller-uid",
          "job-name",
        ], label)
      }, {})
      backoff_limit       = try(item.spec.backoffLimit, null)
      active_deadline     = try(item.spec.activeDeadlineSeconds, null)
      automount_token     = try(item.spec.template.spec.automountServiceAccountToken, null)
      restart_policy      = try(item.spec.template.spec.restartPolicy, "")
      container = try([
        for container in item.spec.template.spec.containers : container
        if container.name == "bootstrap"
      ][0], {})
      bootstrap_volume = try([
        for volume in item.spec.template.spec.volumes : volume
        if volume.name == "bootstrap"
      ][0], {})
      assertion_volume = try([
        for volume in item.spec.template.spec.volumes : volume
        if volume.name == "release-assertion"
      ][0], {})
      active    = coalesce(try(item.status.active, null), 0)
      succeeded = coalesce(try(item.status.succeeded, null), 0)
      failed    = coalesce(try(item.status.failed, null), 0)
      observed_namespace = try(item.metadata.namespace, "")
      observed_object_sha256 = sha256(jsonencode(item))
    }
  }
  model_controller_bootstrap_inventory_receipts = [
    for item in data.kubernetes_resources.model_controller_bootstrap_configmaps.objects : item
    if try(startswith(item.metadata.name, "fs2-model-bootstrap-receipt-"), false)
  ]
  model_controller_bootstrap_receipts = {
    for item in local.model_controller_bootstrap_inventory_receipts :
    "${try(item.metadata.labels["fs2.nebius.ai/generation"], "")}/${try(item.metadata.labels["fs2.nebius.ai/receipt-phase"], "")}" => {
      generation         = try(item.metadata.labels["fs2.nebius.ai/generation"], "")
      phase              = try(item.metadata.labels["fs2.nebius.ai/receipt-phase"], "")
      observed_name      = try(item.metadata.name, "")
      observed_namespace = try(item.metadata.namespace, "")
      observed_uid       = try(item.metadata.uid, "")
      observed_created_at = try(item.metadata.creationTimestamp, "")
      observed_labels    = try(item.metadata.labels, {})
      observed_authority_epoch = try(item.metadata.labels["fs2.nebius.ai/authority-epoch"], "")
      observed_immutable = try(item.immutable, false)
      observed_data      = try(item.data, {})
      receipt_jws        = try(item.data["receipt.jws"], "")
      observed_object_sha256 = sha256(jsonencode(item))
    }
  }
  model_controller_bootstrap_trust_configmaps = [
    for item in data.kubernetes_resources.model_controller_bootstrap_configmaps.objects : item
    if try(item.metadata.name == "fs2-serve-release-identity-trust", false)
  ]
  model_controller_bootstrap_trust_json = try(
    one(local.model_controller_bootstrap_trust_configmaps).data["trust.json"],
    "",
  )
  model_controller_bootstrap_trust_authority_epoch = try(
    one(local.model_controller_bootstrap_trust_configmaps).metadata.labels["fs2.nebius.ai/authority-epoch"],
    "",
  )
  model_controller_bootstrap_trust_key_records = flatten([
    for issuer in try(jsondecode(local.model_controller_bootstrap_trust_json).issuers, []) : [
      for key in try(issuer.keys, []) : jsonencode({
        issuer            = try(issuer.issuer, "")
        key_id            = try(key.key_id, "")
        public_key_base64 = try(key.public_key_base64, "")
      })
    ]
  ])
  model_controller_bootstrap_observed_admission_uids = merge(
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      "validatingadmissionpolicy/${try(item.metadata.name, "")}" => try(item.metadata.uid, "")
      if contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    },
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      "validatingadmissionpolicybinding/${try(item.metadata.name, "")}" => try(item.metadata.uid, "")
      if contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    },
  )
  release_identity_admission_observed_uids = merge(
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      "validatingadmissionpolicy/${try(item.metadata.name, "")}" => try(item.metadata.uid, "")
      if contains(
        keys(local.release_identity_admission_expected_manifests),
        "validatingadmissionpolicy/${try(item.metadata.name, "")}",
      )
    },
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      "validatingadmissionpolicybinding/${try(item.metadata.name, "")}" => try(item.metadata.uid, "")
      if contains(
        keys(local.release_identity_admission_expected_manifests),
        "validatingadmissionpolicybinding/${try(item.metadata.name, "")}",
      )
    },
    {
      "validatingwebhookconfiguration/fs2-security-release-admission" = try(
        one(local.release_identity_external_boundary_objects).metadata.uid,
        "",
      )
    },
  )
  release_identity_admission_observed_object_sha256s = merge(
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      "validatingadmissionpolicy/${try(item.metadata.name, "")}" => sha256(jsonencode(item))
      if contains(
        keys(local.release_identity_admission_expected_manifests),
        "validatingadmissionpolicy/${try(item.metadata.name, "")}",
      )
    },
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      "validatingadmissionpolicybinding/${try(item.metadata.name, "")}" => sha256(jsonencode(item))
      if contains(
        keys(local.release_identity_admission_expected_manifests),
        "validatingadmissionpolicybinding/${try(item.metadata.name, "")}",
      )
    },
    {
      "validatingwebhookconfiguration/fs2-security-release-admission" = try(
        sha256(jsonencode(one(local.release_identity_external_boundary_objects))),
        "",
      )
    },
  )
  release_identity_admission_bundle_valid = try(
    var.release_identity_admission_bundle.enabled &&
    local.release_identity_external_boundary_valid &&
    var.release_identity_admission_bundle.authority.username == var.release_identity_admission_authority.username &&
    var.release_identity_admission_bundle.authority.uid == var.release_identity_admission_authority.uid &&
    var.release_identity_admission_bundle.authority.credential_id == var.release_identity_admission_authority.credential_id &&
    var.release_identity_admission_bundle.signer_key_sha256 == var.release_identity_admission_authority.signer_key_sha256 &&
    var.release_identity_admission_bundle.receipt_sha256 == var.release_identity_admission_authority.approved_receipt_sha256 &&
    var.release_identity_admission_bundle.receipt_jws_sha256 == var.release_identity_admission_authority.approved_receipt_jws_sha256 &&
    toset(keys(var.release_identity_admission_bundle.objects)) ==
    toset(keys(local.release_identity_admission_expected_manifest_sha256s)) &&
    toset(keys(local.release_identity_admission_observed_uids)) ==
    toset(keys(local.release_identity_admission_expected_manifest_sha256s)) &&
    alltrue([
      for object_key, expected_manifest_sha256 in local.release_identity_admission_expected_manifest_sha256s :
      var.release_identity_admission_bundle.objects[object_key].manifest_sha256 == expected_manifest_sha256 &&
      var.release_identity_admission_bundle.objects[object_key].uid == local.release_identity_admission_observed_uids[object_key] &&
      var.release_identity_admission_bundle.objects[object_key].object_sha256 == local.release_identity_admission_observed_object_sha256s[object_key]
    ]),
    false,
  )
  model_controller_bootstrap_unexpected_admission_names = setunion(
    toset([
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      try(item.metadata.name, "")
      if startswith(try(item.metadata.name, ""), "fs2-bootstrap-") &&
      !contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    ]),
    toset([
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      try(item.metadata.name, "")
      if startswith(try(item.metadata.name, ""), "fs2-bootstrap-") &&
      !contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    ]),
  )
  model_controller_bootstrap_observed_admission_created_at = merge(
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_policies.objects :
      "validatingadmissionpolicy/${try(item.metadata.name, "")}" => try(item.metadata.creationTimestamp, "")
      if contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    },
    {
      for item in data.kubernetes_resources.model_controller_bootstrap_admission_bindings.objects :
      "validatingadmissionpolicybinding/${try(item.metadata.name, "")}" => try(item.metadata.creationTimestamp, "")
      if contains(local.model_controller_bootstrap_all_policy_names, try(item.metadata.name, ""))
    },
  )
  model_controller_bootstrap_router_admission_order_valid = try(
    local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicy/${local.model_controller_bootstrap_router_policy_names.lifecycle}"
    ] <= local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.lifecycle}"
    ] && local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicy/${local.model_controller_bootstrap_router_policy_names.router}"
    ] >= local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.lifecycle}"
    ] && local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.router}"
    ] >= local.model_controller_bootstrap_observed_admission_created_at[
      "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.lifecycle}"
    ],
    false,
  )
  model_controller_bootstrap_admission_order_valid = (
    local.model_controller_bootstrap_router_admission_order_valid &&
    alltrue([
      for generation, names in local.model_controller_bootstrap_policy_names : try(
        local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicy/${names.lifecycle}"
        ] >= local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicybinding/${local.model_controller_bootstrap_router_policy_names.router}"
        ] && local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicy/${names.lifecycle}"
        ] <= local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicybinding/${names.lifecycle}"
        ] && alltrue([
          for name in values(names) :
          local.model_controller_bootstrap_observed_admission_created_at[
            "validatingadmissionpolicy/${name}"
          ] >= local.model_controller_bootstrap_observed_admission_created_at[
            "validatingadmissionpolicybinding/${names.lifecycle}"
          ] && local.model_controller_bootstrap_observed_admission_created_at[
            "validatingadmissionpolicybinding/${name}"
          ] >= local.model_controller_bootstrap_observed_admission_created_at[
            "validatingadmissionpolicybinding/${names.lifecycle}"
          ] if name != names.lifecycle
        ]),
        false,
      ) if contains(local.model_controller_bootstrap_bound_authority_epochs, generation)
    ])
  )
  model_controller_bootstrap_admission_binding_valid = (
    var.release_identity_model_bootstrap_trust_binding.enabled &&
    length(local.model_controller_bootstrap_unexpected_admission_names) == 0 &&
    length(local.model_controller_bootstrap_bound_authority_epochs) > 0 &&
    toset(keys(var.release_identity_model_bootstrap_trust_binding.admission_object_uids)) ==
    local.model_controller_bootstrap_bound_admission_keys &&
    {
      for object_key, uid in local.model_controller_bootstrap_observed_admission_uids :
      object_key => uid
      if contains(local.model_controller_bootstrap_bound_admission_keys, object_key)
    } ==
    var.release_identity_model_bootstrap_trust_binding.admission_object_uids &&
    local.model_controller_bootstrap_admission_order_valid
  )
  model_controller_bootstrap_trust_binding_valid = (
    local.model_controller_bootstrap_admission_binding_valid &&
    length(local.model_controller_bootstrap_trust_configmaps) == 1 && try(
      one(local.model_controller_bootstrap_trust_configmaps).metadata.namespace == "fs2-system" &&
      one(local.model_controller_bootstrap_trust_configmaps).metadata.uid ==
      var.release_identity_model_bootstrap_trust_binding.config_map_uid &&
      one(local.model_controller_bootstrap_trust_configmaps).immutable == true &&
      contains(
        local.model_controller_bootstrap_bound_authority_epochs,
        local.model_controller_bootstrap_trust_authority_epoch,
      ) &&
      toset(keys(one(local.model_controller_bootstrap_trust_configmaps).data)) ==
      toset(["trust.json"]) &&
      sha256(jsonencode(one(local.model_controller_bootstrap_trust_configmaps))) ==
      var.release_identity_model_bootstrap_trust_binding.config_map_object_sha256 &&
      one(local.model_controller_bootstrap_trust_configmaps).metadata.creationTimestamp >=
      local.model_controller_bootstrap_observed_admission_created_at[
        "validatingadmissionpolicybinding/${local.model_controller_bootstrap_policy_names[local.model_controller_bootstrap_trust_authority_epoch].trust}"
      ] &&
      sha256(local.model_controller_bootstrap_trust_json) ==
      var.release_identity_model_bootstrap_trust_binding.trust_json_sha256 &&
      sha256(jsonencode(sort(local.model_controller_bootstrap_trust_key_records))) ==
      var.release_identity_model_bootstrap_trust_binding.key_set_sha256,
      false,
    )
  )
  model_controller_bootstrap_inventory_keys = sort(keys(local.model_controller_bootstrap_discovered_specs))
  model_controller_bootstrap_job_keys       = sort(keys(local.model_controller_bootstrap_observed_jobs))
  model_controller_bootstrap_receipt_queries = {
    for receipt_key, receipt in local.model_controller_bootstrap_receipts : receipt_key => {
      receipt_jws             = receipt.receipt_jws
      generation              = receipt.generation
      phase                   = receipt.phase
      receipt_uid             = receipt.observed_uid
      receipt_object_sha256   = receipt.observed_object_sha256
      identity_sha256         = sha256(jsonencode(local.model_controller_bootstrap_discovered_specs[receipt.generation].identity))
      authority_epoch         = local.model_controller_bootstrap_discovered_specs[receipt.generation].authority_epoch
      config_map_uid          = local.model_controller_bootstrap_discovered_specs[receipt.generation].observed_uid
      config_map_object_sha256 = local.model_controller_bootstrap_discovered_specs[receipt.generation].observed_object_sha256
      job_uid = receipt.phase == "terminal" ? (
        local.model_controller_bootstrap_observed_jobs[receipt.generation].observed_uid
      ) : ""
      job_object_sha256 = receipt.phase == "terminal" ? (
        local.model_controller_bootstrap_observed_jobs[receipt.generation].observed_object_sha256
      ) : ""
      trust_config_map_uid           = var.release_identity_model_bootstrap_trust_binding.config_map_uid
      trust_config_map_object_sha256 = var.release_identity_model_bootstrap_trust_binding.config_map_object_sha256
      trust_json_sha256              = var.release_identity_model_bootstrap_trust_binding.trust_json_sha256
      trust_key_set_sha256           = var.release_identity_model_bootstrap_trust_binding.key_set_sha256
      verifier_image                 = "${var.control_plane_schema_compatibility_image.repository}@${var.control_plane_schema_compatibility_image.digest}"
    }
    if local.model_controller_bootstrap_trust_binding_valid &&
    contains(local.model_controller_bootstrap_inventory_keys, receipt.generation) &&
    contains(
      local.model_controller_bootstrap_bound_authority_epochs,
      local.model_controller_bootstrap_discovered_specs[receipt.generation].authority_epoch,
    ) && (
      receipt.phase == "configmap" || (
        receipt.phase == "terminal" &&
        contains(local.model_controller_bootstrap_job_keys, receipt.generation)
      )
    )
  }
  model_controller_bootstrap_verification_queries = {
    for receipt_key, query in local.model_controller_bootstrap_receipt_queries : receipt_key => merge(query, {
      verification_id = substr(sha256(jsonencode(query)), 0, 8)
      verification_name = "fs2-bootstrap-verification-${query.generation}-${substr(sha256(jsonencode(query)), 0, 8)}"
      verification_env = {
        FS2_BOOTSTRAP_GENERATION                  = query.generation
        FS2_BOOTSTRAP_RECEIPT_PHASE               = query.phase
        FS2_BOOTSTRAP_IDENTITY_SHA256             = query.identity_sha256
        FS2_BOOTSTRAP_CONFIG_MAP_UID              = query.config_map_uid
        FS2_BOOTSTRAP_CONFIG_MAP_OBJECT_SHA256    = query.config_map_object_sha256
        FS2_BOOTSTRAP_JOB_UID                     = query.job_uid
        FS2_BOOTSTRAP_JOB_OBJECT_SHA256           = query.job_object_sha256
        FS2_BOOTSTRAP_RECEIPT_UID                 = query.receipt_uid
        FS2_BOOTSTRAP_RECEIPT_OBJECT_SHA256       = query.receipt_object_sha256
        FS2_BOOTSTRAP_TRUST_CONFIG_MAP_UID        = query.trust_config_map_uid
        FS2_BOOTSTRAP_TRUST_CONFIG_MAP_SHA256     = query.trust_config_map_object_sha256
        FS2_BOOTSTRAP_TRUST_JSON_SHA256           = query.trust_json_sha256
        FS2_BOOTSTRAP_TRUST_KEY_SET_SHA256        = query.trust_key_set_sha256
      }
    })
  }
  model_controller_bootstrap_observed_verifications = {
    for item in local.model_controller_bootstrap_inventory_verification_jobs :
    "${try(item.metadata.labels["fs2.nebius.ai/generation"], "")}/${try(item.metadata.labels["fs2.nebius.ai/receipt-phase"], "")}/${try(item.metadata.labels["fs2.nebius.ai/verification-id"], "")}" => {
      observed_name       = try(item.metadata.name, "")
      observed_namespace  = try(item.metadata.namespace, "")
      observed_labels     = try(item.metadata.labels, {})
      observed_uid        = try(item.metadata.uid, "")
      observed_created_at = try(item.metadata.creationTimestamp, "")
      active              = coalesce(try(item.status.active, null), 0)
      succeeded           = coalesce(try(item.status.succeeded, null), 0)
      failed              = coalesce(try(item.status.failed, null), 0)
      backoff_limit       = try(item.spec.backoffLimit, null)
      active_deadline     = try(item.spec.activeDeadlineSeconds, null)
      service_account     = try(item.spec.template.spec.serviceAccountName, "")
      automount_token     = try(item.spec.template.spec.automountServiceAccountToken, null)
      restart_policy      = try(item.spec.template.spec.restartPolicy, "")
      observed_template_labels = try({
        for label, value in item.spec.template.metadata.labels : label => value
        if !contains([
          "batch.kubernetes.io/controller-uid",
          "batch.kubernetes.io/job-name",
          "controller-uid",
          "job-name",
        ], label)
      }, {})
      containers          = try(item.spec.template.spec.containers, [])
      observed_object_sha256 = sha256(jsonencode(item))
    }
  }
  model_controller_bootstrap_verified_receipts = {
    for receipt_key, query in local.model_controller_bootstrap_verification_queries : receipt_key => {
      valid        = "true"
      generation   = query.generation
      phase        = query.phase
      config_map_uid = query.config_map_uid
      job_uid      = query.job_uid
    }
    if try(
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_name == query.verification_name &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_namespace == "fs2-system" &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_created_at >=
      local.model_controller_bootstrap_observed_admission_created_at[
        "validatingadmissionpolicybinding/${local.model_controller_bootstrap_policy_names[query.authority_epoch].verify}"
      ] &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_labels == merge(local.common_labels, {
        "app.kubernetes.io/component" = "model-bootstrap-verification"
        "fs2.nebius.ai/authority-epoch" = query.authority_epoch
        "fs2.nebius.ai/generation"     = query.generation
        "fs2.nebius.ai/receipt-phase"  = query.phase
        "fs2.nebius.ai/verification-id" = query.verification_id
      }) &&
      length(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_uid) > 0 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].active == 0 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].succeeded == 1 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].failed == 0 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].backoff_limit == 0 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].active_deadline == 300 &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].service_account == "fs2-model-bootstrap-verifier" &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].automount_token == true &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].restart_policy == "Never" &&
      local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].observed_template_labels == merge(local.common_labels, {
        "app.kubernetes.io/component"  = "model-bootstrap-verification"
        "fs2.nebius.ai/authority-epoch" = query.authority_epoch
        "fs2.nebius.ai/generation"      = query.generation
        "fs2.nebius.ai/receipt-phase"   = query.phase
        "fs2.nebius.ai/verification-id" = query.verification_id
      }) &&
      length(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers) == 1 &&
      one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).name == "verifier" &&
      one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).image == query.verifier_image &&
      one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).command == ["fs2-serve", "verify-model-bootstrap-retention"] &&
      {
        for environment in one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).env :
        environment.name => environment.value
      } == query.verification_env &&
      one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).resources == {
        requests = { cpu = "25m", memory = "64Mi" }
        limits   = { cpu = "250m", memory = "256Mi" }
      } &&
      one(local.model_controller_bootstrap_observed_verifications["${receipt_key}/${query.verification_id}"].containers).securityContext == {
        allowPrivilegeEscalation = false
        capabilities             = { drop = ["ALL"] }
        readOnlyRootFilesystem   = true
        runAsNonRoot             = true
        runAsUser                = 65532
      },
      false,
    )
  }
  model_controller_bootstrap_verified_specs = {
    for generation_key, spec in local.model_controller_bootstrap_discovered_specs :
    generation_key => spec
    if contains(
      keys(local.model_controller_bootstrap_verified_receipts),
      "${generation_key}/${contains(local.model_controller_bootstrap_job_keys, generation_key) ? "terminal" : "configmap"}",
    )
  }
  model_controller_bootstrap_verified_job_specs = {
    for generation_key, spec in local.model_controller_bootstrap_discovered_specs :
    generation_key => spec
    if contains(
      keys(local.model_controller_bootstrap_verified_receipts),
      "${generation_key}/terminal",
    )
  }
  model_controller_bootstrap_inventory_valid = (
    length(var.release_identity_model_bootstrap_retained_assertions) == 0 &&
    length(local.model_controller_bootstrap_inventory_configmaps) == length(local.model_controller_bootstrap_discovered_specs) &&
    length(local.model_controller_bootstrap_inventory_jobs) == length(local.model_controller_bootstrap_observed_jobs) &&
    length(local.model_controller_bootstrap_inventory_receipts) == length(local.model_controller_bootstrap_receipts) &&
    length(local.model_controller_bootstrap_receipts) == length(local.model_controller_bootstrap_receipt_queries) &&
    (
      length(local.model_controller_bootstrap_inventory_configmaps) == 0 &&
      length(local.model_controller_bootstrap_inventory_jobs) == 0 &&
      length(local.model_controller_bootstrap_inventory_receipts) == 0 ?
      true : local.model_controller_bootstrap_trust_binding_valid
    ) &&
    alltrue([
      for receipt_key, receipt in local.model_controller_bootstrap_receipts : try(
        contains(["configmap", "terminal"], receipt.phase) &&
        receipt.observed_name == "fs2-model-bootstrap-receipt-${receipt.generation}-${receipt.phase}" &&
        receipt.observed_namespace == "fs2-system" &&
        receipt.observed_created_at >= local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicybinding/${local.model_controller_bootstrap_policy_names[local.model_controller_bootstrap_discovered_specs[receipt.generation].authority_epoch].receipts}"
        ] &&
        receipt.observed_immutable == true &&
        length(receipt.observed_uid) > 0 &&
        receipt.observed_labels == merge(local.common_labels, {
          "app.kubernetes.io/component" = "model-bootstrap-retention-receipt"
          "fs2.nebius.ai/authority-epoch" = local.model_controller_bootstrap_discovered_specs[receipt.generation].authority_epoch
          "fs2.nebius.ai/generation"     = receipt.generation
          "fs2.nebius.ai/receipt-phase"  = receipt.phase
        }) &&
        toset(keys(receipt.observed_data)) == toset(["receipt.jws"]) &&
        length(receipt.receipt_jws) > 0 && length(receipt.receipt_jws) <= 8192 &&
        local.model_controller_bootstrap_receipt_queries[receipt_key].generation == receipt.generation &&
        local.model_controller_bootstrap_receipt_queries[receipt_key].phase == receipt.phase &&
        local.model_controller_bootstrap_receipt_queries[receipt_key].config_map_uid == local.model_controller_bootstrap_discovered_specs[receipt.generation].observed_uid &&
        local.model_controller_bootstrap_receipt_queries[receipt_key].job_uid == (
          receipt.phase == "terminal" ? local.model_controller_bootstrap_observed_jobs[receipt.generation].observed_uid : ""
        ),
        false,
      )
    ]) &&
    setunion(
      toset(keys(local.model_controller_bootstrap_verified_specs)),
      toset([for query in values(local.model_controller_bootstrap_verification_queries) : query.generation]),
    ) == toset(local.model_controller_bootstrap_inventory_keys) &&
    setunion(
      toset(keys(local.model_controller_bootstrap_verified_job_specs)),
      toset([
        for query in values(local.model_controller_bootstrap_verification_queries) : query.generation
        if query.phase == "terminal"
      ]),
    ) == toset(local.model_controller_bootstrap_job_keys) &&
    length(distinct([
      for spec in values(local.model_controller_bootstrap_discovered_specs) : spec.secret_name
    ])) == length(local.model_controller_bootstrap_discovered_specs) &&
    (
      length(setsubtract(
        toset(local.model_controller_bootstrap_inventory_keys),
        toset(local.model_controller_bootstrap_job_keys),
      )) == 0 || (
        local.model_controller_bootstrap_enabled &&
        setsubtract(
          toset(local.model_controller_bootstrap_inventory_keys),
          toset(local.model_controller_bootstrap_job_keys),
        ) == toset([local.model_controller_bootstrap_current_generation]) &&
        try(
          local.model_controller_bootstrap_discovered_specs[
            local.model_controller_bootstrap_current_generation
          ].identity == local.model_controller_bootstrap_current_identity,
          false,
        )
      )
    ) &&
    length(setsubtract(
      toset(local.model_controller_bootstrap_job_keys),
      toset(local.model_controller_bootstrap_inventory_keys),
    )) == 0 &&
    alltrue([
      for generation_key, spec in local.model_controller_bootstrap_discovered_specs : try(
        can(regex("^[a-f0-9]{32}$", generation_key)) &&
        spec.observed_name == "fs2-model-bootstrap-${generation_key}" &&
        spec.observed_labels == merge(spec.identity.job_contract.base_labels, {
          "app.kubernetes.io/component" = "model-bootstrap"
          "fs2.nebius.ai/generation"     = generation_key
        }) &&
        spec.observed_generation == generation_key &&
        spec.observed_component == "model-bootstrap" &&
        spec.observed_immutable == true &&
        length(spec.observed_uid) > 0 &&
        spec.observed_created_at >= local.model_controller_bootstrap_observed_admission_created_at[
          "validatingadmissionpolicybinding/${local.model_controller_bootstrap_policy_names[spec.authority_epoch].history}"
        ] &&
        spec.identity.schema == "fs2-serve.nebius.ai/model-bootstrap-identity/v2" &&
        spec.identity == {
          schema                = "fs2-serve.nebius.ai/model-bootstrap-identity/v2"
          payload_sha256        = sha256(spec.payload_json)
          implementation_sha256 = sha256(spec.bootstrap_script)
          runtime_image         = spec.runtime_image
          assertion_generation  = spec.assertion_generation
          assertion_secret_name = spec.secret_name
          job_contract          = spec.identity.job_contract
        } &&
        generation_key == substr(sha256(jsonencode(spec.identity)), 0, 32) &&
        can(regex("@sha256:[a-f0-9]{64}$", spec.runtime_image)) &&
        jsondecode(spec.payload_json).schema == "fs2-serve.nebius.ai/model-bootstrap/v1" &&
        jsondecode(spec.payload_json).generation == spec.assertion_generation &&
        spec.secret_name == "fs2-release-model-bootstrap-${spec.assertion_generation}" &&
        contains(local.model_controller_bootstrap_bound_authority_epochs, spec.authority_epoch) &&
        spec.identity.job_contract.base_labels == merge(local.common_labels, {
          "fs2.nebius.ai/authority-epoch" = spec.authority_epoch
        }) &&
        spec.identity.job_contract.schema == "fs2-serve.nebius.ai/model-bootstrap-job/v1" &&
        spec.identity.job_contract.backoff_limit == 0 &&
        spec.identity.job_contract.active_deadline_seconds == 600 &&
        spec.identity.job_contract.automount_service_account == false &&
        spec.identity.job_contract.restart_policy == "Never" &&
        spec.identity.job_contract.allow_privilege_escalation == false &&
        spec.identity.job_contract.read_only_root_filesystem == true &&
        spec.identity.job_contract.run_as_non_root == true &&
        spec.identity.job_contract.run_as_user == 65532 &&
        spec.identity.job_contract.capabilities_drop == ["ALL"] &&
        (
          !contains(local.model_controller_bootstrap_job_keys, generation_key) ? (
            local.model_controller_bootstrap_enabled &&
            generation_key == local.model_controller_bootstrap_current_generation &&
            spec.identity == local.model_controller_bootstrap_current_identity
          ) : (
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_name == "fs2-model-bootstrap-${generation_key}" &&
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_labels == merge(spec.identity.job_contract.base_labels, {
              "app.kubernetes.io/component" = "model-bootstrap"
              "fs2.nebius.ai/generation"     = generation_key
            }) &&
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_template_labels == merge(spec.identity.job_contract.base_labels, {
              "app.kubernetes.io/component" = "model-bootstrap"
              "fs2.nebius.ai/generation"     = generation_key
            }) &&
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_generation == generation_key &&
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_component == "model-bootstrap" &&
            length(local.model_controller_bootstrap_observed_jobs[generation_key].observed_uid) > 0 &&
            local.model_controller_bootstrap_observed_jobs[generation_key].observed_created_at >=
            local.model_controller_bootstrap_observed_admission_created_at[
              "validatingadmissionpolicybinding/${local.model_controller_bootstrap_policy_names[spec.authority_epoch].history}"
            ] &&
            local.model_controller_bootstrap_observed_jobs[generation_key].active == 0 &&
            (
              local.model_controller_bootstrap_observed_jobs[generation_key].succeeded > 0 ||
              local.model_controller_bootstrap_observed_jobs[generation_key].failed > 0
            ) &&
            local.model_controller_bootstrap_observed_jobs[generation_key].backoff_limit == spec.identity.job_contract.backoff_limit &&
            local.model_controller_bootstrap_observed_jobs[generation_key].active_deadline == spec.identity.job_contract.active_deadline_seconds &&
            local.model_controller_bootstrap_observed_jobs[generation_key].automount_token == spec.identity.job_contract.automount_service_account &&
            local.model_controller_bootstrap_observed_jobs[generation_key].restart_policy == spec.identity.job_contract.restart_policy &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.image == spec.runtime_image &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.command == spec.identity.job_contract.command &&
            {
              for environment in local.model_controller_bootstrap_observed_jobs[generation_key].container.env :
              environment.name => environment.value
            } == {
              FS2_BOOTSTRAP_BASE_URL      = spec.identity.job_contract.bootstrap_base_url
              FS2_BOOTSTRAP_PUBLIC_ORIGIN = spec.identity.job_contract.bootstrap_public_origin
            } &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.resources.requests == {
              cpu    = spec.identity.job_contract.cpu_request
              memory = spec.identity.job_contract.memory_request
            } &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.resources.limits == {
              cpu    = spec.identity.job_contract.cpu_limit
              memory = spec.identity.job_contract.memory_limit
            } &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.securityContext.allowPrivilegeEscalation == spec.identity.job_contract.allow_privilege_escalation &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.securityContext.readOnlyRootFilesystem == spec.identity.job_contract.read_only_root_filesystem &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.securityContext.runAsNonRoot == spec.identity.job_contract.run_as_non_root &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.securityContext.runAsUser == spec.identity.job_contract.run_as_user &&
            local.model_controller_bootstrap_observed_jobs[generation_key].container.securityContext.capabilities.drop == spec.identity.job_contract.capabilities_drop &&
            {
              for mount in local.model_controller_bootstrap_observed_jobs[generation_key].container.volumeMounts :
              mount.name => {
                mount_path = mount.mountPath
                read_only  = mount.readOnly
              }
            } == {
              bootstrap = {
                mount_path = spec.identity.job_contract.bootstrap_mount_path
                read_only  = true
              }
              "release-assertion" = {
                mount_path = spec.identity.job_contract.assertion_mount_path
                read_only  = true
              }
            } &&
            local.model_controller_bootstrap_observed_jobs[generation_key].bootstrap_volume.configMap.name == "fs2-model-bootstrap-${generation_key}" &&
            local.model_controller_bootstrap_observed_jobs[generation_key].assertion_volume.secret.secretName == spec.secret_name &&
            local.model_controller_bootstrap_observed_jobs[generation_key].assertion_volume.secret.items == [{
              key  = "assertion"
              path = "assertion"
            }]
          )
        ),
        false,
      )
    ])
  )
  model_controller_bootstrap_assertions = merge(
    local.model_controller_bootstrap_verified_specs,
    local.model_controller_bootstrap_enabled && !contains(
      local.model_controller_bootstrap_inventory_keys,
      local.model_controller_bootstrap_current_generation,
    ) ? {
      (local.model_controller_bootstrap_current_generation) = merge(
        local.model_controller_bootstrap_current_spec,
        { identity = local.model_controller_bootstrap_current_identity },
      )
    } : {},
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
    accepted_handoff_receipt = var.model_controller.handoff_receipt
    modelexpress_resources   = local.modelexpress_resource_counts
  }

  lifecycle {
    precondition {
      condition = !local.model_controller_bootstrap_security_enabled || (
        var.release_identity_kubeconfig_path != "" &&
        var.release_identity_kube_context != "" &&
        abspath(var.release_identity_kubeconfig_path) != abspath(var.kubeconfig_path) &&
        var.release_identity_kube_context != var.kube_context
      )
      error_message = "Model-bootstrap admission and protected writes require a separately custodied release-identity kubeconfig and context; the general run provider is refused."
    }

    precondition {
      condition = !local.model_controller_bootstrap_enabled || (
        local.release_identity_admission_bundle_valid
      )
      error_message = "Bootstrap execution is refused until the external security-owned admission receipt pins the canonical manifest, provider UID, and full observed object for every fixed and generation-scoped policy/binding."
    }

    precondition {
      condition = !local.model_controller_bootstrap_security_enabled || (
        length(local.model_controller_bootstrap_authority_epochs) > 0 &&
        length(local.model_controller_bootstrap_authority_epochs) == length(local.model_controller_bootstrap_authorities_by_generation) &&
        local.model_controller_bootstrap_current_authority_consistent &&
        local.model_controller_bootstrap_authority_epoch_names_consistent &&
        length(local.model_controller_bootstrap_all_policy_names) ==
        2 + 6 * length(local.model_controller_bootstrap_authority_epochs)
      )
      error_message = "Model-bootstrap security resources require at least one exact digest-derived generation-scoped release ServiceAccount whose username suffix equals its authority epoch, plus its UID and bound-token credential ID; retained epochs are append-only, a current epoch cannot redefine its authority, and digest-derived epoch or policy-name collisions fail closed."
    }

    precondition {
      condition = !local.model_controller_bootstrap_enabled || (
        local.model_controller_bootstrap_authority_bound &&
        contains(
          local.model_controller_bootstrap_bound_authority_epochs,
          local.model_controller_bootstrap_current_authority_epoch,
        )
      )
      error_message = "A bootstrap execution requires a complete integration-pinned admission-policy set for the exact current assertion generation; an expired retained epoch cannot authorize a later generation."
    }

    precondition {
      condition = !local.model_controller_bootstrap_enabled || (
        local.model_controller_bootstrap_trust_binding_valid
      )
      error_message = "Model bootstrap requires the integration-pinned trust ConfigMap UID/full-object/trust/key-set digests and exact admission policy/binding UIDs from a completed policy-first apply."
    }

    precondition {
      condition     = local.model_controller_fast_start_evidence_valid
      error_message = "Fast-start evidence must map only controller-qualified model IDs to the exact bounded wire shape emitted by project_fast_start_evidence.py."
    }

    precondition {
      condition     = local.model_controller_fast_start_mechanisms_valid
      error_message = "Each fast-start mechanism declaration must carry a matching configDigest and only reference selected accelerator pools; host-memory reservations must fit every pool's measured schedulable RAM."
    }

    precondition {
      condition     = local.fast_start_claim_declarations_valid
      error_message = "Fast-start compile-cache and residency-receipt claims must be distinct, bounded RWX infrastructure dependencies in the model runtime namespace."
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
      condition     = local.model_controller_bootstrap_inventory_valid
      error_message = "Every retained model-bootstrap generation must be discovered from exhaustive Kubernetes inventory, carry an externally signed UID/full-object-bound retention receipt, remain terminal and one-to-one, and satisfy append-only admission; caller-supplied or self-authenticating history is refused."
    }

    precondition {
      condition = !local.model_controller_bootstrap_enabled || (
        can(regex("^[a-z0-9][a-z0-9.-]{6,61}[a-z0-9]$", var.release_identity_model_bootstrap_assertion_generation)) &&
        var.release_identity_model_bootstrap_assertion_secret_name == "fs2-release-model-bootstrap-${var.release_identity_model_bootstrap_assertion_generation}" &&
        (
          !contains(
            keys(local.model_controller_bootstrap_discovered_specs),
            local.model_controller_bootstrap_current_generation,
          ) || try(
            local.model_controller_bootstrap_discovered_specs[
              local.model_controller_bootstrap_current_generation
            ].identity == local.model_controller_bootstrap_current_identity,
            false,
          )
        ) &&
        length(distinct([
          for assertion in values(local.model_controller_bootstrap_assertions) : assertion.secret_name
        ])) == length(local.model_controller_bootstrap_assertions)
      )
      error_message = "Model bootstrap requires an exact generation-named immutable assertion Secret and a complete identity which is either new or byte-identical to the provider-discovered retained generation."
    }

    # A measured elasticity receipt is status, not permission to configure
    # Kubernetes scaling. An explicit zero floor is also how a new runtime's
    # real demand-to-ready-to-idle behavior can be tested. The unchanged
    # qualification flag remains false until that test passes; snapshot and
    # fast-start claims still require their exact compatibility evidence.

    precondition {
      condition = (
        var.model_controller.workload_owner != "controller" ||
        var.model_controller.fresh_install ||
        var.model_controller.existing_controller_ownership ||
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
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_assertions

  metadata {
    name      = "fs2-model-bootstrap-${each.key}"
    namespace = "fs2-system"
    labels = merge(each.value.identity.job_contract.base_labels, {
      "app.kubernetes.io/component" = "model-bootstrap"
      "fs2.nebius.ai/generation"     = each.key
    })
  }
  immutable = true
  data = {
    "bootstrap-identity.json" = jsonencode(each.value.identity)
    "bootstrap.json"          = each.value.payload_json
    "bootstrap.py"            = each.value.bootstrap_script
  }

  lifecycle {
    prevent_destroy = true
  }
  depends_on = [
    terraform_data.model_controller_contract,
    kubernetes_manifest.model_controller_bootstrap_history_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_trust_policy_binding,
  ]
}

# A stable, externally rotatable routing guard closes the label-omission gap
# before generation-specific exact-credential policies run. It never embeds a
# reusable token or one permanent credential ID: Kubernetes must authenticate a
# bound token for an automation-only, generation-named release ServiceAccount.
# Once created, this guard protects itself and the data router from mutation.
resource "kubernetes_manifest" "model_controller_bootstrap_epoch_router_lifecycle" {
  provider = kubernetes.release_identity
  count    = 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = local.model_controller_bootstrap_router_policy_names.lifecycle
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = ["admissionregistration.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
          scope       = "Cluster"
        }]
      }
      matchConditions = [{
        name       = "model-bootstrap-epoch-router-lifecycle"
        expression = "request.name in [${jsonencode(local.model_controller_bootstrap_router_policy_names.lifecycle)},${jsonencode(local.model_controller_bootstrap_router_policy_names.router)},'fs2-control-plane-schema-compatibility'] || request.name.startsWith('fs2-bootstrap-')"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap epoch router policy and binding are append-only"
        },
        {
          expression = "request.name in [${jsonencode(local.model_controller_bootstrap_router_policy_names.lifecycle)},${jsonencode(local.model_controller_bootstrap_router_policy_names.router)},'fs2-control-plane-schema-compatibility'] || (has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'].matches('^epoch-[a-f0-9]{20}$') && request.name in ['fs2-bootstrap-policy-' + object.metadata.labels['fs2.nebius.ai/authority-epoch'],'fs2-bootstrap-history-' + object.metadata.labels['fs2.nebius.ai/authority-epoch'],'fs2-bootstrap-receipts-' + object.metadata.labels['fs2.nebius.ai/authority-epoch'],'fs2-bootstrap-trust-' + object.metadata.labels['fs2.nebius.ai/authority-epoch'],'fs2-bootstrap-secrets-' + object.metadata.labels['fs2.nebius.ai/authority-epoch'],'fs2-bootstrap-verify-' + object.metadata.labels['fs2.nebius.ai/authority-epoch']])"
          message    = "generation policy and binding names must contain the exact authority epoch label"
        },
        {
          expression = "request.name in [${jsonencode(local.model_controller_bootstrap_router_policy_names.lifecycle)},${jsonencode(local.model_controller_bootstrap_router_policy_names.router)},'fs2-control-plane-schema-compatibility'] || (${local.model_controller_bootstrap_rotatable_authority_cel})"
          message    = "generation policy creation requires a bound token whose ServiceAccount name exactly matches the authority epoch label"
        },
        {
          expression = "!(request.name in [${jsonencode(local.model_controller_bootstrap_router_policy_names.lifecycle)},${jsonencode(local.model_controller_bootstrap_router_policy_names.router)},'fs2-control-plane-schema-compatibility']) || (${local.model_controller_bootstrap_initial_router_authority_cel})"
          message    = "only the exact externally custodied release credential may create the fixed router and schema-compatibility boundary; the later signed bundle must bind their resulting UIDs and objects"
        },
        {
          expression = "object.kind != 'ValidatingAdmissionPolicyBinding' || (object.spec.policyName == object.metadata.name && object.spec.validationActions == ['Deny'])"
          message    = "model-bootstrap admission bindings must deny with the identically named policy"
        },
        {
          expression = "object.kind != 'ValidatingAdmissionPolicy' || object.spec.failurePolicy == 'Fail'"
          message    = "model-bootstrap admission policies must fail closed"
        },
      ]
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = (
        var.release_identity_kubeconfig_path != "" &&
        var.release_identity_kube_context != "" &&
        abspath(var.release_identity_kubeconfig_path) != abspath(var.kubeconfig_path) &&
        var.release_identity_kube_context != var.kube_context &&
        var.release_identity_admission_authority.username != "" &&
        var.release_identity_admission_authority.uid != "" &&
        var.release_identity_admission_authority.credential_id != "" &&
        var.release_identity_admission_authority.signer_key_sha256 != ""
      )
      error_message = "The fixed admission boundary can be established only through the distinct security-custodied provider with its exact short-lived identity and independently pinned receipt signer."
    }

    precondition {
      condition     = local.release_identity_external_boundary_valid
      error_message = "The external Platform Security webhook must already exist with the independently pinned UID/full-object identity before Terraform may create or adopt any reserved admission object."
    }

    precondition {
      condition     = local.release_identity_admission_adoption_authorized
      error_message = "A pre-existing reserved admission object is refused before Terraform adoption unless the independently custodied partial record or approved receipt pins every observed UID and full provider object."
    }
  }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "model_controller_bootstrap_epoch_router_lifecycle_binding" {
  provider = kubernetes.release_identity
  count    = 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = local.model_controller_bootstrap_router_policy_names.lifecycle
      labels = local.common_labels
    }
    spec = {
      policyName        = local.model_controller_bootstrap_router_policy_names.lifecycle
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_epoch_router_lifecycle]
}

resource "kubernetes_manifest" "model_controller_bootstrap_epoch_router" {
  provider = kubernetes.release_identity
  count    = 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name   = local.model_controller_bootstrap_router_policy_names.router
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["configmaps", "secrets"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["jobs"]
            scope       = "Namespaced"
          },
        ]
      }
      matchConditions = [{
        name = "model-bootstrap-epoch-routed-object"
        expression = "request.namespace == 'fs2-system' && (request.name == 'fs2-serve-release-identity-trust' || request.name.startsWith('fs2-model-bootstrap-') || request.name.startsWith('fs2-release-model-bootstrap-') || request.name.startsWith('fs2-bootstrap-verification-'))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap routed objects are append-only and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_rotatable_authority_cel
          message    = "only a bound-token automation release identity whose ServiceAccount name equals the object authority epoch may create routed bootstrap objects"
        },
        {
          expression = "has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'].matches('^epoch-[a-f0-9]{20}$') && (object.kind != 'Secret' || ('fs2.nebius.ai/assertion-generation' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/assertion-generation'].matches('^[a-z0-9][a-z0-9.-]{6,61}[a-z0-9]$') && object.metadata.name == 'fs2-release-model-bootstrap-' + object.metadata.labels['fs2.nebius.ai/assertion-generation']))"
          message    = "model-bootstrap routed objects require the digest-derived authority epoch; assertion Secrets additionally retain the public v1 generation in their name and label"
        },
      ]
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.release_identity_admission_adoption_authorized
      error_message = "Router creation refuses any unapproved pre-existing reserved admission object; Platform Security must pin the exact UID/full-object set before a partial-apply retry."
    }
  }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_epoch_router_lifecycle_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_epoch_router_binding" {
  provider = kubernetes.release_identity
  count    = 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = local.model_controller_bootstrap_router_policy_names.router
      labels = local.common_labels
    }
    spec = {
      policyName        = local.model_controller_bootstrap_router_policy_names.router
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_epoch_router]
}

# Protect the bootstrap admission boundary itself before any inventory can be
# admitted for recovery. Later changes use new additive policy names: these
# exact policies and bindings cannot be updated or deleted in place.
resource "kubernetes_manifest" "model_controller_bootstrap_policy_lifecycle" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].lifecycle
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = ["admissionregistration.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
            scope       = "Cluster"
          },
        ]
      }
      matchConditions = [{
        name = "model-bootstrap-policy-lifecycle"
        expression = "request.name in [" + join(",", [
          for name in values(local.model_controller_bootstrap_policy_names[each.key]) : jsonencode(name)
        ]) + "]"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap admission policies and bindings are append-only"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this authority epoch may create model-bootstrap admission policy"
        },
      ]
    }
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = local.release_identity_admission_adoption_authorized
      error_message = "Generation policy creation refuses any unapproved pre-existing reserved admission object; Platform Security must pin the exact UID/full-object set before a partial-apply retry."
    }
  }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_epoch_router_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_policy_lifecycle_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].lifecycle
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].lifecycle
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_policy_lifecycle]
}

# The cluster enforces append-only history independently of Terraform state.
# A self-consistent object pair is not authority: only an externally signed,
# UID-bound receipt admitted for the exact automation identity can make it an
# import candidate on a later plan.
resource "kubernetes_manifest" "model_controller_bootstrap_history_policy" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].history
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["configmaps"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["jobs"]
            scope       = "Namespaced"
          },
        ]
      }
      matchConditions = [{
        name       = "model-bootstrap-history"
        expression = "request.namespace == 'fs2-system' && request.name.startsWith('fs2-model-bootstrap-') && !request.name.startsWith('fs2-model-bootstrap-receipt-') && ((request.operation == 'DELETE' && has(oldObject.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in oldObject.metadata.labels && oldObject.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}) || (request.operation != 'DELETE' && has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap ConfigMaps and Jobs are append-only and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this authority epoch may create model-bootstrap history"
        },
        {
          expression = "has(object.metadata.labels) && object.metadata.labels['app.kubernetes.io/component'] == 'model-bootstrap' && object.metadata.labels['fs2.nebius.ai/generation'].matches('^[0-9a-f]{32}$') && object.metadata.name == 'fs2-model-bootstrap-' + object.metadata.labels['fs2.nebius.ai/generation']"
          message    = "model-bootstrap object name and generation labels must be exact"
        },
        {
          expression = "object.kind != 'ConfigMap' || (has(object.immutable) && object.immutable == true && has(object.data) && object.data.size() == 3 && 'bootstrap-identity.json' in object.data && 'bootstrap.json' in object.data && 'bootstrap.py' in object.data)"
          message    = "model-bootstrap ConfigMaps must be immutable and contain only the complete identity, payload, and implementation"
        },
        {
          expression = "object.kind != 'Job' || (object.spec.backoffLimit == 0 && object.spec.template.spec.automountServiceAccountToken == false && object.spec.template.spec.restartPolicy == 'Never')"
          message    = "model-bootstrap Jobs must be tokenless, single-attempt, and non-restarting"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding,
    kubernetes_manifest.model_controller_bootstrap_trust_policy_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_history_policy_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].history
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].history
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_history_policy]
}

resource "kubernetes_manifest" "model_controller_bootstrap_receipt_policy" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].receipts
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["configmaps"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name       = "model-bootstrap-retention-receipt"
        expression = "request.namespace == 'fs2-system' && request.name.startsWith('fs2-model-bootstrap-receipt-') && ((request.operation == 'DELETE' && has(oldObject.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in oldObject.metadata.labels && oldObject.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}) || (request.operation != 'DELETE' && has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap retention receipts are append-only and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this authority epoch may publish retention receipts"
        },
        {
          expression = "has(object.immutable) && object.immutable == true && has(object.data) && object.data.size() == 1 && 'receipt.jws' in object.data && object.data['receipt.jws'].size() > 0 && object.data['receipt.jws'].size() <= 8192"
          message    = "model-bootstrap retention receipts must be immutable single-JWS ConfigMaps"
        },
        {
          expression = "has(object.metadata.labels) && object.metadata.labels['app.kubernetes.io/component'] == 'model-bootstrap-retention-receipt' && object.metadata.labels['fs2.nebius.ai/generation'].matches('^[0-9a-f]{32}$') && object.metadata.labels['fs2.nebius.ai/receipt-phase'] in ['configmap', 'terminal'] && object.metadata.name == 'fs2-model-bootstrap-receipt-' + object.metadata.labels['fs2.nebius.ai/generation'] + '-' + object.metadata.labels['fs2.nebius.ai/receipt-phase']"
          message    = "model-bootstrap retention receipt name, generation, and phase must be identical"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding,
    kubernetes_manifest.model_controller_bootstrap_trust_policy_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_receipt_policy_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].receipts
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].receipts
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_receipt_policy]
}

# The public key document is the root for retained-history signatures. Once
# present it is immutable and cannot be replaced with an attacker-selected
# root; only the same automation-only release identity may create it while the
# policy is active.
resource "kubernetes_manifest" "model_controller_bootstrap_trust_policy" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].trust
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["configmaps"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name       = "release-identity-trust"
        expression = "request.namespace == 'fs2-system' && request.name == 'fs2-serve-release-identity-trust' && ((request.operation == 'DELETE' && has(oldObject.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in oldObject.metadata.labels && oldObject.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}) || (request.operation != 'DELETE' && has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "the release identity trust root is append-only and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this authority epoch may publish the trust root"
        },
        {
          expression = "has(object.immutable) && object.immutable == true && has(object.data) && object.data.size() == 1 && 'trust.json' in object.data && object.data['trust.json'].size() > 0 && object.data['trust.json'].size() <= 65536"
          message    = "the release identity trust root must be an immutable single-document ConfigMap"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_trust_policy_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].trust
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].trust
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_trust_policy]
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

resource "kubernetes_manifest" "model_controller_bootstrap_secret_policy" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].secrets
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["secrets"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name       = "model-bootstrap-assertion-secret"
        expression = "request.namespace == 'fs2-system' && request.name.startsWith('fs2-release-model-bootstrap-') && ((request.operation == 'DELETE' && has(oldObject.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in oldObject.metadata.labels && oldObject.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}) || (request.operation != 'DELETE' && has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap assertion Secrets are generation-retained and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this assertion generation may create model-bootstrap assertion Secrets"
        },
        {
          expression = "has(object.immutable) && object.immutable == true"
          message    = "model-bootstrap assertion Secrets must be immutable"
        },
        {
          expression = "has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)} && 'fs2.nebius.ai/assertion-generation' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/assertion-generation'] == ${jsonencode(local.model_controller_bootstrap_authority_generations_by_epoch[each.key])} && object.metadata.labels['fs2.nebius.ai/assertion-generation'].matches('^[a-z0-9][a-z0-9.-]{6,61}[a-z0-9]$') && object.metadata.name == 'fs2-release-model-bootstrap-' + object.metadata.labels['fs2.nebius.ai/assertion-generation']"
          message    = "model-bootstrap assertion Secret name and generation label must be identical"
        },
        {
          expression = "has(object.data) && object.data.size() == 1 && 'assertion' in object.data && (!has(object.stringData) || object.stringData.size() == 0)"
          message    = "model-bootstrap assertion Secrets contain only the assertion data key"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_secret_policy_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].secrets
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].secrets
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_secret_policy]
}

resource "kubernetes_manifest" "model_controller_bootstrap_verification_policy" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].verify
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [{
          apiGroups   = ["batch"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["jobs"]
          scope       = "Namespaced"
        }]
      }
      matchConditions = [{
        name       = "model-bootstrap-verification"
        expression = "request.namespace == 'fs2-system' && request.name.startsWith('fs2-bootstrap-verification-') && ((request.operation == 'DELETE' && has(oldObject.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in oldObject.metadata.labels && oldObject.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}) || (request.operation != 'DELETE' && has(object.metadata.labels) && 'fs2.nebius.ai/authority-epoch' in object.metadata.labels && object.metadata.labels['fs2.nebius.ai/authority-epoch'] == ${jsonencode(each.key)}))"
      }]
      validations = [
        {
          expression = "request.operation == 'CREATE'"
          message    = "model-bootstrap verification Jobs are append-only and cannot be updated or deleted"
        },
        {
          expression = local.model_controller_bootstrap_authority_cel[each.key]
          message    = "only the exact short-lived release credential for this authority epoch may create model-bootstrap verification Jobs"
        },
        {
          expression = "has(object.metadata.labels) && object.metadata.labels['app.kubernetes.io/component'] == 'model-bootstrap-verification' && object.metadata.labels['fs2.nebius.ai/generation'].matches('^[0-9a-f]{32}$') && object.metadata.labels['fs2.nebius.ai/receipt-phase'] in ['configmap', 'terminal'] && object.metadata.labels['fs2.nebius.ai/verification-id'].matches('^[0-9a-f]{8}$') && object.metadata.name == 'fs2-bootstrap-verification-' + object.metadata.labels['fs2.nebius.ai/generation'] + '-' + object.metadata.labels['fs2.nebius.ai/verification-id']"
          message    = "model-bootstrap verification Job identity must be content-addressed"
        },
        {
          expression = "object.spec.backoffLimit == 0 && object.spec.activeDeadlineSeconds == 300 && object.spec.template.spec.serviceAccountName == 'fs2-model-bootstrap-verifier' && object.spec.template.spec.automountServiceAccountToken == true && object.spec.template.spec.restartPolicy == 'Never' && object.spec.template.spec.containers.size() == 1 && object.spec.template.spec.containers[0].name == 'verifier' && object.spec.template.spec.containers[0].image.matches('^.+@sha256:[a-f0-9]{64}$')"
          message    = "model-bootstrap verification Jobs must be bounded, digest-pinned, and use only the read-only verifier identity"
        },
      ]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    terraform_data.cluster_contract,
    kubernetes_manifest.model_controller_bootstrap_policy_lifecycle_binding,
  ]
}

resource "kubernetes_manifest" "model_controller_bootstrap_verification_policy_binding" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_security_enabled ? local.model_controller_bootstrap_authority_epochs : {}

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = local.model_controller_bootstrap_policy_names[each.key].verify
      labels = merge(local.common_labels, {
        "fs2.nebius.ai/authority-epoch" = each.key
      })
    }
    spec = {
      policyName        = local.model_controller_bootstrap_policy_names[each.key].verify
      validationActions = ["Deny"]
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [kubernetes_manifest.model_controller_bootstrap_verification_policy]
}

resource "kubernetes_manifest" "model_controller_bootstrap_verifier_service_account" {
  count = local.model_controller_bootstrap_security_enabled ? 1 : 0

  manifest = {
    apiVersion = "v1"
    kind       = "ServiceAccount"
    metadata = {
      name      = "fs2-model-bootstrap-verifier"
      namespace = "fs2-system"
      labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap-verifier" })
    }
    automountServiceAccountToken = false
  }

  lifecycle { prevent_destroy = true }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "model_controller_bootstrap_verifier_role" {
  count = local.model_controller_bootstrap_security_enabled ? 1 : 0

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "Role"
    metadata = {
      name      = "fs2-model-bootstrap-verifier"
      namespace = "fs2-system"
      labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap-verifier" })
    }
    rules = concat(
      [{
        apiGroups = [""]
        resources = ["configmaps"]
        verbs     = ["get"]
        resourceNames = distinct(concat(
          ["fs2-serve-release-identity-trust"],
          [for query in values(local.model_controller_bootstrap_verification_queries) : "fs2-model-bootstrap-${query.generation}"],
          [for query in values(local.model_controller_bootstrap_verification_queries) : "fs2-model-bootstrap-receipt-${query.generation}-${query.phase}"],
        ))
      }],
      length([
        for query in values(local.model_controller_bootstrap_verification_queries) :
        query if query.phase == "terminal"
      ]) > 0 ? [{
        apiGroups = ["batch"]
        resources = ["jobs"]
        verbs     = ["get"]
        resourceNames = distinct([
          for query in values(local.model_controller_bootstrap_verification_queries) :
          "fs2-model-bootstrap-${query.generation}" if query.phase == "terminal"
        ])
      }] : [],
    )
  }

  lifecycle { prevent_destroy = true }
  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "model_controller_bootstrap_verifier_role_binding" {
  count = local.model_controller_bootstrap_security_enabled ? 1 : 0

  manifest = {
    apiVersion = "rbac.authorization.k8s.io/v1"
    kind       = "RoleBinding"
    metadata = {
      name      = "fs2-model-bootstrap-verifier"
      namespace = "fs2-system"
      labels    = merge(local.common_labels, { "app.kubernetes.io/component" = "model-bootstrap-verifier" })
    }
    roleRef = {
      apiGroup = "rbac.authorization.k8s.io"
      kind     = "Role"
      name     = "fs2-model-bootstrap-verifier"
    }
    subjects = [{
      kind      = "ServiceAccount"
      name      = "fs2-model-bootstrap-verifier"
      namespace = "fs2-system"
    }]
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    kubernetes_manifest.model_controller_bootstrap_verifier_service_account,
    kubernetes_manifest.model_controller_bootstrap_verifier_role,
  ]
}

# This Job is the apply-time fence. It runs from the retained newest-schema
# image, after all admission bindings, and re-reads the exact UID/spec/hash
# tuple from the API. Only a later plan may treat its terminal success as
# authority for declarative import.
resource "kubernetes_job_v1" "model_controller_bootstrap_receipt_verification" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_verification_queries

  metadata {
    name      = each.value.verification_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"  = "model-bootstrap-verification"
      "fs2.nebius.ai/authority-epoch" = each.value.authority_epoch
      "fs2.nebius.ai/generation"      = each.value.generation
      "fs2.nebius.ai/receipt-phase"   = each.value.phase
      "fs2.nebius.ai/verification-id" = each.value.verification_id
    })
  }

  wait_for_completion = true
  timeouts { create = "10m" }

  spec {
    backoff_limit           = 0
    active_deadline_seconds = 300
    template {
      metadata {
        labels = merge(local.common_labels, {
          "app.kubernetes.io/component"  = "model-bootstrap-verification"
          "fs2.nebius.ai/authority-epoch" = each.value.authority_epoch
          "fs2.nebius.ai/generation"      = each.value.generation
          "fs2.nebius.ai/receipt-phase"   = each.value.phase
          "fs2.nebius.ai/verification-id" = each.value.verification_id
        })
      }
      spec {
        service_account_name            = "fs2-model-bootstrap-verifier"
        automount_service_account_token = true
        restart_policy                  = "Never"
        container {
          name    = "verifier"
          image   = each.value.verifier_image
          command = ["fs2-serve", "verify-model-bootstrap-retention"]
          dynamic "env" {
            for_each = each.value.verification_env
            content {
              name  = env.key
              value = env.value
            }
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
        }
      }
    }
  }

  lifecycle { prevent_destroy = true }
  depends_on = [
    kubernetes_manifest.model_controller_bootstrap_history_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_receipt_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_trust_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_verification_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_verifier_role_binding,
  ]
}

resource "kubernetes_job_v1" "model_controller_bootstrap" {
  provider = kubernetes.release_identity
  for_each = local.model_controller_bootstrap_assertions

  metadata {
    name      = "fs2-model-bootstrap-${each.key}"
    namespace = "fs2-system"
    labels = merge(each.value.identity.job_contract.base_labels, {
      "app.kubernetes.io/component" = "model-bootstrap"
      "fs2.nebius.ai/generation"     = each.key
    })
  }
  wait_for_completion = true
  timeouts { create = "15m" }

  spec {
    # A signed release assertion is single-use. Never replay it through an
    # automatic Job retry after an ambiguous response.
    backoff_limit           = each.value.identity.job_contract.backoff_limit
    active_deadline_seconds = each.value.identity.job_contract.active_deadline_seconds
    template {
      metadata {
        labels = merge(each.value.identity.job_contract.base_labels, {
          "app.kubernetes.io/component" = "model-bootstrap"
          "fs2.nebius.ai/generation"     = each.key
        })
      }
      spec {
        automount_service_account_token = each.value.identity.job_contract.automount_service_account
        restart_policy                  = each.value.identity.job_contract.restart_policy
        container {
          name    = "bootstrap"
          image   = each.value.runtime_image
          command = each.value.identity.job_contract.command
          env {
            name  = "FS2_BOOTSTRAP_BASE_URL"
            value = each.value.identity.job_contract.bootstrap_base_url
          }
          env {
            name  = "FS2_BOOTSTRAP_PUBLIC_ORIGIN"
            value = each.value.identity.job_contract.bootstrap_public_origin
          }
          resources {
            requests = {
              cpu    = each.value.identity.job_contract.cpu_request
              memory = each.value.identity.job_contract.memory_request
            }
            limits = {
              cpu    = each.value.identity.job_contract.cpu_limit
              memory = each.value.identity.job_contract.memory_limit
            }
          }
          security_context {
            allow_privilege_escalation = each.value.identity.job_contract.allow_privilege_escalation
            read_only_root_filesystem  = each.value.identity.job_contract.read_only_root_filesystem
            run_as_non_root            = each.value.identity.job_contract.run_as_non_root
            run_as_user                = each.value.identity.job_contract.run_as_user
            capabilities { drop = each.value.identity.job_contract.capabilities_drop }
          }
          volume_mount {
            name       = "bootstrap"
            mount_path = each.value.identity.job_contract.bootstrap_mount_path
            read_only  = true
          }
          volume_mount {
            name       = "release-assertion"
            mount_path = each.value.identity.job_contract.assertion_mount_path
            read_only  = true
          }
        }
        volume {
          name = "bootstrap"
          config_map { name = kubernetes_config_map_v1.model_controller_bootstrap[each.key].metadata[0].name }
        }
        volume {
          name = "release-assertion"
          secret {
            secret_name = each.value.secret_name
            items {
              key  = "assertion"
              path = "assertion"
            }
          }
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_network_policy_v1.model_controller_bootstrap,
    kubernetes_manifest.model_controller_bootstrap_history_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_trust_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_receipt_policy_binding,
    kubernetes_manifest.model_controller_bootstrap_secret_policy_binding,
  ]
}

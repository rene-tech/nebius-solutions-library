# Scientific result artifact store, workload side.
#
# The infrastructure stage hands over three non-secret values: the S3 access-key
# ID, the opaque MysteryBox reference, and a revision. The S3 secret itself is
# resolved here as an ephemeral value and written with the provider's write-only
# argument, so it exists only for the duration of the apply. It is never in this
# stage's state, in a plan file, in generated tfvars, in a Helm release value or
# in any output.
#
# The Secret name is stable on purpose. Workers never mount it: they receive
# short-lived signed handles from the control plane, which is the only consumer
# of the key. Rotating the key changes the revision below, which rewrites the
# write-only data and moves the pod annotation, so the control plane restarts
# and picks the new credential up without the annotation ever carrying a secret.

locals {
  scientific_artifacts_enabled        = var.scientific_artifacts.enabled
  scientific_artifacts_secret_name    = "fs2-serve-artifact-store"
  scientific_artifacts_secret_key     = "credentials.json"
  scientific_runtime_cache_claim_name = "fs2-scientific-runtime-cache"
  scientific_runtime_cache_mount_path = "/cache"
  scientific_model_runtime_images = sort(distinct(flatten([
    for model in try(var.scientific_batch.execution_map.models, []) : [
      for stage in try(model.stages, []) : try(stage.image, "")
    ]
  ])))
  scientific_final_container_images = concat(
    local.scientific_model_runtime_images,
    ["${var.control_plane_image.repository}@${var.control_plane_image.digest}"],
  )
  scientific_image_supply_valid = alltrue([
    for image in local.scientific_final_container_images :
    can(regex("^[^\\s@]+@sha256:[0-9a-f]{64}$", image)) &&
    startswith(image, "${var.accelerator_pool_contract.artifact_source.registry.fqdn}/")
  ])
  scientific_image_authorizations_valid = alltrue([
    for image in local.scientific_final_container_images : anytrue([
      for authorization in values(local.verified_runtime_security_authorizations) :
      authorization.kind == "scientific-image" &&
      authorization.subject_sha256 == sha256(jsonencode({
        schema = "fs2-serve.nebius.ai/scientific-image-subject/v1"
        image  = image
      }))
    ])
  ])
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
        cache_mount_path = try(one([
          for mount in try(stage.mounts, []) : mount.mount_path
          if try(mount.kind, "") == "runtime-cache"
        ]), null)
        cache_sub_path = try(one(distinct([
          for value in values(try(stage.environment, {})) : split("/", value)[2]
          if try(startswith(value, "${local.scientific_runtime_cache_mount_path}/"), false)
        ])), null)
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
  scientific_runtime_cache_boundaries = try(var.scientific_batch.execution_map.runtime_cache_boundaries, [])
  scientific_runtime_cache_directory_claims = [
    for boundary in local.scientific_runtime_cache_boundaries : {
      name               = try(boundary.directory, "")
      uid                = try(boundary.run_as_user, null)
      gid                = try(boundary.run_as_group, null)
      legacy_uid         = try(boundary.legacy_uid, null)
      legacy_gid         = try(boundary.legacy_gid, null)
      tenant_id          = try(boundary.tenant_id, "")
      model_id           = try(boundary.model_id, "")
      stage_id           = "tenant-boundary"
      workload_namespace = try(boundary.workload_namespace, "")
      origin             = try(boundary.origin, "")
      activation_id      = try(boundary.activation_id, "")
      authorization_id   = try(boundary.authorization_id, "")
      boundary_sha256    = try(boundary.boundary_sha256, "")
    }
  ]
  scientific_runtime_cache_directory_claims_by_name = {
    for claim in local.scientific_runtime_cache_directory_claims : claim.name => claim...
  }
  scientific_runtime_cache_primary_directory_claims_by_name = {
    for claim in local.scientific_runtime_cache_directory_claims : claim.name => claim...
    if claim.workload_namespace == var.scientific_batch.namespace
  }
  scientific_runtime_cache_directories = [
    for name in sort(keys(local.scientific_runtime_cache_primary_directory_claims_by_name)) : {
      name            = name
      uid             = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].uid
      gid             = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].gid
      legacy_uid      = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].legacy_uid
      legacy_gid      = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].legacy_gid
      tenant_id       = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].tenant_id
      model_id        = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].model_id
      origin          = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].origin
      boundary_sha256 = local.scientific_runtime_cache_primary_directory_claims_by_name[name][0].boundary_sha256
      migration_phase = "journaled-dual-access-legacy-group"
      mode            = "2770"
    }
  ]
  scientific_runtime_cache_writer_fence_name = "fs2-scientific-runtime-cache-writer-fence"
  scientific_runtime_cache_ownership_contracts_by_namespace = merge(
    { (var.scientific_batch.namespace) = local.scientific_runtime_cache_ownership_contract },
    local.scientific_runtime_cache_additional_ownership_contracts,
  )
  scientific_runtime_cache_bootstrap_instance_cel = [
    for namespace, claim in local.scientific_runtime_cache_namespace_claims : format(
      "(object.metadata.namespace == %s && ((request.resource.resource == 'jobs' && request.userInfo.username == %s && variables.hasPolicyOwner && object.metadata.name == %s) || (request.resource.resource == 'pods' && request.subResource != 'ephemeralcontainers' && request.userInfo.username == %s && has(object.metadata.ownerReferences) && object.metadata.ownerReferences.size() == 1 && object.metadata.ownerReferences.exists_one(o, o.apiVersion == 'batch/v1' && o.kind == 'Job' && o.name == %s && o.uid.matches('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') && o.controller == true && o.blockOwnerDeletion == true))))",
      jsonencode(namespace),
      jsonencode(var.scientific_batch.runtime_cache.admission_actors.bootstrap_job_creator),
      jsonencode(claim.bootstrap_job),
      jsonencode(var.scientific_batch.runtime_cache.admission_actors.job_controller),
      jsonencode(claim.bootstrap_job),
    )
  ]
  scientific_runtime_cache_bootstrap_contract_cel = [
    for namespace, contract in local.scientific_runtime_cache_ownership_contracts_by_namespace : format(
      "(object.metadata.namespace == %s && variables.podSpec.containers[0].env[0].value == %s)",
      jsonencode(namespace),
      jsonencode(jsonencode(contract)),
    )
  ]
  scientific_runtime_cache_writer_boundary_cel = [
    for boundary in local.scientific_runtime_cache_directory_claims : format(
      "(object.metadata.namespace == %s && variables.podMetadata.annotations[%s] == %s && variables.podMetadata.annotations[%s] == %s && variables.podSpec.securityContext.supplementalGroupsPolicy == 'Strict' && variables.podSpec.securityContext.supplementalGroups.exists(g, g == %d) && variables.podSpec.securityContext.supplementalGroups.filter(g, variables.allCacheGroups.exists(cacheGroup, cacheGroup == g)).size() == 1 && variables.podSpec.containers.exists_one(c, c.name == 'scientific-stage' && c.securityContext.runAsUser == %d && c.securityContext.runAsGroup == %d && c.env.exists_one(e, e.name == 'FS2_RUNTIME_CACHE_ACTIVATION_ID' && e.value == %s) && c.volumeMounts.filter(m, variables.cacheVolumeNames.exists(v, v == m.name)).size() == 2 && c.volumeMounts.exists_one(m, variables.cacheVolumeNames.exists(v, v == m.name) && m.mountPath == '/cache' && m.subPath == %s && m.readOnly == false) && c.volumeMounts.exists_one(m, variables.cacheVolumeNames.exists(v, v == m.name) && m.mountPath == '/var/run/fs2-cache-writer-admission.lock' && m.subPath == '.fs2-cache-writer-admission.lock' && m.readOnly == true)) && variables.podSpec.containers.filter(c, c.name != 'scientific-stage').all(c, !has(c.volumeMounts) || c.volumeMounts.all(m, !variables.cacheVolumeNames.exists(v, v == m.name))) && (!has(variables.podSpec.initContainers) || variables.podSpec.initContainers.all(c, !has(c.volumeMounts) || c.volumeMounts.all(m, !variables.cacheVolumeNames.exists(v, v == m.name)))) && (!has(variables.podSpec.ephemeralContainers) || variables.podSpec.ephemeralContainers.all(c, !has(c.volumeMounts) || c.volumeMounts.all(m, !variables.cacheVolumeNames.exists(v, v == m.name)))))",
      jsonencode(boundary.workload_namespace),
      jsonencode("fs2-serve.nebius.ai/runtime-cache-activation"),
      jsonencode(boundary.activation_id),
      jsonencode("fs2-serve.nebius.ai/runtime-cache-boundary"),
      jsonencode(boundary.boundary_sha256),
      boundary.legacy_gid,
      boundary.run_as_user,
      boundary.run_as_group,
      jsonencode(boundary.activation_id),
      jsonencode(boundary.directory),
    )
  ]
  scientific_runtime_cache_writer_fence_manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.scientific_runtime_cache_writer_fence_name
      labels = local.common_labels
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["UPDATE"]
            resources   = ["pods/ephemeralcontainers"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobs"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["jobset.x-k8s.io"]
            apiVersions = ["v1alpha2"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobsets"]
            scope       = "Namespaced"
          },
        ]
      }
      matchConditions = [{
        name = "scientific-runtime-cache-claim-only"
        expression = "${jsonencode(local.scientific_runtime_cache_consumer_namespaces)}.exists(namespace, namespace == object.metadata.namespace) && (request.resource.resource == 'pods' ? has(object.spec.volumes) && object.spec.volumes.exists(v, has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == '${local.scientific_runtime_cache_claim_name}') : request.resource.resource == 'jobs' ? has(object.spec.template.spec.volumes) && object.spec.template.spec.volumes.exists(v, has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == '${local.scientific_runtime_cache_claim_name}') : has(object.spec.replicatedJobs) && object.spec.replicatedJobs.exists(rj, has(rj.template.spec.template.spec.volumes) && rj.template.spec.template.spec.volumes.exists(v, has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == '${local.scientific_runtime_cache_claim_name}')))"
      }]
      variables = [
        {
          name = "podSpec"
          expression = "request.resource.resource == 'pods' ? object.spec : request.resource.resource == 'jobs' ? object.spec.template.spec : object.spec.replicatedJobs[0].template.spec.template.spec"
        },
        {
          name = "podMetadata"
          expression = "request.resource.resource == 'pods' ? object.metadata : request.resource.resource == 'jobs' ? object.spec.template.metadata : object.spec.replicatedJobs[0].template.spec.template.metadata"
        },
        {
          name = "cacheVolumeNames"
          expression = "has(variables.podSpec.volumes) ? variables.podSpec.volumes.filter(v, has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == '${local.scientific_runtime_cache_claim_name}').map(v, v.name) : []"
        },
        {
          name       = "allCacheGroups"
          expression = jsonencode(sort(distinct(concat([for boundary in local.scientific_runtime_cache_directory_claims : boundary.run_as_group], [for boundary in local.scientific_runtime_cache_directory_claims : boundary.legacy_gid]))))
        },
        {
          name       = "hasPolicyOwner"
          expression = "has(object.metadata.ownerReferences) && object.metadata.ownerReferences.size() == 1 && object.metadata.ownerReferences.exists_one(o, o.apiVersion == 'admissionregistration.k8s.io/v1' && o.kind == 'ValidatingAdmissionPolicy' && o.name == '${local.scientific_runtime_cache_writer_fence_name}' && o.uid == '${var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid}' && o.controller == false && o.blockOwnerDeletion == false)"
        },
        {
          name       = "isControllerProduct"
          expression = "(request.resource.resource == 'pods' && request.subResource != 'ephemeralcontainers' && request.userInfo.username == ${jsonencode(var.scientific_batch.runtime_cache.admission_actors.job_controller)} && has(object.metadata.ownerReferences) && object.metadata.ownerReferences.size() == 1 && object.metadata.ownerReferences.exists_one(o, o.apiVersion == 'batch/v1' && o.kind == 'Job' && o.uid.matches('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') && o.controller == true && o.blockOwnerDeletion == true)) || (request.resource.resource == 'jobs' && request.userInfo.username == ${jsonencode(var.scientific_batch.runtime_cache.admission_actors.jobset_controller)} && has(object.metadata.ownerReferences) && object.metadata.ownerReferences.size() == 1 && object.metadata.ownerReferences.exists_one(o, o.apiVersion == 'jobset.x-k8s.io/v1alpha2' && o.kind == 'JobSet' && o.uid.matches('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') && o.controller == true && o.blockOwnerDeletion == true))"
        },
        {
          name       = "isDirectWorkload"
          expression = "request.resource.resource in ['jobs', 'jobsets'] && request.userInfo.username == ${jsonencode(var.scientific_batch.runtime_cache.admission_actors.scientific_workload_creator)} && variables.hasPolicyOwner"
        },
        {
          name       = "isBootstrapInstance"
          expression = join(" || ", local.scientific_runtime_cache_bootstrap_instance_cel)
        },
        {
          name       = "bootstrapEnvelopeExact"
          expression = "variables.podMetadata.labels['app.kubernetes.io/component'] == 'scientific-runtime-cache-bootstrap' && variables.podSpec.serviceAccountName == 'fs2-scientific-cache-bootstrap' && variables.podSpec.automountServiceAccountToken == false && variables.podSpec.enableServiceLinks == false && variables.podSpec.restartPolicy == 'Never' && (!has(variables.podSpec.hostNetwork) || variables.podSpec.hostNetwork == false) && (!has(variables.podSpec.hostPID) || variables.podSpec.hostPID == false) && (!has(variables.podSpec.hostIPC) || variables.podSpec.hostIPC == false) && (!has(variables.podSpec.shareProcessNamespace) || variables.podSpec.shareProcessNamespace == false) && variables.podSpec.nodeSelector == {'workload.fs2.nebius/system':'true','capacity.fs2.nebius/pool':'system','storage.fs2.nebius/shared-cache':'true'} && variables.podSpec.securityContext.runAsNonRoot == false && variables.podSpec.securityContext.seccompProfile.type == 'RuntimeDefault' && !has(variables.podSpec.securityContext.runAsUser) && !has(variables.podSpec.securityContext.runAsGroup) && !has(variables.podSpec.securityContext.fsGroup) && (!has(variables.podSpec.securityContext.supplementalGroups) || variables.podSpec.securityContext.supplementalGroups.size() == 0) && (!has(variables.podSpec.securityContext.sysctls) || variables.podSpec.securityContext.sysctls.size() == 0) && variables.podSpec.volumes.size() == 1 && variables.podSpec.volumes[0].name == 'runtime-cache' && variables.podSpec.volumes[0].persistentVolumeClaim.claimName == '${local.scientific_runtime_cache_claim_name}' && variables.podSpec.volumes[0].persistentVolumeClaim.readOnly == false && (!has(variables.podSpec.initContainers) || variables.podSpec.initContainers.size() == 0) && (!has(variables.podSpec.ephemeralContainers) || variables.podSpec.ephemeralContainers.size() == 0) && variables.podSpec.containers.size() == 1 && variables.podSpec.containers[0].name == 'prepare' && variables.podSpec.containers[0].image == ${jsonencode("${var.control_plane_image.repository}@${var.control_plane_image.digest}")} && variables.podSpec.containers[0].command == ${jsonencode(["python", "-c", file("${path.module}/scripts/scientific_runtime_cache_bootstrap.py")])} && (!has(variables.podSpec.containers[0].args) || variables.podSpec.containers[0].args.size() == 0) && (!has(variables.podSpec.containers[0].envFrom) || variables.podSpec.containers[0].envFrom.size() == 0) && variables.podSpec.containers[0].env.size() == 1 && variables.podSpec.containers[0].env[0].name == 'FS2_SCIENTIFIC_RUNTIME_CACHE_OWNERSHIP_JSON' && (${join(" || ", local.scientific_runtime_cache_bootstrap_contract_cel)}) && variables.podSpec.containers[0].volumeMounts.size() == 1 && variables.podSpec.containers[0].volumeMounts[0].name == 'runtime-cache' && variables.podSpec.containers[0].volumeMounts[0].mountPath == '/cache' && variables.podSpec.containers[0].volumeMounts[0].readOnly == false && !has(variables.podSpec.containers[0].volumeMounts[0].subPath) && !has(variables.podSpec.containers[0].volumeMounts[0].subPathExpr) && (!has(variables.podSpec.containers[0].volumeDevices) || variables.podSpec.containers[0].volumeDevices.size() == 0) && (!has(variables.podSpec.containers[0].ports) || variables.podSpec.containers[0].ports.size() == 0) && variables.podSpec.containers[0].securityContext.runAsUser == 0 && variables.podSpec.containers[0].securityContext.runAsGroup == 0 && variables.podSpec.containers[0].securityContext.runAsNonRoot == false && variables.podSpec.containers[0].securityContext.allowPrivilegeEscalation == false && variables.podSpec.containers[0].securityContext.readOnlyRootFilesystem == true && (!has(variables.podSpec.containers[0].securityContext.privileged) || variables.podSpec.containers[0].securityContext.privileged == false) && variables.podSpec.containers[0].securityContext.capabilities.add == ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'FSETID'] && variables.podSpec.containers[0].securityContext.capabilities.drop == ['ALL'] && !has(variables.podSpec.containers[0].securityContext.procMount) && !has(variables.podSpec.containers[0].securityContext.seLinuxOptions) && !has(variables.podSpec.containers[0].securityContext.windowsOptions) && (!has(variables.podSpec.containers[0].securityContext.appArmorProfile) || variables.podSpec.containers[0].securityContext.appArmorProfile.type == 'RuntimeDefault')"
        },
        {
          name       = "isBootstrap"
          expression = "variables.isBootstrapInstance && variables.bootstrapEnvelopeExact && (request.resource.resource != 'jobs' || object.spec.backoffLimit == 3 && object.spec.activeDeadlineSeconds == 600 && (!has(object.spec.parallelism) || object.spec.parallelism == 1) && (!has(object.spec.completions) || object.spec.completions == 1) && (!has(object.spec.completionMode) || object.spec.completionMode == 'NonIndexed') && (!has(object.spec.suspend) || object.spec.suspend == false) && (!has(object.spec.manualSelector) || object.spec.manualSelector == false) && !has(object.spec.ttlSecondsAfterFinished) && !has(object.spec.podFailurePolicy) && object.spec.template.spec == variables.podSpec)"
        },
      ]
      validations = [
        {
          expression = "request.resource.resource != 'jobsets' || object.spec.replicatedJobs.size() == 1"
          message    = "scientific runtime-cache JobSets require one exact replicated-job template"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || request.subResource != 'ephemeralcontainers'"
          message    = "ephemeral-container updates may not target scientific runtime-cache Pods"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || variables.cacheVolumeNames.size() == 1"
          message    = "a workload may project at most one scientific runtime-cache claim"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || variables.isBootstrap || variables.isDirectWorkload || variables.isControllerProduct"
          message    = "scientific runtime-cache access requires an authenticated controller path and exact owner chain"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || variables.podSpec.containers.all(c, (!has(c.volumeMounts) || c.volumeMounts.all(m, !has(m.subPathExpr))) && (!has(c.ports) || c.ports.all(p, !has(p.hostIP) && (!has(p.hostPort) || p.hostPort == 0)))) && (!has(variables.podSpec.initContainers) || variables.podSpec.initContainers.all(c, (!has(c.volumeMounts) || c.volumeMounts.all(m, !has(m.subPathExpr))) && (!has(c.ports) || c.ports.all(p, !has(p.hostIP) && (!has(p.hostPort) || p.hostPort == 0))))) && (!has(variables.podSpec.ephemeralContainers) || variables.podSpec.ephemeralContainers.all(c, (!has(c.volumeMounts) || c.volumeMounts.all(m, !has(m.subPathExpr))) && (!has(c.ports) || c.ports.all(p, !has(p.hostIP) && (!has(p.hostPort) || p.hostPort == 0)))))"
          message    = "scientific runtime-cache workloads may not use subPathExpr or host ports"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || variables.podSpec.containers.all(c, !has(c.volumeDevices) || c.volumeDevices.size() == 0) && (!has(variables.podSpec.initContainers) || variables.podSpec.initContainers.all(c, !has(c.volumeDevices) || c.volumeDevices.size() == 0)) && (!has(variables.podSpec.ephemeralContainers) || variables.podSpec.ephemeralContainers.all(c, !has(c.volumeDevices) || c.volumeDevices.size() == 0))"
          message    = "scientific runtime-cache workloads may not project block devices"
        },
        {
          expression = "variables.cacheVolumeNames.size() == 0 || variables.isBootstrap || (${join(" || ", local.scientific_runtime_cache_writer_boundary_cel)})"
          message    = "scientific runtime-cache writers require the exact activated tenant/model boundary or immutable bootstrap"
        },
      ]
    }
  }
  scientific_runtime_cache_writer_fence_sha256 = sha256(jsonencode(
    local.scientific_runtime_cache_writer_fence_manifest
  ))
  # The runtime contract deliberately excludes admission_policy_sha256 and
  # quiescence_sha256. The independently signed Terraform precondition below
  # binds both values to the complete VAP manifest. Excluding those two
  # self-referential digests lets the same VAP compare the bootstrap Job's
  # ownership JSON byte-for-byte instead of trusting a same-author assertion.
  scientific_runtime_cache_active_fence = {
    schema                            = "fs2-serve.nebius.ai/scientific-runtime-cache-active-fence/v1"
    lease_name                        = var.scientific_batch.runtime_cache.migration_quiescence.lease_name
    lease_uid                         = var.scientific_batch.runtime_cache.migration_quiescence.lease_uid
    lock_device                       = var.scientific_batch.runtime_cache.migration_quiescence.lock_device
    lock_inode                        = var.scientific_batch.runtime_cache.migration_quiescence.lock_inode
    lock_content_sha256               = var.scientific_batch.runtime_cache.migration_quiescence.lock_content_sha256
    zero_writers                      = var.scientific_batch.runtime_cache.migration_quiescence.zero_writers
    writer_admission_fenced           = var.scientific_batch.runtime_cache.migration_quiescence.writer_admission_fenced
    active_writer_count               = var.scientific_batch.runtime_cache.migration_quiescence.active_writer_count
    activation_id                     = var.scientific_batch.runtime_cache.migration_quiescence.activation_id
    admission_policy_name             = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_name
    admission_policy_uid              = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid
    admission_policy_resource_version = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_resource_version
    admission_binding_name            = var.scientific_batch.runtime_cache.migration_quiescence.admission_binding_name
    observed_at                       = var.scientific_batch.runtime_cache.migration_quiescence.observed_at
    expires_at                        = var.scientific_batch.runtime_cache.migration_quiescence.expires_at
    evidence_sha256                   = var.scientific_batch.runtime_cache.migration_quiescence.evidence_sha256
    authorization_id                  = var.scientific_batch.runtime_cache.migration_quiescence.authorization_id
  }
  scientific_runtime_cache_ownership_contract = {
    schema             = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v4"
    root               = local.scientific_runtime_cache_mount_path
    writer_quiescence  = local.scientific_runtime_cache_active_fence
    directories        = local.scientific_runtime_cache_directories
  }
  scientific_runtime_cache_quiescence_valid = try(
    var.scientific_batch.runtime_cache.migration_quiescence.quiescence_sha256 == sha256(jsonencode({
      schema          = "fs2-serve.nebius.ai/scientific-runtime-cache-quiescence/v2"
      lease_name      = var.scientific_batch.runtime_cache.migration_quiescence.lease_name
      lease_uid       = var.scientific_batch.runtime_cache.migration_quiescence.lease_uid
      lock_device     = var.scientific_batch.runtime_cache.migration_quiescence.lock_device
      lock_inode      = var.scientific_batch.runtime_cache.migration_quiescence.lock_inode
      lock_content_sha256 = var.scientific_batch.runtime_cache.migration_quiescence.lock_content_sha256
      zero_writers    = var.scientific_batch.runtime_cache.migration_quiescence.zero_writers
      writer_admission_fenced = var.scientific_batch.runtime_cache.migration_quiescence.writer_admission_fenced
      active_writer_count = var.scientific_batch.runtime_cache.migration_quiescence.active_writer_count
      activation_id   = var.scientific_batch.runtime_cache.migration_quiescence.activation_id
      admission_policy_name = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_name
      admission_policy_uid = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid
      admission_policy_resource_version = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_resource_version
      admission_policy_sha256 = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_sha256
      admission_binding_name = var.scientific_batch.runtime_cache.migration_quiescence.admission_binding_name
      observed_at     = var.scientific_batch.runtime_cache.migration_quiescence.observed_at
      expires_at      = var.scientific_batch.runtime_cache.migration_quiescence.expires_at
      evidence_sha256 = var.scientific_batch.runtime_cache.migration_quiescence.evidence_sha256
    })) &&
    var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_sha256 == local.scientific_runtime_cache_writer_fence_sha256 &&
    contains(
      keys(local.verified_runtime_security_authorizations),
      var.scientific_batch.runtime_cache.migration_quiescence.authorization_id,
    ) &&
    local.verified_runtime_security_authorizations[var.scientific_batch.runtime_cache.migration_quiescence.authorization_id].kind == "cache-migration-quiescence" &&
    local.verified_runtime_security_authorizations[var.scientific_batch.runtime_cache.migration_quiescence.authorization_id].subject_schema == "fs2-serve.nebius.ai/scientific-runtime-cache-quiescence/v2" &&
    local.verified_runtime_security_authorizations[var.scientific_batch.runtime_cache.migration_quiescence.authorization_id].subject_sha256 == var.scientific_batch.runtime_cache.migration_quiescence.quiescence_sha256,
    false,
  )
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
        name            = name
        uid             = claims_by_name[name][0].uid
        gid             = claims_by_name[name][0].gid
        legacy_uid      = claims_by_name[name][0].legacy_uid
        legacy_gid      = claims_by_name[name][0].legacy_gid
        tenant_id       = claims_by_name[name][0].tenant_id
        model_id        = claims_by_name[name][0].model_id
        origin          = claims_by_name[name][0].origin
        boundary_sha256 = claims_by_name[name][0].boundary_sha256
        migration_phase = "journaled-dual-access-legacy-group"
        mode            = "2770"
      }
    ]
  }
  scientific_runtime_cache_additional_ownership_contracts = {
    for namespace, directories in local.scientific_runtime_cache_additional_directories : namespace => {
      schema            = "fs2-serve.nebius.ai/scientific-runtime-cache-ownership/v4"
      root              = local.scientific_runtime_cache_mount_path
      writer_quiescence = local.scientific_runtime_cache_active_fence
      directories       = directories
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
  # The rollout identity must move whenever the mounted credential could differ.
  # The cloud key's resource_version restarts at zero when the key is replaced,
  # so a revision derived from it alone repeats after a rotation and leaves the
  # stale secret mounted. Cover the key's own identity instead, and keep the
  # operator's explicit generation as the leading term so a deliberate rotation
  # is always an increase.
  scientific_artifacts_credential_identity = local.scientific_artifacts_enabled ? join("|", [
    var.scientific_artifacts.object_storage_access.key_id,
    var.scientific_artifacts.object_storage_access.access_key_id,
    var.scientific_artifacts.object_storage_access.secret_reference_id,
    tostring(var.scientific_artifacts.object_storage_access.resource_version),
  ]) : ""
  scientific_artifacts_revision = local.scientific_artifacts_enabled ? (
    var.scientific_artifacts.credential_generation * 16777216 +
    parseint(substr(sha256(local.scientific_artifacts_credential_identity), 0, 6), 16)
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
        maxBytes         = var.scientific_artifacts.max_artifact_bytes
        retentionSeconds = var.scientific_artifacts.retention_days * 86400
        mediaTypes       = sort(var.scientific_artifacts.media_types)
        egressCidrs      = sort(var.scientific_artifacts.egress_cidrs)
      }
      secrets = {
        artifactStore = {
          name = local.scientific_artifacts_secret_name
          key  = local.scientific_artifacts_secret_key
        }
      }
      networkPolicy = {
        artifactStoreCidrs = sort(var.scientific_artifacts.egress_cidrs)
      }
      # Non-secret rollout trigger. It carries the credential revision, never
      # the credential, so a rotation restarts the control plane deterministically.
      podAnnotations = {
        "fs2.nebius.ai/artifact-store-credential-revision" = tostring(local.scientific_artifacts_revision)
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
        {
          runtime_security_trust = {
            session_id = local.runtime_security_authority_session_id
          }
          runtime_security_authorizations = {
            for authorization_id, authorization in local.verified_runtime_security_authorizations :
            authorization_id => {
              kind               = authorization.kind
              model_id           = authorization.model_id
              subject_schema     = authorization.subject_schema
              subject_sha256     = authorization.subject_sha256
              evidence_sha256    = authorization.evidence_sha256
              evidence           = authorization.evidence
              attestation_sha256 = authorization.attestation_sha256
              attestation        = authorization.attestation
              verified_key_id    = authorization.verified_key_id
            }
          }
          runtime_cache_admission = var.scientific_batch.runtime_cache.enabled ? {
            apiVersion         = "admissionregistration.k8s.io/v1"
            kind               = "ValidatingAdmissionPolicy"
            name               = local.scientific_runtime_cache_writer_fence_name
            uid                = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid
            controller         = false
            blockOwnerDeletion = false
          } : null
        },
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

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "scientific_artifacts" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  secret_id = var.scientific_artifacts.object_storage_access.secret_reference_id
  key       = "secret"
}

resource "kubernetes_secret_v1" "scientific_artifact_store" {
  count = local.scientific_artifacts_enabled ? 1 : 0

  metadata {
    name      = local.scientific_artifacts_secret_name
    namespace = "fs2-system"
    labels = merge(local.common_labels, {
      "fs2.nebius.ai/credential-purpose" = "scientific-artifact-store"
    })
    annotations = {
      # Non-secret. It exists so an operator can tell which key generation the
      # cluster currently holds without reading the Secret's data.
      "fs2.nebius.ai/artifact-store-credential-revision"   = tostring(local.scientific_artifacts_revision)
      "fs2.nebius.ai/artifact-store-credential-generation" = tostring(var.scientific_artifacts.credential_generation)
      "fs2.nebius.ai/artifact-store-access-key-id"         = var.scientific_artifacts.object_storage_access.access_key_id
    }
  }

  type = "Opaque"

  # data_wo keeps the value out of state entirely; plain `data` would persist it.
  data_wo = {
    (local.scientific_artifacts_secret_key) = jsonencode({
      access_key_id     = var.scientific_artifacts.object_storage_access.access_key_id
      secret_access_key = ephemeral.nebius_mysterybox_v1_secret_payload_entry.scientific_artifacts[0].data.string_value
    })
  }
  # Monotonic with the cloud-side key version, so rotating the access key is the
  # only thing that rewrites the Secret.
  data_wo_revision = local.scientific_artifacts_revision

  depends_on = [terraform_data.cluster_contract]
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

# A cluster-scoped fail-closed admission fence prevents a legacy or
# non-cooperating Pod from opening the RWX claim after the independent
# zero-writer observation. The signed quiescence record binds this exact
# manifest digest plus the already-observed policy UID/resourceVersion; a
# boolean assertion alone is never migration authority.
resource "kubernetes_manifest" "scientific_runtime_cache_writer_fence" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  manifest = local.scientific_runtime_cache_writer_fence_manifest

  field_manager {
    name            = "fs2-scientific-runtime-cache-security"
    force_conflicts = false
  }

  depends_on = [terraform_data.cluster_contract]
}

resource "kubernetes_manifest" "scientific_runtime_cache_writer_fence_binding" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name   = local.scientific_runtime_cache_writer_fence_name
      labels = local.common_labels
    }
    spec = {
      policyName        = local.scientific_runtime_cache_writer_fence_name
      validationActions = ["Deny", "Audit"]
      matchResources = {
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = local.scientific_runtime_cache_consumer_namespaces
          }]
        }
      }
    }
  }

  field_manager {
    name            = "fs2-scientific-runtime-cache-security"
    force_conflicts = false
  }

  depends_on = [kubernetes_manifest.scientific_runtime_cache_writer_fence]
}

# Root cache ownership preparation has a dedicated tokenless identity with no
# RoleBinding. Model runtime service accounts cannot impersonate or invoke it;
# Terraform is the only owner of the bootstrap Jobs below.
resource "kubernetes_service_account_v1" "scientific_runtime_cache_bootstrap" {
  count = var.scientific_batch.runtime_cache.enabled ? 1 : 0

  metadata {
    name      = "fs2-scientific-cache-bootstrap"
    namespace = var.scientific_batch.namespace
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
    })
  }

  automount_service_account_token = false
}

resource "kubernetes_service_account_v1" "scientific_runtime_cache_bootstrap_additional" {
  for_each = var.scientific_batch.runtime_cache.enabled ? local.scientific_runtime_cache_additional_namespaces : toset([])

  metadata {
    name      = "fs2-scientific-cache-bootstrap"
    namespace = each.key
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component" = "scientific-runtime-cache-bootstrap"
    })
  }

  automount_service_account_token = false
}

# Prepare only the model-owned boundaries declared above. The root-capable
# container sees no credential, service-account token, network requirement or
# other writable volume. Its checked-in program refuses nested/traversing names
# and uses descriptor-relative, no-follow traversal. The dual-access phase
# preserves every byte, journals the former UID/GID/mode, moves the subtree to
# its signed tenant/model UID plus legacy group, and mirrors owner permissions
# to that group for rollback.
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
    owner_references {
      api_version          = "admissionregistration.k8s.io/v1"
      kind                 = "ValidatingAdmissionPolicy"
      name                 = local.scientific_runtime_cache_writer_fence_name
      uid                  = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid
      controller           = false
      block_owner_deletion = false
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
        service_account_name            = kubernetes_service_account_v1.scientific_runtime_cache_bootstrap[0].metadata[0].name
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
    kubernetes_service_account_v1.scientific_runtime_cache_bootstrap,
    kubernetes_manifest.scientific_runtime_cache_writer_fence_binding,
    terraform_data.scientific_artifacts_contract,
  ]
}

# Additional namespace-local claims receive the same bounded dual-access
# migration as the original claim. Each contract contains only the model-owned
# first-level boundaries consumed in that namespace.
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
    owner_references {
      api_version          = "admissionregistration.k8s.io/v1"
      kind                 = "ValidatingAdmissionPolicy"
      name                 = local.scientific_runtime_cache_writer_fence_name
      uid                  = var.scientific_batch.runtime_cache.migration_quiescence.admission_policy_uid
      controller           = false
      block_owner_deletion = false
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
        service_account_name            = kubernetes_service_account_v1.scientific_runtime_cache_bootstrap_additional[each.key].metadata[0].name
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
    kubernetes_service_account_v1.scientific_runtime_cache_bootstrap_additional,
    kubernetes_manifest.scientific_runtime_cache_writer_fence_binding,
    terraform_data.scientific_artifacts_contract,
  ]
}

# Publishes exactly what the control-plane chart receives, so the projection is
# assertable without standing up the whole stage. Everything here is non-secret.
resource "terraform_data" "scientific_artifacts_contract" {
  input = {
    enabled     = local.scientific_artifacts_enabled
    secret_name = local.scientific_artifacts_secret_name
    secret_key  = local.scientific_artifacts_secret_key
    namespace   = "fs2-system"
    bucket_name = try(var.scientific_artifacts.storage_contract.object_storage.name, null)
    object_key  = try(var.scientific_artifacts.storage_contract.layout.object_key, null)
    # Derived from the cloud-side key version alone, so a rotation is visible
    # without ever recording the secret.
    credential_revision   = local.scientific_artifacts_revision
    credential_generation = var.scientific_artifacts.credential_generation
    # A digest of the non-secret key identity, so the receipt shows that a
    # replaced key really does move the rollout identity.
    credential_identity_sha256 = local.scientific_artifacts_enabled ? sha256(local.scientific_artifacts_credential_identity) : null
    chart_values               = local.scientific_chart_overrides
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
      condition     = !var.scientific_batch.enabled || var.scientific_artifacts.enabled
      error_message = "staged scientific batch execution requires the dedicated artifact store; a batch cannot commit an immutable result manifest without it."
    }
    precondition {
      condition     = !var.scientific_batch.enabled || (local.scientific_image_supply_valid && local.scientific_image_authorizations_valid)
      error_message = "Every scientific stage, init, and companion image must be digest-pinned beneath the approved private registry and dereference an accepted independent scientific-image attestation."
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
      error_message = "A scientific runtime cache must be enabled exactly when the execution map consumes it, and every source binding must use the Terraform-owned claim at /cache."
    }
    precondition {
      condition = (
        !var.scientific_batch.runtime_cache.enabled || (
          local.scientific_runtime_cache_quiescence_valid &&
          length(local.scientific_runtime_cache_consumers) > 0 &&
          alltrue([
            for consumer in local.scientific_runtime_cache_consumers :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", consumer.workload_namespace)) &&
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", consumer.cache_sub_path)) &&
            consumer.cache_mount_path == local.scientific_runtime_cache_mount_path &&
            length(consumer.cache_paths) > 0 &&
            alltrue([
              for path in consumer.cache_paths :
              path == format("%s/%s", consumer.cache_mount_path, consumer.cache_sub_path) ||
              startswith(path, format("%s/%s/", consumer.cache_mount_path, consumer.cache_sub_path))
            ])
          ]) &&
          alltrue([
            for claim in local.scientific_runtime_cache_directory_claims :
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", claim.name)) &&
            can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$", claim.tenant_id)) &&
            can(regex("^[a-z0-9](?:[-a-z0-9.]{0,126}[a-z0-9])?$", claim.model_id)) &&
            try(claim.uid >= 1 && claim.uid <= 2147483647, false) &&
            try(claim.gid >= 1 && claim.gid <= 2147483647, false) &&
            try(claim.legacy_uid >= 1 && claim.legacy_uid <= 2147483647, false) &&
            try(claim.legacy_gid >= 1 && claim.legacy_gid <= 2147483647, false) &&
            claim.legacy_uid != claim.uid && claim.legacy_gid != claim.gid &&
            contains(["legacy-existing", "new-empty"], claim.origin) &&
            claim.activation_id == var.scientific_batch.runtime_cache.migration_quiescence.activation_id &&
            claim.boundary_sha256 == sha256(jsonencode({
              schema             = "fs2-serve.nebius.ai/scientific-runtime-cache-boundary/v1"
              tenant_id          = claim.tenant_id
              model_id           = claim.model_id
              workload_namespace = claim.workload_namespace
              directory          = claim.name
              run_as_user        = claim.uid
              run_as_group       = claim.gid
              legacy_uid         = claim.legacy_uid
              legacy_gid         = claim.legacy_gid
              origin             = claim.origin
              activation_id      = claim.activation_id
            })) &&
            contains(keys(local.verified_runtime_security_authorizations), claim.authorization_id) &&
            local.verified_runtime_security_authorizations[claim.authorization_id].kind == "cache-boundary" &&
            local.verified_runtime_security_authorizations[claim.authorization_id].model_id == claim.model_id &&
            local.verified_runtime_security_authorizations[claim.authorization_id].subject_sha256 == claim.boundary_sha256
          ]) &&
          alltrue([
            for consumer in local.scientific_runtime_cache_consumers :
            length([
              for claim in local.scientific_runtime_cache_directory_claims : claim
              if claim.model_id == consumer.model_id && claim.workload_namespace == consumer.workload_namespace
            ]) >= 1
          ]) &&
          length(local.scientific_runtime_cache_directory_claims) == length(distinct([
            for claim in local.scientific_runtime_cache_directory_claims : "${claim.tenant_id}|${claim.model_id}"
          ])) &&
          length(local.scientific_runtime_cache_directory_claims) == length(distinct([
            for claim in local.scientific_runtime_cache_directory_claims : "${claim.workload_namespace}|${claim.name}"
          ])) &&
          length(local.scientific_runtime_cache_directory_claims) == length(distinct([
            for claim in local.scientific_runtime_cache_directory_claims : tostring(claim.uid)
          ])) &&
          length(local.scientific_runtime_cache_directory_claims) == length(distinct([
            for claim in local.scientific_runtime_cache_directory_claims : tostring(claim.gid)
          ])) &&
          alltrue([
            for claims in values(local.scientific_runtime_cache_directory_claims_by_name) :
            length(claims) == 1
          ])
        )
      )
      error_message = "Every runtime-cache consumer requires accepted, unique tenant+model directory and UID/GID boundaries plus an exact reversible legacy/new origin contract."
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

locals {
  model_runtime_network_profile_label = "fs2-serve.nebius.ai/network-profile"
  model_runtime_network_class_label   = "fs2-serve.nebius.ai/network-workload-class"

  # These catalog models start exclusively from exact mounted content and keep
  # a true zero-egress contract. They deliberately do not join a DNS profile:
  # NetworkPolicy allows are additive, so a second DNS policy would reopen it.
  model_runtime_zero_egress_model_ids = toset([
    "glm-5-2-fp8",
    "nv-reason-cxr-3b",
    "qwen3-8b",
  ])
  model_runtime_base_profile_rows = [
    for model_id, route in local.selected_routes : {
      profile = format(
        "gateway-%s-tcp-%d-v1",
        contains(local.model_runtime_zero_egress_model_ids, model_id) ? "zero-egress" : "dns",
        route.service.port,
      )
      service_port = route.service.port
      egress_mode = (
        contains(local.model_runtime_zero_egress_model_ids, model_id) ? "none" : "dns"
      )
    }
  ]
  model_runtime_base_profile_groups = {
    for row in local.model_runtime_base_profile_rows : row.profile => row...
  }
  model_runtime_base_profiles = {
    for profile, rows in local.model_runtime_base_profile_groups : profile => rows[0]
  }

  # ModelExpress profiles are finite and qualification-owned. Arbitrarily
  # named customer Apps reuse the exact profile for their canonical runtime,
  # accelerator class, transport, port and device count; no policy is created
  # from an App UUID. The digest mirrors model_deployment.py.
  model_runtime_modelexpress_profile_rows = flatten([
    for model_id, binding in local.model_controller_modelexpress_bindings : [
      for pool_id in binding.poolRefs : {
        profile = "mx-${substr(sha256(jsonencode({
          acceleratorClass       = local.selected_queue_pools[pool_id].accelerator_class
          acceleratorsPerReplica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
          configDigest           = binding.configDigest
          nixlBackend            = binding.poolTransports[pool_id].nixlBackend
          servicePort            = local.selected_routes[model_id].service.port
        })), 0, 60)}"
        service_port             = local.selected_routes[model_id].service.port
        accelerators_per_replica = local.profile_contract.model_autoscaling_targets[model_id].gpu_count
        coordinator_port         = tonumber(element(split(":", binding.endpoint), 1))
        coordinator_type         = binding.coordinatorNetworkType
        coordinator_namespace    = binding.coordinatorNamespace
        coordinator_pod_labels   = binding.coordinatorPodLabels
        coordinator_cidrs        = binding.coordinatorCidrs
      }
    ]
  ])
  model_runtime_modelexpress_profile_groups = {
    for row in local.model_runtime_modelexpress_profile_rows : row.profile => row...
  }
  model_runtime_modelexpress_profiles = {
    for profile, rows in local.model_runtime_modelexpress_profile_groups : profile => rows[0]
  }
  model_namespace_support_profiles = {
    "acceptance-zero-egress-v1" = {
      egress_mode      = "none"
      workload_classes = ["acceptance"]
    }
    "cache-resident-zero-egress-v1" = {
      egress_mode      = "none"
      workload_classes = ["cache-keeper", "cache-resident"]
    }
    "job-internal-v1" = {
      egress_mode      = "internal"
      workload_classes = ["internal-job"]
    }
    "job-public-acquisition-v1" = {
      egress_mode      = "public-acquisition"
      workload_classes = ["public-acquisition"]
    }
  }
  model_runtime_serving_profile_names = sort(distinct(concat(
    keys(local.model_runtime_base_profiles),
    keys(local.model_runtime_modelexpress_profiles),
  )))
  model_runtime_profile_names = sort(distinct(concat(
    keys(local.model_runtime_base_profiles),
    keys(local.model_runtime_modelexpress_profiles),
    keys(local.model_namespace_support_profiles),
  )))
  model_runtime_allow_policy_names = [
    for profile in local.model_runtime_profile_names : "fs2-runtime-profile-${profile}"
  ]
  model_runtime_profiles_sha256 = sha256(jsonencode(local.model_runtime_profile_names))
  model_runtime_profile_admission_policy_names = [
    "fs2-model-network-profile-apps",
    "fs2-model-network-profile-cronjobs",
    "fs2-model-network-profile-jobs",
    "fs2-model-network-profile-jobsets",
    "fs2-model-network-profile-pods",
    "fs2-model-network-profile-replicationcontrollers",
  ]
  model_runtime_boundary_marker_admission_policy_name   = "fs2-model-network-boundary-marker"
  model_runtime_controller_freeze_admission_policy_name = "fs2-model-network-controller-freeze"
  model_runtime_helm_freeze_admission_policy_name       = "fs2-model-network-helm-freeze"
  model_runtime_lease_guard_admission_policy_name       = "fs2-model-network-transition-lease-guard"
  model_runtime_transition_guard_admission_policy_name  = "fs2-model-network-transition-guard"
  model_runtime_transition_lease_name                   = "fs2-model-network-transition"
  model_runtime_transition_lease_namespace              = "fs2-system"
  model_runtime_transition_writer_annotation            = "fs2-serve.nebius.ai/network-transition-writer"
  model_runtime_transition_holder_annotation            = "fs2-serve.nebius.ai/network-transition-holder"
  model_runtime_authorizer_writer                       = "fs2-model-network-authorizer"
  model_runtime_transition_writer                       = "fs2-model-network-transition"
  model_runtime_acquisition_writer                      = "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
  model_runtime_boundary_webhook_name                   = "fs2-model-network-boundary"
  model_runtime_controller_writer = (
    "system:serviceaccount:fs2-system:fs2-serve-control-plane-controller"
  )
  model_runtime_scientific_writer = (
    "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
  )
  model_runtime_jobset_writer = format(
    "system:serviceaccount:jobset-system:%s",
    try(
      data.terraform_remote_state.foundation.outputs.cluster_contract.jobset.controller_name,
      "fs2-${var.run_id}-jobset-controller",
    ),
  )
  model_runtime_controller_manager_writer = "system:kube-controller-manager"
  model_runtime_replicaset_writers = [
    local.model_runtime_controller_manager_writer,
    "system:serviceaccount:kube-system:deployment-controller",
  ]
  model_runtime_cronjob_writers = [
    local.model_runtime_controller_manager_writer,
    "system:serviceaccount:kube-system:cronjob-controller",
  ]
  model_runtime_pod_controller_writers = [
    local.model_runtime_controller_manager_writer,
    "system:serviceaccount:kube-system:daemon-set-controller",
    "system:serviceaccount:kube-system:job-controller",
    "system:serviceaccount:kube-system:replicaset-controller",
    "system:serviceaccount:kube-system:replication-controller",
    "system:serviceaccount:kube-system:statefulset-controller",
  ]
  model_runtime_active_transition_writer_expression = trimspace(<<-CEL
    request.userInfo.username == ${jsonencode(local.model_runtime_transition_writer)} &&
    has(params.spec.holderIdentity) &&
    params.spec.holderIdentity.matches('^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$') &&
    has(params.metadata.annotations) &&
    params.metadata.annotations[${jsonencode(local.model_runtime_transition_writer_annotation)}] == request.userInfo.username
  CEL
  )
  model_runtime_admission_policy_names = sort(concat(
    local.model_runtime_profile_admission_policy_names,
    [
      local.model_runtime_boundary_marker_admission_policy_name,
      local.model_runtime_controller_freeze_admission_policy_name,
      local.model_runtime_helm_freeze_admission_policy_name,
      local.model_runtime_lease_guard_admission_policy_name,
      local.model_runtime_transition_guard_admission_policy_name,
    ],
  ))
  model_runtime_controller_deployment_name = "fs2-serve-control-plane-model-controller"
  model_runtime_admission_specs = {
    apps = {
      name              = "fs2-model-network-profile-apps"
      api_groups        = ["apps"]
      api_versions      = ["v1"]
      resources         = ["deployments", "statefulsets", "daemonsets", "replicasets"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        (request.kind.kind == 'ReplicaSet' ?
          (request.userInfo.username in ${jsonencode(local.model_runtime_replicaset_writers)} &&
           has(object.metadata.ownerReferences) &&
           object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller == true && owner.kind == 'Deployment')) :
          (request.userInfo.username == ${jsonencode(local.model_runtime_controller_writer)} ||
           (${local.model_runtime_active_transition_writer_expression})))
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        ((object.metadata.labels['app.kubernetes.io/component'] == 'model-runtime' &&
          object.metadata.labels['${local.model_runtime_network_class_label}'] == 'runtime' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_serving_profile_names)}) ||
         (request.kind.kind == 'DaemonSet' &&
          object.metadata.labels['app.kubernetes.io/component'] == 'model-cache-keeper' &&
          object.metadata.labels['${local.model_runtime_network_class_label}'] == 'cache-keeper' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'cache-resident-zero-egress-v1')) &&
        has(object.spec.template.metadata.labels) &&
        object.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.spec.template.metadata.labels['app.kubernetes.io/component'] == object.metadata.labels['app.kubernetes.io/component'] &&
        object.spec.template.metadata.labels['${local.model_runtime_network_class_label}'] == object.metadata.labels['${local.model_runtime_network_class_label}'] &&
        object.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    jobs = {
      name              = "fs2-model-network-profile-jobs"
      api_groups        = ["batch"]
      api_versions      = ["v1"]
      resources         = ["jobs"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        (has(object.metadata.ownerReferences) &&
         object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller == true && owner.kind == 'JobSet') ?
          request.userInfo.username == ${jsonencode(local.model_runtime_jobset_writer)} :
         (has(object.metadata.ownerReferences) &&
          object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller == true && owner.kind == 'CronJob') ?
           request.userInfo.username in ${jsonencode(local.model_runtime_cronjob_writers)} :
          (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' ?
            request.userInfo.username == ${jsonencode(local.model_runtime_scientific_writer)} :
           (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'public-acquisition' ?
            request.userInfo.username == ${jsonencode(local.model_runtime_acquisition_writer)} :
            (${local.model_runtime_active_transition_writer_expression})))))
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        ((object.metadata.labels['${local.model_runtime_network_class_label}'] == 'acceptance' &&
          object.metadata.labels['app.kubernetes.io/component'] == 'acceptance' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'acceptance-zero-egress-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
          object.metadata.labels['fs2-serve.nebius.ai/job-kind'] in ['batch', 'evaluation'] &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'cache-resident' &&
          object.metadata.labels['fs2-serve.nebius.ai/job-kind'] == 'cache' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'cache-resident-zero-egress-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'public-acquisition' &&
          object.metadata.labels['fs2-serve.nebius.ai/job-kind'] == 'cache' &&
          object.metadata.labels['app.kubernetes.io/managed-by'] == 'fs2-serve-models' &&
          'fs2-serve.nebius.ai/model-id' in object.metadata.labels &&
          object.metadata.labels['fs2-serve.nebius.ai/model-id'].size() > 0 &&
          'fs2-serve.nebius.ai/operation-id' in object.metadata.labels &&
          object.metadata.labels['fs2-serve.nebius.ai/operation-id'].size() > 0 &&
          object.metadata.labels['fs2-serve.nebius.ai/acquisition-authority'] == 'catalog-qualified-v1' &&
          has(object.metadata.annotations) &&
          'fs2-serve.nebius.ai/acquisition-plan-sha256' in object.metadata.annotations &&
          object.metadata.annotations['fs2-serve.nebius.ai/acquisition-plan-sha256'].matches('^[a-f0-9]{64}$') &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-public-acquisition-v1')) &&
        has(object.spec.template.metadata.labels) &&
        object.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.spec.template.metadata.labels['${local.model_runtime_network_class_label}'] == object.metadata.labels['${local.model_runtime_network_class_label}'] &&
        object.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
        (object.metadata.labels['${local.model_runtime_network_class_label}'] != 'public-acquisition' ||
         (object.spec.template.metadata.labels['app.kubernetes.io/managed-by'] == 'fs2-serve-models' &&
          object.spec.template.metadata.labels['fs2-serve.nebius.ai/acquisition-authority'] == 'catalog-qualified-v1' &&
          has(object.spec.template.metadata.annotations) &&
          object.spec.template.metadata.annotations['fs2-serve.nebius.ai/acquisition-plan-sha256'] == object.metadata.annotations['fs2-serve.nebius.ai/acquisition-plan-sha256'] &&
          object.spec.template.spec.serviceAccountName == 'cache-service-account')) &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    jobsets = {
      name              = "fs2-model-network-profile-jobsets"
      api_groups        = ["jobset.x-k8s.io"]
      api_versions      = ["v1alpha2"]
      resources         = ["jobsets"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        request.userInfo.username == ${jsonencode(local.model_runtime_scientific_writer)} ||
        (${local.model_runtime_active_transition_writer_expression})
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1' &&
        object.spec.replicatedJobs.size() > 0 &&
        object.spec.replicatedJobs.all(job,
          has(job.template.metadata.labels) &&
          job.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
          job.template.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
          job.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
          has(job.template.spec.template.metadata.labels) &&
          job.template.spec.template.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
          job.template.spec.template.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
          job.template.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}']) &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    pods = {
      name              = "fs2-model-network-profile-pods"
      api_groups        = [""]
      api_versions      = ["v1"]
      resources         = ["pods"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        request.userInfo.username in ${jsonencode(local.model_runtime_pod_controller_writers)}
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        ((object.metadata.labels['${local.model_runtime_network_class_label}'] == 'runtime' &&
          object.metadata.labels['app.kubernetes.io/component'] == 'model-runtime' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_serving_profile_names)}) ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'cache-keeper' &&
          object.metadata.labels['app.kubernetes.io/component'] == 'model-cache-keeper' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'cache-resident-zero-egress-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'acceptance' &&
          object.metadata.labels['app.kubernetes.io/component'] == 'acceptance' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'acceptance-zero-egress-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'cache-resident' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'cache-resident-zero-egress-v1') ||
         (object.metadata.labels['${local.model_runtime_network_class_label}'] == 'public-acquisition' &&
          object.metadata.labels['app.kubernetes.io/managed-by'] == 'fs2-serve-models' &&
          'fs2-serve.nebius.ai/model-id' in object.metadata.labels &&
          object.metadata.labels['fs2-serve.nebius.ai/model-id'].size() > 0 &&
          'fs2-serve.nebius.ai/operation-id' in object.metadata.labels &&
          object.metadata.labels['fs2-serve.nebius.ai/operation-id'].size() > 0 &&
          object.metadata.labels['fs2-serve.nebius.ai/acquisition-authority'] == 'catalog-qualified-v1' &&
          object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-public-acquisition-v1' &&
          has(object.metadata.annotations) &&
          'fs2-serve.nebius.ai/acquisition-plan-sha256' in object.metadata.annotations &&
          object.metadata.annotations['fs2-serve.nebius.ai/acquisition-plan-sha256'].matches('^[a-f0-9]{64}$') &&
          object.spec.serviceAccountName == 'cache-service-account')) &&
        has(object.metadata.ownerReferences) &&
        object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller == true && owner.kind in ['Deployment', 'StatefulSet', 'DaemonSet', 'ReplicaSet', 'ReplicationController', 'Job']) &&
        ((request.operation == 'CREATE' && request.userInfo.username in ${jsonencode(local.model_runtime_pod_controller_writers)}) ||
         (request.operation == 'UPDATE' &&
          has(oldObject.metadata.labels) &&
          oldObject.metadata.labels['${local.model_runtime_network_class_label}'] == object.metadata.labels['${local.model_runtime_network_class_label}'] &&
          oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}']))
      CEL
    }
    cronjobs = {
      name              = "fs2-model-network-profile-cronjobs"
      api_groups        = ["batch"]
      api_versions      = ["v1"]
      resources         = ["cronjobs"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        (${local.model_runtime_active_transition_writer_expression})
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1' &&
        has(object.spec.jobTemplate.metadata.labels) &&
        object.spec.jobTemplate.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
        object.spec.jobTemplate.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1' &&
        has(object.spec.jobTemplate.spec.template.metadata.labels) &&
        object.spec.jobTemplate.spec.template.metadata.labels['${local.model_runtime_network_class_label}'] == 'internal-job' &&
        object.spec.jobTemplate.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == 'job-internal-v1' &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
    replicationcontrollers = {
      name              = "fs2-model-network-profile-replicationcontrollers"
      api_groups        = [""]
      api_versions      = ["v1"]
      resources         = ["replicationcontrollers"]
      writer_expression = <<-CEL
        request.operation != 'CREATE' ||
        (${local.model_runtime_active_transition_writer_expression})
      CEL
      expression        = <<-CEL
        has(object.metadata.labels) &&
        object.metadata.labels['app.kubernetes.io/part-of'] == 'fs2-serve' &&
        object.metadata.labels['app.kubernetes.io/component'] == 'model-runtime' &&
        object.metadata.labels['${local.model_runtime_network_class_label}'] == 'runtime' &&
        object.metadata.labels['${local.model_runtime_network_profile_label}'] in ${jsonencode(local.model_runtime_serving_profile_names)} &&
        has(object.spec.template.metadata.labels) &&
        object.spec.template.metadata.labels['app.kubernetes.io/component'] == 'model-runtime' &&
        object.spec.template.metadata.labels['${local.model_runtime_network_class_label}'] == 'runtime' &&
        object.spec.template.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'] &&
        (request.operation != 'UPDATE' || !has(oldObject.metadata.labels) || !('${local.model_runtime_network_profile_label}' in oldObject.metadata.labels) || oldObject.metadata.labels['${local.model_runtime_network_profile_label}'] == object.metadata.labels['${local.model_runtime_network_profile_label}'])
      CEL
    }
  }
  model_runtime_profile_admission_policy_specs = {
    for _, admission in local.model_runtime_admission_specs : admission.name => {
      failurePolicy = "Fail"
      paramKind = {
        apiVersion = "coordination.k8s.io/v1"
        kind       = "Lease"
      }
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = admission.api_groups
          apiVersions = admission.api_versions
          operations  = ["CREATE", "UPDATE"]
          resources   = admission.resources
          scope       = "Namespaced"
        }]
      }
      validations = [
        {
          expression = trimspace(admission.expression)
          message    = "fs2-models workloads and Pods require one immutable Terraform-owned network profile"
          reason     = "Forbidden"
        },
        {
          expression = trimspace(admission.writer_expression)
          message    = "only the workload's exact platform controller or the active network-transition writer may create this profiled object"
          reason     = "Forbidden"
        },
      ]
    }
  }
  model_runtime_marker_admission_policy_spec = {
    failurePolicy = "Fail"
    matchConstraints = {
      matchPolicy = "Equivalent"
      resourceRules = [{
        apiGroups   = [""]
        apiVersions = ["v1"]
        operations  = ["UPDATE", "DELETE"]
        resources   = ["configmaps"]
        scope       = "Namespaced"
      }]
    }
    validations = [{
      expression = "oldObject.metadata.name != 'fs2-runtime-network-policy-boundary-v2'"
      message    = "the fs2 model-network boundary marker is immutable and non-deletable"
      reason     = "Forbidden"
    }]
  }
  model_runtime_controller_freeze_admission_policy_spec = {
    failurePolicy = "Fail"
    matchConstraints = {
      matchPolicy = "Equivalent"
      resourceRules = [{
        apiGroups   = ["apps"]
        apiVersions = ["v1"]
        operations  = ["UPDATE", "DELETE"]
        resources   = ["deployments"]
        scope       = "Namespaced"
      }]
    }
    validations = [{
      expression = "oldObject.metadata.name != '${local.model_runtime_controller_deployment_name}'"
      message    = "the live model-controller is frozen while the fs2-models network boundary is armed"
      reason     = "Forbidden"
    }]
  }
  model_runtime_helm_freeze_admission_policy_spec = {
    failurePolicy = "Fail"
    paramKind = {
      apiVersion = "coordination.k8s.io/v1"
      kind       = "Lease"
    }
    matchConstraints = {
      matchPolicy = "Equivalent"
      resourceRules = [{
        apiGroups   = [""]
        apiVersions = ["v1"]
        operations  = ["CREATE", "UPDATE", "DELETE"]
        resources   = ["configmaps", "secrets"]
        scope       = "Namespaced"
      }]
    }
    validations = [{
      expression = trimspace(<<-CEL
        !has(params.spec.holderIdentity) ||
        params.spec.holderIdentity == '' ||
        (request.operation == 'CREATE' ?
          (!has(object.metadata.labels) ||
            !('owner' in object.metadata.labels) ||
            !('name' in object.metadata.labels) ||
            object.metadata.labels['owner'] != 'helm' ||
            object.metadata.labels['name'] != 'fs2-serve-control-plane') :
          (!has(oldObject.metadata.labels) ||
            !('owner' in oldObject.metadata.labels) ||
            !('name' in oldObject.metadata.labels) ||
            oldObject.metadata.labels['owner'] != 'helm' ||
            oldObject.metadata.labels['name'] != 'fs2-serve-control-plane'))
      CEL
      )
      message = "the fs2-serve-control-plane Helm release is frozen while a model-network transition Lease is active"
      reason  = "Forbidden"
    }]
  }
  model_runtime_lease_guard_admission_policy_spec = {
    failurePolicy = "Fail"
    matchConstraints = {
      matchPolicy = "Equivalent"
      resourceRules = [{
        apiGroups   = ["coordination.k8s.io"]
        apiVersions = ["v1"]
        operations  = ["UPDATE", "DELETE"]
        resources   = ["leases"]
        scope       = "Namespaced"
      }]
    }
    validations = [{
      expression = trimspace(<<-CEL
        oldObject.metadata.name != ${jsonencode(local.model_runtime_transition_lease_name)} ||
        (request.operation == 'UPDATE' &&
         request.userInfo.username == ${jsonencode(local.model_runtime_transition_writer)} &&
         has(oldObject.metadata.annotations) &&
         oldObject.metadata.annotations[${jsonencode(local.model_runtime_transition_writer_annotation)}] == request.userInfo.username &&
         (oldObject.spec.holderIdentity == '' ||
          (oldObject.spec.holderIdentity.matches('^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$') &&
           oldObject.metadata.annotations[${jsonencode(local.model_runtime_transition_holder_annotation)}] == oldObject.spec.holderIdentity)) &&
         has(object.metadata.annotations) &&
         object.metadata.annotations[${jsonencode(local.model_runtime_transition_writer_annotation)}] == request.userInfo.username &&
         (object.spec.holderIdentity == '' ||
          (object.spec.holderIdentity.matches('^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$') &&
           object.metadata.annotations[${jsonencode(local.model_runtime_transition_holder_annotation)}] == object.spec.holderIdentity)) &&
         ((oldObject.spec.holderIdentity == '' && object.spec.holderIdentity != '') ||
          (oldObject.spec.holderIdentity != '' && object.spec.holderIdentity == oldObject.spec.holderIdentity) ||
          (oldObject.spec.holderIdentity != '' && object.spec.holderIdentity == '')))
      CEL
      )
      message = "the retained model-network transition Lease may be acquired, renewed, or released only by its exact authenticated writer"
      reason  = "Forbidden"
    }]
  }
  model_runtime_transition_guard_resource_rules = [
    {
      apiGroups   = ["networking.k8s.io"]
      apiVersions = ["v1"]
      operations  = ["CREATE", "UPDATE", "DELETE"]
      resources   = ["networkpolicies"]
      scope       = "Namespaced"
    },
    {
      apiGroups   = [""]
      apiVersions = ["v1"]
      operations  = ["CREATE", "UPDATE", "DELETE"]
      resources   = ["configmaps"]
      scope       = "Namespaced"
    },
    {
      apiGroups   = ["admissionregistration.k8s.io"]
      apiVersions = ["v1"]
      operations  = ["CREATE", "UPDATE", "DELETE"]
      resources = [
        "validatingadmissionpolicies",
        "validatingadmissionpolicybindings",
        "validatingwebhookconfigurations",
      ]
      scope = "Cluster"
    },
  ]
  model_runtime_transition_guard_admission_policy_spec = {
    failurePolicy = "Fail"
    paramKind = {
      apiVersion = "coordination.k8s.io/v1"
      kind       = "Lease"
    }
    matchConstraints = {
      matchPolicy   = "Equivalent"
      resourceRules = local.model_runtime_transition_guard_resource_rules
    }
    variables = [
      {
        name       = "targetMetadata"
        expression = "request.operation == 'CREATE' ? object.metadata : oldObject.metadata"
      },
      {
        name = "protectedObject"
        expression = trimspace(<<-CEL
          (request.resource.group == 'networking.k8s.io' &&
           request.resource.resource == 'networkpolicies' &&
           request.namespace == 'fs2-models') ||
          (request.resource.group == '' &&
           request.resource.resource == 'configmaps' &&
           request.namespace == 'fs2-models' &&
           variables.targetMetadata.name == 'fs2-runtime-network-policy-boundary-v2') ||
          (request.resource.group == 'admissionregistration.k8s.io' &&
           request.resource.resource == 'validatingadmissionpolicies' &&
           variables.targetMetadata.name in ${jsonencode(local.model_runtime_admission_policy_names)}) ||
          (request.resource.group == 'admissionregistration.k8s.io' &&
           request.resource.resource == 'validatingadmissionpolicybindings' &&
           variables.targetMetadata.name in ${jsonencode(local.model_runtime_admission_binding_names)}) ||
          (request.resource.group == 'admissionregistration.k8s.io' &&
           request.resource.resource == 'validatingwebhookconfigurations' &&
           variables.targetMetadata.name == ${jsonencode(local.model_runtime_boundary_webhook_name)})
        CEL
        )
      },
    ]
    validations = [{
      expression = trimspace(<<-CEL
        !variables.protectedObject ||
        (request.userInfo.username == ${jsonencode(local.model_runtime_transition_writer)} &&
         has(params.spec.holderIdentity) &&
         params.spec.holderIdentity.matches('^[a-z][a-z0-9]{5,11}:[1-9][0-9]*:[a-f0-9]{32}$') &&
         has(params.metadata.annotations) &&
         params.metadata.annotations[${jsonencode(local.model_runtime_transition_writer_annotation)}] == request.userInfo.username &&
         (request.operation == 'DELETE' ||
          (has(variables.targetMetadata.annotations) &&
           variables.targetMetadata.annotations[${jsonencode(local.model_runtime_transition_holder_annotation)}] == params.spec.holderIdentity)))
      CEL
      )
      message = "fs2-models NetworkPolicies and Terraform-owned boundary admission objects require the exact active transition writer and Lease"
      reason  = "Forbidden"
    }]
  }
  model_runtime_admission_policy_specs = merge(
    local.model_runtime_profile_admission_policy_specs,
    {
      (local.model_runtime_boundary_marker_admission_policy_name)   = local.model_runtime_marker_admission_policy_spec
      (local.model_runtime_controller_freeze_admission_policy_name) = local.model_runtime_controller_freeze_admission_policy_spec
      (local.model_runtime_helm_freeze_admission_policy_name)       = local.model_runtime_helm_freeze_admission_policy_spec
      (local.model_runtime_lease_guard_admission_policy_name)       = local.model_runtime_lease_guard_admission_policy_spec
      (local.model_runtime_transition_guard_admission_policy_name)  = local.model_runtime_transition_guard_admission_policy_spec
    },
  )
  model_runtime_admission_binding_specs = merge(
    {
      for name in local.model_runtime_profile_admission_policy_names : "${name}-fs2-models" => {
        policyName        = name
        validationActions = ["Deny"]
        paramRef = {
          name                    = local.model_runtime_transition_lease_name
          namespace               = local.model_runtime_transition_lease_namespace
          parameterNotFoundAction = "Deny"
        }
        matchResources = {
          matchPolicy = "Equivalent"
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = "fs2-models" }
          }
        }
      }
    },
    {
      "${local.model_runtime_boundary_marker_admission_policy_name}-fs2-models" = {
        policyName        = local.model_runtime_boundary_marker_admission_policy_name
        validationActions = ["Deny"]
        matchResources = {
          matchPolicy = "Equivalent"
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = "fs2-models" }
          }
        }
      }
      "${local.model_runtime_controller_freeze_admission_policy_name}-fs2-system" = {
        policyName        = local.model_runtime_controller_freeze_admission_policy_name
        validationActions = ["Deny"]
        matchResources = {
          matchPolicy = "Equivalent"
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = "fs2-system" }
          }
        }
      }
      "${local.model_runtime_helm_freeze_admission_policy_name}-fs2-system" = {
        policyName        = local.model_runtime_helm_freeze_admission_policy_name
        validationActions = ["Deny"]
        paramRef = {
          name                    = local.model_runtime_transition_lease_name
          namespace               = local.model_runtime_transition_lease_namespace
          parameterNotFoundAction = "Allow"
        }
        matchResources = {
          matchPolicy = "Equivalent"
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = "fs2-system" }
          }
        }
      }
      "${local.model_runtime_lease_guard_admission_policy_name}-fs2-system" = {
        policyName        = local.model_runtime_lease_guard_admission_policy_name
        validationActions = ["Deny"]
        matchResources = {
          matchPolicy = "Equivalent"
          resourceRules = [{
            apiGroups   = ["coordination.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["UPDATE", "DELETE"]
            resources   = ["leases"]
            scope       = "Namespaced"
          }]
          namespaceSelector = {
            matchLabels = { "kubernetes.io/metadata.name" = local.model_runtime_transition_lease_namespace }
          }
        }
      }
      "${local.model_runtime_transition_guard_admission_policy_name}-global" = {
        policyName        = local.model_runtime_transition_guard_admission_policy_name
        validationActions = ["Deny"]
        paramRef = {
          name                    = local.model_runtime_transition_lease_name
          namespace               = local.model_runtime_transition_lease_namespace
          parameterNotFoundAction = "Deny"
        }
        matchResources = {
          matchPolicy   = "Equivalent"
          resourceRules = local.model_runtime_transition_guard_resource_rules
        }
      }
    },
  )
  model_runtime_admission_binding_names = sort(keys(local.model_runtime_admission_binding_specs))
  model_runtime_admission_policy_spec_sha256 = {
    for name, spec in local.model_runtime_admission_policy_specs : name => sha256(jsonencode(spec))
  }
  model_runtime_admission_binding_spec_sha256 = {
    for name, spec in local.model_runtime_admission_binding_specs : name => sha256(jsonencode(spec))
  }

  model_runtime_inventory_receipt_payload = var.model_runtime_network_policy.inventory_receipt == null ? null : {
    schema              = var.model_runtime_network_policy.inventory_receipt.schema
    cluster_id          = var.model_runtime_network_policy.inventory_receipt.cluster_id
    namespace           = var.model_runtime_network_policy.inventory_receipt.namespace
    captured_at         = var.model_runtime_network_policy.inventory_receipt.captured_at
    profiles_sha256     = var.model_runtime_network_policy.inventory_receipt.profiles_sha256
    resource_apis       = var.model_runtime_network_policy.inventory_receipt.resource_apis
    workloads           = var.model_runtime_network_policy.inventory_receipt.workloads
    pods                = var.model_runtime_network_policy.inventory_receipt.pods
    live_controller     = var.model_runtime_network_policy.inventory_receipt.live_controller
    transition_lock_uid = var.model_runtime_network_policy.inventory_receipt.transition_lock_uid
    admission_policies  = var.model_runtime_network_policy.inventory_receipt.admission_policies
    admission_bindings  = var.model_runtime_network_policy.inventory_receipt.admission_bindings
    admission_webhook   = var.model_runtime_network_policy.inventory_receipt.admission_webhook
  }
  model_runtime_deny_absent_receipt_payload = var.model_runtime_network_policy.deny_absent_receipt == null ? null : {
    schema                     = var.model_runtime_network_policy.deny_absent_receipt.schema
    cluster_id                 = var.model_runtime_network_policy.deny_absent_receipt.cluster_id
    namespace                  = var.model_runtime_network_policy.deny_absent_receipt.namespace
    captured_at                = var.model_runtime_network_policy.deny_absent_receipt.captured_at
    enforcement_payload_sha256 = var.model_runtime_network_policy.deny_absent_receipt.enforcement_payload_sha256
    profiles_sha256            = var.model_runtime_network_policy.deny_absent_receipt.profiles_sha256
    allow_policy_names         = var.model_runtime_network_policy.deny_absent_receipt.allow_policy_names
    default_deny_absent        = var.model_runtime_network_policy.deny_absent_receipt.default_deny_absent
  }

  live_model_network_policy_names = var.model_runtime_network_policy.phase == "rollback-helm" ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_policy_prepare_names = contains(["prepare", "inventory"], var.model_runtime_network_policy.phase) ? sort([
    for policy in coalesce(data.kubernetes_resources.model_runtime_network_policies[0].objects, []) : policy.metadata.name
  ]) : []
  live_model_network_enforcement_markers = contains(["prepare", "rollback-helm"], var.model_runtime_network_policy.phase) ? coalesce(data.kubernetes_resources.model_runtime_network_enforcement_markers[0].objects, []) : []
  model_runtime_boundary_marker_payload = {
    schema                = "fs2-serve.nebius.ai/model-runtime-network-boundary/v2"
    profiles_sha256       = local.model_runtime_profiles_sha256
    admission_policies    = local.model_runtime_admission_policy_names
    admission_bindings    = local.model_runtime_admission_binding_names
    controller_deployment = local.model_runtime_controller_deployment_name
  }
}

data "kubernetes_resources" "model_runtime_network_policies" {
  provider = kubernetes.network_boundary
  count = contains([
    "prepare",
    "inventory",
    "rollback-helm",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version = "networking.k8s.io/v1"
  kind        = "NetworkPolicy"
  namespace   = "fs2-models"
}

data "kubernetes_resources" "model_runtime_network_enforcement_markers" {
  provider = kubernetes.network_boundary
  count    = contains(["prepare", "rollback-helm"], var.model_runtime_network_policy.phase) ? 1 : 0

  api_version    = "v1"
  kind           = "ConfigMap"
  namespace      = "fs2-models"
  label_selector = "fs2-serve.nebius.ai/network-boundary-marker=true"
}

resource "terraform_data" "model_runtime_network_policy_transition" {
  input = {
    phase                         = var.model_runtime_network_policy.phase
    cluster_id                    = var.cluster_id
    namespace                     = "fs2-models"
    profiles                      = local.model_runtime_profile_names
    serving_profiles              = local.model_runtime_serving_profile_names
    profiles_sha256               = local.model_runtime_profiles_sha256
    allow_policy_names            = local.model_runtime_allow_policy_names
    admission_policy_names        = local.model_runtime_admission_policy_names
    admission_binding_names       = local.model_runtime_admission_binding_names
    admission_policy_spec_sha256  = local.model_runtime_admission_policy_spec_sha256
    admission_binding_spec_sha256 = local.model_runtime_admission_binding_spec_sha256
    controller_deployment_name    = local.model_runtime_controller_deployment_name
    transition_lock_name          = local.model_runtime_transition_lease_name
    transition_lock_namespace     = local.model_runtime_transition_lease_namespace
    transition_writer_username    = local.model_runtime_transition_writer
    boundary_webhook_name         = local.model_runtime_boundary_webhook_name
    control_plane_image           = var.control_plane_image
    inventory_receipt_sha256      = try(var.model_runtime_network_policy.inventory_receipt.payload_sha256, null)
    deny_absent_receipt_sha256    = try(var.model_runtime_network_policy.deny_absent_receipt.payload_sha256, null)
    default_deny_planned          = var.model_runtime_network_policy.phase == "enforce"
    helm_rollback_authorized      = var.model_runtime_network_policy.phase == "rollback-helm"
    deny_removal_apply_isolation  = var.model_runtime_network_policy.phase == "rollback-remove-deny"
  }

  lifecycle {
    precondition {
      condition = nonsensitive(var.model_network_boundary_kubeconfig_path) == "/var/run/fs2-network-boundary/credential-required" || (
        var.model_network_transition_lock_required &&
        var.model_network_transition_lock_identity != "" &&
        var.model_network_transition_writer_username == local.model_runtime_transition_writer
      )
      error_message = "Every model-network phase, including prepare, must run through inference-stack with separate authorizer/transition credentials while the random transition Lease holder is active."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "prepare" || (
        !contains(local.live_model_network_policy_prepare_names, "default-deny") &&
        length(local.live_model_network_enforcement_markers) == 0
      )
      error_message = "Prepare is initial-only and requires both default-deny and the armed boundary marker to be absent. Once inventory arms admission, refresh receipts in enforce instead of returning to prepare."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "inventory" || (
        !contains(local.live_model_network_policy_prepare_names, "default-deny")
      )
      error_message = "Inventory can arm admission only while fs2-models/default-deny remains absent."
    }

    precondition {
      condition = !contains([
        "enforce",
        "rollback-remove-deny",
        "rollback-helm",
        ], var.model_runtime_network_policy.phase) || try(
        var.model_runtime_network_policy.inventory_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-inventory/v5" &&
        var.model_runtime_network_policy.inventory_receipt.cluster_id == var.cluster_id &&
        var.model_runtime_network_policy.inventory_receipt.namespace == "fs2-models" &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.model_runtime_network_policy.inventory_receipt.captured_at)) &&
        var.model_runtime_network_policy.inventory_receipt.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        length(var.model_runtime_network_policy.inventory_receipt.workloads) > 0 &&
        var.model_runtime_network_policy.inventory_receipt.live_controller.deployment_name == local.model_runtime_controller_deployment_name &&
        var.model_runtime_network_policy.inventory_receipt.live_controller.image == "${var.control_plane_image.repository}@${var.control_plane_image.digest}" &&
        var.model_runtime_network_policy.inventory_receipt.transition_lock_uid != "" &&
        jsonencode(sort(keys(var.model_runtime_network_policy.inventory_receipt.admission_policies))) == jsonencode(local.model_runtime_admission_policy_names) &&
        jsonencode(sort(keys(var.model_runtime_network_policy.inventory_receipt.admission_bindings))) == jsonencode(local.model_runtime_admission_binding_names) &&
        alltrue([for name, policy in var.model_runtime_network_policy.inventory_receipt.admission_policies :
          policy.uid != "" &&
          policy.resource_version != "" &&
          policy.spec_sha256 == local.model_runtime_admission_policy_spec_sha256[name]
        ]) &&
        alltrue([for name, binding in var.model_runtime_network_policy.inventory_receipt.admission_bindings :
          binding.uid != "" &&
          binding.resource_version != "" &&
          binding.spec_sha256 == local.model_runtime_admission_binding_spec_sha256[name]
        ]) &&
        var.model_runtime_network_policy.inventory_receipt.admission_webhook.uid != "" &&
        var.model_runtime_network_policy.inventory_receipt.admission_webhook.resource_version != "" &&
        can(regex("^[a-f0-9]{64}$", var.model_runtime_network_policy.inventory_receipt.admission_webhook.spec_sha256)) &&
        var.model_runtime_network_policy.inventory_receipt.payload_sha256 == sha256(jsonencode(local.model_runtime_inventory_receipt_payload)),
        false,
      )
      error_message = "Enforcement and deny removal require a valid v5 workload/Pod receipt for this cluster, the live digest-pinned model-controller, exact policy/binding/webhook UIDs, resourceVersions and full semantics, namespace, and finite profile catalog. Expected payload digest: ${sha256(jsonencode(local.model_runtime_inventory_receipt_payload))}."
    }

    precondition {
      condition = var.model_runtime_network_policy.phase != "rollback-helm" || try(
        var.model_runtime_network_policy.deny_absent_receipt.schema == "fs2-serve.nebius.ai/model-runtime-network-deny-absent/v2" &&
        var.model_runtime_network_policy.deny_absent_receipt.cluster_id == var.cluster_id &&
        var.model_runtime_network_policy.deny_absent_receipt.namespace == "fs2-models" &&
        can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.model_runtime_network_policy.deny_absent_receipt.captured_at)) &&
        var.model_runtime_network_policy.deny_absent_receipt.enforcement_payload_sha256 == var.model_runtime_network_policy.inventory_receipt.payload_sha256 &&
        var.model_runtime_network_policy.deny_absent_receipt.profiles_sha256 == local.model_runtime_profiles_sha256 &&
        jsonencode(var.model_runtime_network_policy.deny_absent_receipt.allow_policy_names) == jsonencode(local.model_runtime_allow_policy_names) &&
        var.model_runtime_network_policy.deny_absent_receipt.default_deny_absent &&
        var.model_runtime_network_policy.deny_absent_receipt.payload_sha256 == sha256(jsonencode(local.model_runtime_deny_absent_receipt_payload)) &&
        !contains(local.live_model_network_policy_names, "default-deny") &&
        length(local.live_model_network_enforcement_markers) == 1 &&
        local.live_model_network_enforcement_markers[0].metadata.name == "fs2-runtime-network-policy-boundary-v2" &&
        try(local.live_model_network_enforcement_markers[0].immutable, false) &&
        local.live_model_network_enforcement_markers[0].data.payload_sha256 == sha256(jsonencode(local.model_runtime_boundary_marker_payload)) &&
        length(setsubtract(
          toset(local.model_runtime_allow_policy_names),
          toset(local.live_model_network_policy_names),
        )) == 0,
        false,
      )
      error_message = "Helm rollback is forbidden until a separate applied rollback-remove-deny phase has a valid receipt and live fs2-models inventory proves default-deny absent while finite allow profiles remain. Expected receipt digest: ${sha256(jsonencode(local.model_runtime_deny_absent_receipt_payload))}; live policies: ${jsonencode(local.live_model_network_policy_names)}."
    }
  }
}

# This fence stays active in every transition phase. Once installed, no new
# Pod-producing object or naked Pod can enter fs2-models without selecting one
# of the finite Terraform-owned profiles. That closes the plan/apply race while
# still allowing arbitrary customer App UUIDs to reuse an immutable profile.
resource "kubernetes_manifest" "model_runtime_network_profile_admission" {
  provider = kubernetes.network_boundary
  for_each = local.model_runtime_admission_specs

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = each.value.name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-profile-admission"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[each.value.name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-profile-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

resource "kubernetes_manifest" "model_runtime_network_profile_admission_binding" {
  provider = kubernetes.network_boundary
  for_each = var.model_runtime_network_policy.phase == "prepare" ? {} : local.model_runtime_admission_specs

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${each.value.name}-fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-profile-admission"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${each.value.name}-fs2-models"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-profile-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model,
    kubernetes_manifest.cold_start_keeper,
    kubernetes_manifest.kueue_admission_acceptance,
    kubernetes_manifest.model_runtime_network_profile_admission,
  ]
}

# The model controller legitimately manages ordinary ConfigMaps for dynamic
# Apps, so its namespaced ConfigMap verbs cannot be removed. Protect the stable
# transition marker at admission instead: after inventory starts, nobody can
# update or delete the exact marker, while all other ConfigMaps keep their
# existing behavior. The controller has no cluster-scoped admission-policy
# permissions and therefore cannot weaken this fence.
resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_boundary_marker_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-boundary-marker-admission"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[local.model_runtime_boundary_marker_admission_policy_name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-boundary-marker-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

resource "kubernetes_manifest" "model_runtime_network_boundary_marker_admission_binding" {
  provider = kubernetes.network_boundary
  count    = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_boundary_marker_admission_policy_name}-fs2-models"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-boundary-marker-admission"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${local.model_runtime_boundary_marker_admission_policy_name}-fs2-models"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-boundary-marker-admission"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.model_runtime_network_boundary_marker_admission,
  ]
}

# Once inventory begins, freeze the exact live model-controller Deployment.
# This is an API-server-enforced barrier, not a cooperative Helm convention:
# an out-of-band Helm upgrade cannot change the producer after the inventory
# verifier runs. The binding remains through deny removal and is removed only
# in rollback-helm, whose precondition proves default-deny is already absent.
resource "kubernetes_manifest" "model_runtime_network_controller_freeze_admission" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_controller_freeze_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-controller-freeze"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[local.model_runtime_controller_freeze_admission_policy_name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-controller-freeze"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

# Block Helm at its release-storage boundary before it can partially mutate
# chart resources. The exact model-controller Deployment freeze above remains
# the second API-server barrier if another client bypasses Helm storage.
resource "kubernetes_manifest" "model_runtime_network_helm_freeze_admission" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_helm_freeze_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-helm-freeze"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[local.model_runtime_helm_freeze_admission_policy_name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-helm-freeze"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

# The retained Lease has an API-server-enforced writer boundary. A client may
# acquire, renew, or release it only as the exact authenticated identity that
# the supported wrapper recorded. It can never delete the Lease or steal a
# non-empty holder identity. This closes the cooperative-lock gap without
# granting the model controller any admission-policy authority.
resource "kubernetes_manifest" "model_runtime_network_lease_guard_admission" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_lease_guard_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-transition-guard"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[local.model_runtime_lease_guard_admission_policy_name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-transition-guard"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

resource "kubernetes_manifest" "model_runtime_network_lease_guard_admission_binding" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_lease_guard_admission_policy_name}-fs2-system"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-transition-guard"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${local.model_runtime_lease_guard_admission_policy_name}-fs2-system"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-transition-guard"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.model_runtime_network_lease_guard_admission,
  ]
}

# This second guard protects the exact finite policies, default deny, marker,
# VAPs, and bindings. After inventory, every mutation must come from the exact
# authenticated Lease writer while its holder is active. CREATE/UPDATE also
# carries the current holder token, so an unrelated Helm/Terraform process that
# merely shares RBAC cannot race the verified transition.
resource "kubernetes_manifest" "model_runtime_network_transition_guard_admission" {
  provider = kubernetes.network_boundary
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = local.model_runtime_transition_guard_admission_policy_name
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-transition-guard"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_policy_specs[local.model_runtime_transition_guard_admission_policy_name]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-transition-guard"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [helm_release.control_plane]
}

resource "kubernetes_manifest" "model_runtime_network_controller_freeze_admission_binding" {
  provider = kubernetes.network_boundary
  count = contains([
    "inventory",
    "enforce",
    "rollback-remove-deny",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_controller_freeze_admission_policy_name}-fs2-system"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-controller-freeze"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${local.model_runtime_controller_freeze_admission_policy_name}-fs2-system"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-controller-freeze"
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model_runtime_network_controller_freeze_admission,
  ]
}

resource "kubernetes_manifest" "model_runtime_network_helm_freeze_admission_binding" {
  provider = kubernetes.network_boundary
  count = contains([
    "prepare",
    "inventory",
    "enforce",
    "rollback-remove-deny",
  ], var.model_runtime_network_policy.phase) ? 1 : 0

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_helm_freeze_admission_policy_name}-fs2-system"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-helm-freeze"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${local.model_runtime_helm_freeze_admission_policy_name}-fs2-system"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-helm-freeze"
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model_runtime_network_helm_freeze_admission,
  ]
}

resource "kubernetes_manifest" "model_runtime_network_transition_guard_admission_binding" {
  provider = kubernetes.network_boundary
  count    = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1

  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "${local.model_runtime_transition_guard_admission_policy_name}-global"
      labels = merge(local.common_labels, {
        "app.kubernetes.io/component"      = "namespace-network-boundary"
        "fs2-serve.nebius.ai/policy-owner" = "terraform-transition-guard"
      })
      annotations = {
        (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
        (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
      }
    }
    spec = local.model_runtime_admission_binding_specs["${local.model_runtime_transition_guard_admission_policy_name}-global"]
  }

  field_manager {
    force_conflicts = false
    name            = "fs2-model-network-transition-guard"
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    kubernetes_manifest.model_runtime_network_transition_guard_admission,
    kubernetes_manifest.model_runtime_network_lease_guard_admission_binding,
    kubernetes_manifest.model_runtime_network_profile_admission_binding,
    kubernetes_manifest.model_runtime_network_boundary_marker_admission_binding,
    kubernetes_manifest.model_runtime_network_controller_freeze_admission_binding,
    kubernetes_manifest.model_runtime_network_helm_freeze_admission_binding,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
    kubernetes_network_policy_v1.model_namespace_support_profile,
  ]
}

resource "kubernetes_config_map_v1" "model_runtime_network_enforcement" {
  provider = kubernetes.network_boundary
  count    = var.model_runtime_network_policy.phase == "prepare" ? 0 : 1

  metadata {
    name      = "fs2-runtime-network-policy-boundary-v2"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"                 = "namespace-network-boundary"
      "fs2-serve.nebius.ai/network-boundary-marker" = "true"
    })
    annotations = {
      (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
      (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
    }
  }

  data = {
    schema         = local.model_runtime_boundary_marker_payload.schema
    payload_sha256 = sha256(jsonencode(local.model_runtime_boundary_marker_payload))
  }

  immutable = true

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [
    terraform_data.model_runtime_network_policy_transition,
    kubernetes_manifest.model_runtime_network_profile_admission_binding,
    kubernetes_manifest.model_runtime_network_boundary_marker_admission_binding,
    kubernetes_manifest.model_runtime_network_controller_freeze_admission_binding,
    kubernetes_manifest.model_runtime_network_helm_freeze_admission_binding,
    kubernetes_manifest.model_runtime_network_transition_guard_admission_binding,
  ]
}

resource "kubernetes_network_policy_v1" "model_runtime_base_profile" {
  provider = kubernetes.network_boundary
  for_each = local.model_runtime_base_profiles

  depends_on = [helm_release.control_plane]

  metadata {
    name      = "fs2-runtime-profile-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"               = "model-runtime-network"
      (local.model_runtime_network_profile_label) = each.key
      "fs2-serve.nebius.ai/policy-owner"          = "terraform-profile"
    })
    annotations = {
      (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
      (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "model-runtime"
        (local.model_runtime_network_class_label)   = "runtime"
        (local.model_runtime_network_profile_label) = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(each.value.service_port)
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "dns" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
          }
          pod_selector {
            match_expressions {
              key      = "k8s-app"
              operator = "In"
              values   = ["coredns", "kube-dns"]
            }
          }
        }
        ports {
          protocol = "UDP"
          port     = "53"
        }
        ports {
          protocol = "TCP"
          port     = "53"
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "model_runtime_modelexpress_profile" {
  provider = kubernetes.network_boundary
  for_each = local.model_runtime_modelexpress_profiles

  depends_on = [helm_release.control_plane]

  metadata {
    name      = "fs2-runtime-profile-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"               = "model-runtime-network"
      (local.model_runtime_network_profile_label) = each.key
      "fs2-serve.nebius.ai/policy-owner"          = "terraform-profile"
    })
    annotations = {
      (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
      (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component"               = "model-runtime"
        (local.model_runtime_network_class_label)   = "runtime"
        (local.model_runtime_network_profile_label) = each.key
      }
    }
    policy_types = ["Ingress", "Egress"]

    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
        }
        pod_selector {
          match_labels = {
            "app.kubernetes.io/name"      = "fs2-serve-control-plane"
            "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
            "app.kubernetes.io/component" = "gateway"
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = tostring(each.value.service_port)
      }
    }

    ingress {
      from {
        pod_selector {
          match_labels = {
            (local.model_runtime_network_profile_label) = each.key
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = "5555"
        end_port = each.value.accelerators_per_replica > 1 ? 5555 + each.value.accelerators_per_replica - 1 : null
      }
      ports {
        protocol = "TCP"
        port     = "6555"
        end_port = each.value.accelerators_per_replica > 1 ? 6555 + each.value.accelerators_per_replica - 1 : null
      }
    }

    egress {
      to {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
        }
        pod_selector {
          match_expressions {
            key      = "k8s-app"
            operator = "In"
            values   = ["coredns", "kube-dns"]
          }
        }
      }
      ports {
        protocol = "UDP"
        port     = "53"
      }
      ports {
        protocol = "TCP"
        port     = "53"
      }
    }

    egress {
      to {
        pod_selector {
          match_labels = {
            (local.model_runtime_network_profile_label) = each.key
          }
        }
      }
      ports {
        protocol = "TCP"
        port     = "5555"
        end_port = each.value.accelerators_per_replica > 1 ? 5555 + each.value.accelerators_per_replica - 1 : null
      }
      ports {
        protocol = "TCP"
        port     = "6555"
        end_port = each.value.accelerators_per_replica > 1 ? 6555 + each.value.accelerators_per_replica - 1 : null
      }
    }

    dynamic "egress" {
      for_each = each.value.coordinator_type == "pod-selector" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = {
              "kubernetes.io/metadata.name" = each.value.coordinator_namespace
            }
          }
          pod_selector {
            match_labels = each.value.coordinator_pod_labels
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(each.value.coordinator_port)
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.coordinator_type == "ip-blocks" ? toset(each.value.coordinator_cidrs) : toset([])
      content {
        to {
          ip_block {
            cidr = egress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = tostring(each.value.coordinator_port)
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "model_namespace_support_profile" {
  provider = kubernetes.network_boundary
  for_each = local.model_namespace_support_profiles

  depends_on = [helm_release.control_plane]

  metadata {
    name      = "fs2-runtime-profile-${each.key}"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"               = "model-runtime-network"
      (local.model_runtime_network_profile_label) = each.key
      "fs2-serve.nebius.ai/policy-owner"          = "terraform-profile"
    })
    annotations = {
      (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
      (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
    }
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/part-of"                 = "fs2-serve"
        (local.model_runtime_network_profile_label) = each.key
      }
      match_expressions {
        key      = local.model_runtime_network_class_label
        operator = "In"
        values   = each.value.workload_classes
      }
    }
    policy_types = ["Ingress", "Egress"]

    dynamic "egress" {
      for_each = contains(["internal", "public-acquisition"], each.value.egress_mode) ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = { "kubernetes.io/metadata.name" = "kube-system" }
          }
          pod_selector {
            match_expressions {
              key      = "k8s-app"
              operator = "In"
              values   = ["coredns", "kube-dns"]
            }
          }
        }
        ports {
          protocol = "UDP"
          port     = "53"
        }
        ports {
          protocol = "TCP"
          port     = "53"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "internal" ? [true] : []
      content {
        to {
          namespace_selector {
            match_labels = { "kubernetes.io/metadata.name" = "fs2-system" }
          }
          pod_selector {
            match_labels = {
              "app.kubernetes.io/name"     = "fs2-serve-control-plane"
              "app.kubernetes.io/instance" = "fs2-serve-control-plane"
            }
          }
        }
        ports {
          protocol = "TCP"
          port     = "8080"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "internal" ? toset(var.scientific_artifacts.egress_cidrs) : toset([])
      content {
        to {
          ip_block {
            cidr = egress.value
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "public-acquisition" ? [true] : []
      content {
        to {
          ip_block {
            cidr = "0.0.0.0/0"
            except = [
              "0.0.0.0/8",
              "10.0.0.0/8",
              "100.64.0.0/10",
              "127.0.0.0/8",
              "169.254.0.0/16",
              "172.16.0.0/12",
              "192.0.0.0/24",
              "192.0.2.0/24",
              "192.168.0.0/16",
              "198.18.0.0/15",
              "198.51.100.0/24",
              "203.0.113.0/24",
              "224.0.0.0/4",
              "240.0.0.0/4",
            ]
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }

    dynamic "egress" {
      for_each = each.value.egress_mode == "public-acquisition" ? [true] : []
      content {
        to {
          ip_block {
            cidr = "::/0"
            except = [
              "::/128",
              "::1/128",
              "2001:db8::/32",
              "fc00::/7",
              "fe80::/10",
              "ff00::/8",
            ]
          }
        }
        ports {
          protocol = "TCP"
          port     = "443"
        }
      }
    }
  }
}

# The receipt is re-read from the live API during apply, after every known
# producer and the admission bindings exist. It runs on the first enforcement
# and whenever the receipt, release image, profile catalog, or verifier changes.
resource "terraform_data" "model_runtime_network_policy_apply_fence" {
  count = var.model_runtime_network_policy.phase == "enforce" ? 1 : 0

  triggers_replace = [
    var.model_runtime_network_policy.inventory_receipt.payload_sha256,
    local.model_runtime_profiles_sha256,
    var.control_plane_image.digest,
    filesha256("${path.module}/scripts/model_network_policy_transition.py"),
  ]

  provisioner "local-exec" {
    command = "python3 ${jsonencode("${path.module}/scripts/model_network_policy_transition.py")} verify-enforce --contract-json-env FS2_NETWORK_TRANSITION_JSON --receipt-json-env FS2_NETWORK_RECEIPT_JSON --kubeconfig ${jsonencode(pathexpand(var.kubeconfig_path))} --context ${jsonencode(var.kube_context)}"
    environment = {
      FS2_NETWORK_TRANSITION_JSON          = jsonencode(terraform_data.model_runtime_network_policy_transition.output)
      FS2_NETWORK_RECEIPT_JSON             = jsonencode(var.model_runtime_network_policy.inventory_receipt)
      FS2_NETWORK_TRANSITION_LOCK_IDENTITY = var.model_network_transition_lock_identity
    }
  }

  depends_on = [
    helm_release.control_plane,
    kubernetes_manifest.model,
    kubernetes_manifest.cold_start_keeper,
    kubernetes_manifest.kueue_admission_acceptance,
    kubernetes_manifest.model_runtime_network_profile_admission_binding,
    kubernetes_manifest.model_runtime_network_controller_freeze_admission_binding,
    kubernetes_manifest.model_runtime_network_helm_freeze_admission_binding,
    kubernetes_manifest.model_runtime_network_transition_guard_admission_binding,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
    kubernetes_network_policy_v1.model_namespace_support_profile,
  ]
}

resource "kubernetes_network_policy_v1" "model_namespace_default_deny" {
  provider = kubernetes.network_boundary
  count    = var.model_runtime_network_policy.phase == "enforce" ? 1 : 0

  metadata {
    name      = "default-deny"
    namespace = "fs2-models"
    labels = merge(local.common_labels, {
      "app.kubernetes.io/component"      = "namespace-network-boundary"
      "fs2-serve.nebius.ai/policy-owner" = "terraform-default-deny"
    })
    annotations = {
      (local.model_runtime_transition_writer_annotation) = local.model_runtime_transition_writer
      (local.model_runtime_transition_holder_annotation) = var.model_network_transition_lock_identity
    }
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress", "Egress"]
  }

  # Every finite allow profile exists before the namespace closes. Rollback
  # destroys the deny before removing either class of allow profile.
  depends_on = [
    terraform_data.model_runtime_network_policy_apply_fence,
    kubernetes_config_map_v1.model_runtime_network_enforcement,
    kubernetes_manifest.model_runtime_network_transition_guard_admission_binding,
    kubernetes_network_policy_v1.model_runtime_base_profile,
    kubernetes_network_policy_v1.model_runtime_modelexpress_profile,
    kubernetes_network_policy_v1.model_namespace_support_profile,
  ]
}

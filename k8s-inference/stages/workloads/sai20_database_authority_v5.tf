variable "sai20_database_authority_v5" {
  description = "Paths to the externally enrolled SAI-20 v5 successor bundle and the signed apply-time provider observer."
  type = object({
    successor_bundle_path       = string
    provider_group_observer_path = string
  })
  nullable = false

  validation {
    condition = (
      startswith(var.sai20_database_authority_v5.successor_bundle_path, "/") &&
      !strcontains(var.sai20_database_authority_v5.successor_bundle_path, "..") &&
      startswith(var.sai20_database_authority_v5.provider_group_observer_path, "/") &&
      !strcontains(var.sai20_database_authority_v5.provider_group_observer_path, "..")
    )
    error_message = "SAI-20 v5 requires absolute successor-bundle and provider-observer paths without parent traversal."
  }
}

locals {
  sai20_authority_v5_enrollment_authorities_path = abspath(
    "${path.module}/../../security/sai20/enrollment-authorities-v1.json"
  )
  sai20_authority_v5_root_enrollment_receipts_path = abspath(
    "${path.module}/../../security/sai20/root-enrollment-receipts-v1.json"
  )
  sai20_authority_v5_bootstrap_guard_contract_path = abspath(
    "${path.module}/contracts/sai20-bootstrap-guard-v5.json"
  )
  sai20_authority_v5_common_query = merge(local.sai20_authority_v4_common_query, {
    successor_bundle_path                     = var.sai20_database_authority_v5.successor_bundle_path
    enrollment_authorities_path               = local.sai20_authority_v5_enrollment_authorities_path
    expected_enrollment_authorities_sha256     = filesha256(local.sai20_authority_v5_enrollment_authorities_path)
    root_enrollment_receipts_path              = local.sai20_authority_v5_root_enrollment_receipts_path
    expected_root_enrollment_receipts_sha256   = filesha256(local.sai20_authority_v5_root_enrollment_receipts_path)
    bootstrap_guard_contract_path              = local.sai20_authority_v5_bootstrap_guard_contract_path
    expected_bootstrap_guard_contract_sha256   = filesha256(local.sai20_authority_v5_bootstrap_guard_contract_path)
  })
}

# The empty source-owned enrollment registries deliberately make this gate
# fail closed. Activation requires a later independently reviewed root
# ceremony whose detached signature chains to a strict-ancestor external
# Platform Security authority; the v4 self-asserted acceptance is insufficient.
data "external" "sai20_database_authority_v5_plan" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v5.py"),
  ]

  query = merge(local.sai20_authority_v5_common_query, {
    mode = "plan"
  })
}

resource "terraform_data" "sai20_database_authority_v5_plan" {
  input = data.external.sai20_database_authority_v5_plan.result

  lifecycle {
    precondition {
      condition = try(
        data.external.sai20_database_authority_v5_plan.result.verified == "true" &&
        data.external.sai20_database_authority_v5_plan.result.successor_verified == "true" &&
        data.external.sai20_database_authority_v5_plan.result.identity_reobserved == "false" &&
        data.external.sai20_database_authority_v5_plan.result.bootstrap_reobserved == "false" &&
        data.external.sai20_database_authority_v5_plan.result.provider_group_reobserved == "false" &&
        data.external.sai20_database_authority_v5_plan.result.successor_source_exact_reobserved == "false" &&
        contains(["INITIAL", "RENEWAL"], data.external.sai20_database_authority_v5_plan.result.successor_transition_mode),
        false,
      )
      error_message = "SAI-20 v5 planning requires externally enrolled roots and an exact dual-signed successor envelope."
    }
  }
}

resource "terraform_data" "sai20_database_authority_v5_identity_nonce" {
  input = {
    nonce                   = timestamp()
    successor_bundle_sha256 = terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256
  }
}

data "external" "sai20_database_authority_v5_identity" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v5.py"),
  ]

  query = merge(local.sai20_authority_v5_common_query, {
    mode                         = "identity"
    kubeconfig_path              = var.kubeconfig_path
    kube_context                 = var.kube_context
    kubectl_path                 = var.sai20_database_authority_v4.kubectl_path
    provider_group_observer_path = var.sai20_database_authority_v5.provider_group_observer_path
    apply_nonce                  = terraform_data.sai20_database_authority_v5_identity_nonce.output.nonce
  })
}

resource "terraform_data" "sai20_database_authority_v5_identity" {
  input = data.external.sai20_database_authority_v5_identity.result

  lifecycle {
    precondition {
      condition = try(
        data.external.sai20_database_authority_v5_identity.result.verified == "true" &&
        data.external.sai20_database_authority_v5_identity.result.successor_verified == "true" &&
        data.external.sai20_database_authority_v5_identity.result.identity_reobserved == "true" &&
        data.external.sai20_database_authority_v5_identity.result.bootstrap_reobserved == "true" &&
        data.external.sai20_database_authority_v5_identity.result.provider_group_reobserved == "false" &&
        data.external.sai20_database_authority_v5_identity.result.successor_source_exact_reobserved == "false" &&
        data.external.sai20_database_authority_v5_identity.result.apply_nonce == terraform_data.sai20_database_authority_v5_identity_nonce.output.nonce &&
        data.external.sai20_database_authority_v5_identity.result.successor_bundle_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.bootstrap_guard_sha256 == terraform_data.sai20_database_authority_v5_plan.output.bootstrap_guard_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.namespace_names_json == terraform_data.sai20_database_authority_v5_plan.output.namespace_names_json &&
        data.external.sai20_database_authority_v5_identity.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.service_account_names_json == terraform_data.sai20_database_authority_v5_plan.output.service_account_names_json &&
        data.external.sai20_database_authority_v5_identity.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.secret_names_json == terraform_data.sai20_database_authority_v5_plan.output.secret_names_json &&
        data.external.sai20_database_authority_v5_identity.result.successor_transition_mode == terraform_data.sai20_database_authority_v5_plan.output.successor_transition_mode &&
        data.external.sai20_database_authority_v5_identity.result.successor_old_objects_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_old_objects_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.successor_planned_objects_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.executor_uid == terraform_data.sai20_database_authority_v5_plan.output.executor_uid &&
        data.external.sai20_database_authority_v5_identity.result.principal_identities_json == terraform_data.sai20_database_authority_v5_plan.output.principal_identities_json &&
        data.external.sai20_database_authority_v5_identity.result.rollout_lineages_json == terraform_data.sai20_database_authority_v5_plan.output.rollout_lineages_json &&
        data.external.sai20_database_authority_v5_identity.result.source_commit == terraform_data.sai20_database_authority_v5_plan.output.source_commit &&
        data.external.sai20_database_authority_v5_identity.result.source_tree == terraform_data.sai20_database_authority_v5_plan.output.source_tree,
        false,
      )
      error_message = "SAI-20 v5 bootstrap requires the exact signed executor and a pre-existing source-exact guard policy plus active binding."
    }
  }
}

locals {
  sai20_authority_v5_peer_parents = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.authorized_peer_parents_json
  )
  sai20_authority_v5_cnpg_controller_identities = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.cnpg_controller_identities_json
  )
  sai20_authority_v5_rollout_lineages = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.rollout_lineages_json
  )
  sai20_authority_v5_cnpg_controller_cel = join(" || ", [
    for principal in local.sai20_authority_v5_cnpg_controller_identities : format(
      "(has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)))",
      jsonencode(principal.uid),
      jsonencode(principal.username),
      length(principal.groups),
      jsonencode(principal.groups),
      jsonencode(principal.extra),
      jsonencode(principal.extra),
    )
  ])
  sai20_authority_v5_peer_parent_child_resource = {
    deployments            = "replicasets"
    statefulsets            = "pods"
    daemonsets              = "pods"
    replicasets             = "pods"
    jobs                    = "pods"
    cronjobs                = "jobs"
    replicationcontrollers = "pods"
  }
  sai20_authority_v5_exact_peer_parent_cel = join(" || ", [
    for parent in local.sai20_authority_v5_peer_parents : format(
      "(request.namespace == %s && request.resource.resource == %s && request.userInfo.username == %s && has(variables.targetObject.metadata.ownerReferences) && variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller && owner.apiVersion == %s && owner.kind == %s && owner.name == %s && string(owner.uid) == %s))",
      jsonencode(parent.namespace),
      jsonencode(local.sai20_authority_v5_peer_parent_child_resource[parent.resource]),
      jsonencode(parent.controller_username),
      jsonencode(parent.api_version),
      jsonencode(parent.kind),
      jsonencode(parent.name),
      jsonencode(parent.uid),
    )
  ])
  sai20_authority_v5_exact_operator_object_cel = join(" || ", [
    for parent in local.sai20_authority_v5_peer_parents : format(
      "(request.namespace == %s && request.resource.resource == %s && request.userInfo.username == %s && variables.targetObject.metadata.name == %s && string(variables.targetObject.metadata.uid) == %s)",
      jsonencode(parent.namespace),
      jsonencode(parent.resource),
      jsonencode(parent.controller_username),
      jsonencode(parent.name),
      jsonencode(parent.uid),
    )
  ])
  sai20_authority_v5_rollout_child_cel = join(" || ", [
    for lineage in local.sai20_authority_v5_rollout_lineages : format(
      "(request.operation == 'CREATE' && request.namespace == %s && request.resource.resource == 'replicasets' && request.userInfo.username == %s && has(variables.targetObject.spec.replicas) && variables.targetObject.spec.replicas == 0 && %s in variables.effectivePodLabels && variables.effectivePodLabels[%s] == %s && has(variables.targetObject.metadata.ownerReferences) && variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller && owner.apiVersion == %s && owner.kind == %s && owner.name == %s && string(owner.uid) == %s))",
      jsonencode(lineage.namespace),
      jsonencode(lineage.controller_username),
      jsonencode("security.fs2.nebius.ai/sai20-rollout-lineage"),
      jsonencode("security.fs2.nebius.ai/sai20-rollout-lineage"),
      jsonencode(lineage.lineage),
      jsonencode(lineage.api_version),
      jsonencode(lineage.kind),
      jsonencode(lineage.name),
      jsonencode(lineage.uid),
    )
  ])
  sai20_authority_v5_known_rollout_lineage_cel = join(" || ", [
    for lineage in local.sai20_authority_v5_rollout_lineages : format(
      "(%s in variables.effectivePodLabels && variables.effectivePodLabels[%s] == %s)",
      jsonencode("security.fs2.nebius.ai/sai20-rollout-lineage"),
      jsonencode("security.fs2.nebius.ai/sai20-rollout-lineage"),
      jsonencode(lineage.lineage),
    )
  ])
}

# NetworkPolicy label peers are identities only because this policy binds the
# exact pre-existing workload inventory and protects all future label-bearing
# Pods and controller templates in fs2-data and cnpg-system. Direct Pods and
# spoofed ownerReferences do not satisfy either exact-owner branch.
resource "kubernetes_manifest" "sai20_database_peer_identity_v5" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-peer-identity-v5"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v5"
      })
      annotations = {
        "security.fs2.nebius.ai/successor-bundle-sha256" = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
        "security.fs2.nebius.ai/peer-inventory-sha256"   = terraform_data.sai20_database_authority_v5_identity.output.peer_workload_inventory_sha256
        "security.fs2.nebius.ai/bootstrap-guard-sha256"  = terraform_data.sai20_database_authority_v5_identity.output.bootstrap_guard_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [
          {
            apiGroups   = [""]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["pods", "replicationcontrollers"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["deployments", "statefulsets", "daemonsets", "replicasets"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["jobs", "cronjobs"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["postgresql.cnpg.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["clusters"]
            scope       = "Namespaced"
          },
        ]
      }
      variables = [
        {
          name       = "targetObject"
          expression = "request.operation == 'DELETE' ? oldObject : object"
        },
        {
          name = "effectivePodLabels"
          expression = join(" ", [
            "request.resource.resource == 'clusters' ? {} :",
            "request.resource.resource == 'pods' ? (has(variables.targetObject.metadata.labels) ? variables.targetObject.metadata.labels : {}) :",
            "request.resource.resource == 'cronjobs' ? (has(variables.targetObject.spec.jobTemplate.spec.template.metadata.labels) ? variables.targetObject.spec.jobTemplate.spec.template.metadata.labels : {}) :",
            "has(variables.targetObject.spec.template.metadata.labels) ? variables.targetObject.spec.template.metadata.labels : {}",
          ])
        },
        {
          name = "oldEffectivePodLabels"
          expression = join(" ", [
            "request.operation != 'UPDATE' ? {} :",
            "request.resource.resource == 'clusters' ? {} :",
            "request.resource.resource == 'pods' ? (has(oldObject.metadata.labels) ? oldObject.metadata.labels : {}) :",
            "request.resource.resource == 'cronjobs' ? (has(oldObject.spec.jobTemplate.spec.template.metadata.labels) ? oldObject.spec.jobTemplate.spec.template.metadata.labels : {}) :",
            "has(oldObject.spec.template.metadata.labels) ? oldObject.spec.template.metadata.labels : {}",
          ])
        },
        {
          name = "databasePeer"
          expression = join(" ", [
            "request.namespace == 'fs2-data' &&",
            "'cnpg.io/cluster' in variables.effectivePodLabels &&",
            "variables.effectivePodLabels['cnpg.io/cluster'] == 'fs2-control-db'",
          ])
        },
        {
          name = "operatorPeer"
          expression = join(" ", [
            "request.namespace == 'cnpg-system' &&",
            "'app.kubernetes.io/name' in variables.effectivePodLabels &&",
            "variables.effectivePodLabels['app.kubernetes.io/name'] == 'cloudnative-pg'",
          ])
        },
        {
          name = "oldDatabasePeer"
          expression = join(" ", [
            "request.operation == 'UPDATE' && request.namespace == 'fs2-data' &&",
            "'cnpg.io/cluster' in variables.oldEffectivePodLabels &&",
            "variables.oldEffectivePodLabels['cnpg.io/cluster'] == 'fs2-control-db'",
          ])
        },
        {
          name = "oldOperatorPeer"
          expression = join(" ", [
            "request.operation == 'UPDATE' && request.namespace == 'cnpg-system' &&",
            "'app.kubernetes.io/name' in variables.oldEffectivePodLabels &&",
            "variables.oldEffectivePodLabels['app.kubernetes.io/name'] == 'cloudnative-pg'",
          ])
        },
        {
          name = "cnpgClusterObject"
          expression = join(" ", [
            "request.namespace == 'fs2-data' && request.resource.resource == 'clusters' &&",
            "variables.targetObject.metadata.name == 'fs2-control-db'",
          ])
        },
        {
          name = "custodian"
          expression = format(
            "has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s))",
            jsonencode(terraform_data.sai20_database_authority_v5_identity.output.executor_uid),
            jsonencode(terraform_data.sai20_database_authority_v5_identity.output.executor_username),
            length(jsondecode(terraform_data.sai20_database_authority_v5_identity.output.executor_groups_json)),
            terraform_data.sai20_database_authority_v5_identity.output.executor_groups_json,
            terraform_data.sai20_database_authority_v5_identity.output.executor_extra_json,
            terraform_data.sai20_database_authority_v5_identity.output.executor_extra_json,
          )
        },
        {
          name       = "controllerIdentity"
          expression = local.sai20_authority_v5_cnpg_controller_cel
        },
        {
          name       = "exactOperatorParent"
          expression = local.sai20_authority_v5_exact_peer_parent_cel
        },
        {
          name       = "exactSignedOperatorObject"
          expression = local.sai20_authority_v5_exact_operator_object_cel
        },
        {
          name       = "stagedRolloutReplicaSet"
          expression = local.sai20_authority_v5_rollout_child_cel
        },
        {
          name       = "knownRolloutLineage"
          expression = local.sai20_authority_v5_known_rollout_lineage_cel
        },
        {
          name = "exactCnpgClusterOwner"
          expression = join(" ", [
            "request.namespace == 'fs2-data' && request.resource.resource == 'pods' && variables.controllerIdentity &&",
            "has(variables.targetObject.metadata.ownerReferences) &&",
            "variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller &&",
            "owner.apiVersion == 'postgresql.cnpg.io/v1' && owner.kind == 'Cluster' &&",
            "owner.name == 'fs2-control-db' && string(owner.uid) == '${terraform_data.sai20_database_authority_v5_identity.output.cnpg_cluster_uid}')",
          ])
        },
        {
          name = "controllerOwnedOperatorChild"
          expression = join(" ", [
            "variables.controllerIdentity && ((request.resource.resource == 'pods' && variables.exactOperatorParent) ||",
            "(request.resource.resource == 'replicasets' && ((request.operation == 'CREATE' && variables.stagedRolloutReplicaSet) ||",
            "(request.operation != 'CREATE' && variables.exactSignedOperatorObject))))",
          ])
        },
      ]
      validations = [
        {
          expression = "!(variables.databasePeer || variables.operatorPeer) || variables.custodian || (variables.databasePeer && variables.exactCnpgClusterOwner) || (variables.operatorPeer && variables.controllerOwnedOperatorChild)"
          message    = "database and CNPG operator peers require the exact custodian, an exact signed live owner chain, or a zero-replica staged rollout root"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.operatorPeer || variables.knownRolloutLineage"
          message    = "every CNPG operator peer must retain a source-derived Deployment rollout lineage"
          reason     = "Forbidden"
        },
        {
          expression = "request.operation != 'UPDATE' || !(variables.oldDatabasePeer || variables.oldOperatorPeer) || variables.custodian || variables.databasePeer || variables.operatorPeer"
          message    = "removing a protected database or CNPG peer label requires the exact externally enrolled custodian"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.cnpgClusterObject || (request.operation != 'DELETE' && variables.custodian)"
          message    = "the exact live CNPG database owner is non-deletable and mutable only by the externally enrolled custodian"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v4]
}

resource "kubernetes_manifest" "sai20_database_peer_identity_binding_v5" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-peer-identity-binding-v5"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v5"
      })
      annotations = {
        "security.fs2.nebius.ai/successor-bundle-sha256" = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
        "security.fs2.nebius.ai/bootstrap-guard-sha256"  = terraform_data.sai20_database_authority_v5_identity.output.bootstrap_guard_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_peer_identity_v5.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = ["cnpg-system", "fs2-data"]
          }]
        }
      }
    }
  }
}

resource "terraform_data" "sai20_database_authority_v5_apply_nonce" {
  input = {
    nonce                   = timestamp()
    successor_bundle_sha256 = terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256
  }

  depends_on = [
    kubernetes_manifest.sai20_database_policy_freeze_v4,
    kubernetes_manifest.sai20_database_policy_freeze_binding_v4,
    kubernetes_manifest.sai20_database_exact_owner_v4,
    kubernetes_manifest.sai20_database_exact_owner_binding_v4,
    kubernetes_manifest.sai20_database_peer_identity_v5,
    kubernetes_manifest.sai20_database_peer_identity_binding_v5,
  ]
}

data "external" "sai20_database_authority_v5_apply" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v5.py"),
  ]

  query = merge(local.sai20_authority_v5_common_query, {
    mode                         = "apply"
    kubeconfig_path              = var.kubeconfig_path
    kube_context                 = var.kube_context
    kubectl_path                 = var.sai20_database_authority_v4.kubectl_path
    provider_group_observer_path = var.sai20_database_authority_v5.provider_group_observer_path
    apply_nonce                  = terraform_data.sai20_database_authority_v5_apply_nonce.output.nonce
    expected_successor_admission_objects_json = jsonencode([
      kubernetes_manifest.sai20_database_authority_object_custody_v4.manifest,
      kubernetes_manifest.sai20_database_authority_object_custody_binding_v4.manifest,
      kubernetes_manifest.sai20_database_policy_freeze_v4.manifest,
      kubernetes_manifest.sai20_database_policy_freeze_binding_v4.manifest,
      kubernetes_manifest.sai20_database_exact_owner_v4.manifest,
      kubernetes_manifest.sai20_database_exact_owner_binding_v4.manifest,
      kubernetes_manifest.sai20_database_peer_identity_v5.manifest,
      kubernetes_manifest.sai20_database_peer_identity_binding_v5.manifest,
    ])
  })
}

resource "terraform_data" "sai20_database_authority_v5_apply" {
  input = data.external.sai20_database_authority_v5_apply.result

  lifecycle {
    precondition {
      condition = try(
        data.external.sai20_database_authority_v5_apply.result.verified == "true" &&
        data.external.sai20_database_authority_v5_apply.result.successor_verified == "true" &&
        data.external.sai20_database_authority_v5_apply.result.identity_reobserved == "true" &&
        data.external.sai20_database_authority_v5_apply.result.apply_reobserved == "true" &&
        data.external.sai20_database_authority_v5_apply.result.bootstrap_reobserved == "true" &&
        data.external.sai20_database_authority_v5_apply.result.provider_group_reobserved == "true" &&
        data.external.sai20_database_authority_v5_apply.result.successor_source_exact_reobserved == "true" &&
        data.external.sai20_database_authority_v5_apply.result.apply_nonce == terraform_data.sai20_database_authority_v5_apply_nonce.output.nonce &&
        data.external.sai20_database_authority_v5_apply.result.successor_bundle_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.bootstrap_guard_sha256 == terraform_data.sai20_database_authority_v5_identity.output.bootstrap_guard_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.namespace_names_json == terraform_data.sai20_database_authority_v5_identity.output.namespace_names_json &&
        data.external.sai20_database_authority_v5_apply.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.service_account_names_json == terraform_data.sai20_database_authority_v5_identity.output.service_account_names_json &&
        data.external.sai20_database_authority_v5_apply.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.secret_names_json == terraform_data.sai20_database_authority_v5_identity.output.secret_names_json &&
        data.external.sai20_database_authority_v5_apply.result.sealed_kubeconfig_sha256 == terraform_data.sai20_database_authority_v5_identity.output.sealed_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_transition_mode == terraform_data.sai20_database_authority_v5_identity.output.successor_transition_mode &&
        data.external.sai20_database_authority_v5_apply.result.successor_old_objects_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_old_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_planned_objects_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_activation_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.executor_uid == terraform_data.sai20_database_authority_v5_identity.output.executor_uid &&
        data.external.sai20_database_authority_v5_apply.result.principal_identities_json == terraform_data.sai20_database_authority_v5_identity.output.principal_identities_json &&
        data.external.sai20_database_authority_v5_apply.result.rollout_lineages_json == terraform_data.sai20_database_authority_v5_identity.output.rollout_lineages_json &&
        data.external.sai20_database_authority_v5_apply.result.peer_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.peer_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.cluster_authority_review_sha256 == terraform_data.sai20_database_authority_v5_plan.output.cluster_authority_review_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.secret_names_json == terraform_data.sai20_database_authority_v5_plan.output.secret_names_json &&
        data.external.sai20_database_authority_v5_apply.result.successor_transition_mode == terraform_data.sai20_database_authority_v5_plan.output.successor_transition_mode &&
        data.external.sai20_database_authority_v5_apply.result.successor_old_objects_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_old_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_planned_objects_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.source_commit == terraform_data.sai20_database_authority_v5_plan.output.source_commit &&
        data.external.sai20_database_authority_v5_apply.result.source_tree == terraform_data.sai20_database_authority_v5_plan.output.source_tree,
        false,
      )
      error_message = "SAI-20 v5 apply requires fresh Kubernetes, bootstrap-guard, peer, cluster-RBAC and provider-membership re-observation."
    }
  }
}

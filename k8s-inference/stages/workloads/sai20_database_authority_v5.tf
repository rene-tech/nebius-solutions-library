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
        data.external.sai20_database_authority_v5_plan.result.provider_group_reobserved == "false",
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
        data.external.sai20_database_authority_v5_identity.result.apply_nonce == terraform_data.sai20_database_authority_v5_identity_nonce.output.nonce &&
        data.external.sai20_database_authority_v5_identity.result.successor_bundle_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.bootstrap_guard_sha256 == terraform_data.sai20_database_authority_v5_plan.output.bootstrap_guard_sha256 &&
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
  sai20_authority_v5_cnpg_controller_cel = join(" || ", [
    for principal in local.sai20_authority_v5_cnpg_controller_identities : format(
      "(request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)))",
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
          name = "cnpgClusterObject"
          expression = join(" ", [
            "request.namespace == 'fs2-data' && request.resource.resource == 'clusters' &&",
            "variables.targetObject.metadata.name == 'fs2-control-db'",
          ])
        },
        {
          name = "custodian"
          expression = format(
            "request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s))",
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
            "variables.controllerIdentity && variables.exactOperatorParent &&",
            "has(variables.targetObject.metadata.ownerReferences) &&",
            "variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller)",
          ])
        },
      ]
      validations = [
        {
          expression = "!(variables.databasePeer || variables.operatorPeer) || variables.custodian || (variables.databasePeer && variables.exactCnpgClusterOwner) || (variables.operatorPeer && variables.controllerOwnedOperatorChild)"
          message    = "database and CNPG operator label peers require the exact custodian or an authenticated controller with the exact signed live owner name and UID"
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
        data.external.sai20_database_authority_v5_apply.result.apply_nonce == terraform_data.sai20_database_authority_v5_apply_nonce.output.nonce &&
        data.external.sai20_database_authority_v5_apply.result.successor_bundle_sha256 == terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.bootstrap_guard_sha256 == terraform_data.sai20_database_authority_v5_identity.output.bootstrap_guard_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.peer_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.peer_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.cluster_authority_review_sha256 == terraform_data.sai20_database_authority_v5_plan.output.cluster_authority_review_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.source_commit == terraform_data.sai20_database_authority_v5_plan.output.source_commit &&
        data.external.sai20_database_authority_v5_apply.result.source_tree == terraform_data.sai20_database_authority_v5_plan.output.source_tree,
        false,
      )
      error_message = "SAI-20 v5 apply requires fresh Kubernetes, bootstrap-guard, peer, cluster-RBAC and provider-membership re-observation."
    }
  }
}

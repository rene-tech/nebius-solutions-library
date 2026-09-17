variable "sai20_database_network_custody" {
  description = "Independently accepted, non-secret admission/RBAC handoff for SAI-20 database-client labels and policy custody. The rejected SAI-03 cea63190 lineage is forbidden."
  type = object({
    schema                             = string
    status                             = string
    admission_source_commit            = string
    admission_source_tree              = string
    independent_review_receipt_sha256  = string
    rbac_census_receipt_sha256         = string
    impersonation_guard_receipt_sha256 = string
    release_writer_group               = string
    custodian = object({
      username = string
      groups   = list(string)
    })
    release_writers = list(object({
      username = string
      groups   = list(string)
    }))
    controller_writers = list(object({
      username = string
      groups   = list(string)
    }))
  })
  nullable = false

  validation {
    condition = try(
      var.sai20_database_network_custody.schema == "fs2-serve.nebius.ai/sai20-database-network-custody/v2" &&
      var.sai20_database_network_custody.status == "ACCEPTED" &&
      can(regex("^[a-f0-9]{40}$", var.sai20_database_network_custody.admission_source_commit)) &&
      can(regex("^[a-f0-9]{40}$", var.sai20_database_network_custody.admission_source_tree)) &&
      var.sai20_database_network_custody.admission_source_commit != var.sai20_database_network_custody.admission_source_tree &&
      var.sai20_database_network_custody.admission_source_commit != "cea63190aca6548d8be961a9432cc7cc1277721e" &&
      alltrue([
        for digest in [
          var.sai20_database_network_custody.independent_review_receipt_sha256,
          var.sai20_database_network_custody.rbac_census_receipt_sha256,
          var.sai20_database_network_custody.impersonation_guard_receipt_sha256,
        ] : can(regex("^[a-f0-9]{64}$", digest))
      ]) &&
      can(regex("^fs2:[a-z0-9:-]+$", var.sai20_database_network_custody.release_writer_group)) &&
      length(var.sai20_database_network_custody.custodian.username) > 0 &&
      length(var.sai20_database_network_custody.custodian.groups) > 0 &&
      contains(var.sai20_database_network_custody.custodian.groups, var.sai20_database_network_custody.release_writer_group) &&
      contains(var.sai20_database_network_custody.custodian.groups, "system:authenticated") &&
      length(var.sai20_database_network_custody.release_writers) > 0 &&
      length(var.sai20_database_network_custody.release_writers) <= 16 &&
      length(var.sai20_database_network_custody.controller_writers) > 0 &&
      length(var.sai20_database_network_custody.controller_writers) <= 16 &&
      length(distinct([
        for identity in var.sai20_database_network_custody.release_writers : identity.username
      ])) == length(var.sai20_database_network_custody.release_writers) &&
      length(distinct([
        for identity in var.sai20_database_network_custody.controller_writers : identity.username
      ])) == length(var.sai20_database_network_custody.controller_writers) &&
      contains([
        for identity in var.sai20_database_network_custody.release_writers : identity.username
      ], var.sai20_database_network_custody.custodian.username) &&
      length([
        for identity in var.sai20_database_network_custody.release_writers : identity
        if identity.username == var.sai20_database_network_custody.custodian.username &&
        toset(identity.groups) == toset(var.sai20_database_network_custody.custodian.groups)
      ]) == 1 &&
      length(distinct(var.sai20_database_network_custody.custodian.groups)) == length(var.sai20_database_network_custody.custodian.groups) &&
      alltrue([
        for identity in var.sai20_database_network_custody.release_writers :
        length(identity.username) > 0 &&
        length(identity.groups) > 0 &&
        length(distinct(identity.groups)) == length(identity.groups) &&
        contains(identity.groups, var.sai20_database_network_custody.release_writer_group) &&
        contains(identity.groups, "system:authenticated")
      ]) &&
      alltrue([
        for identity in var.sai20_database_network_custody.controller_writers :
        startswith(identity.username, "system:") &&
        length(identity.groups) > 0 &&
        length(distinct(identity.groups)) == length(identity.groups) &&
        contains(identity.groups, "system:authenticated")
      ]),
      false,
    )
    error_message = "SAI-20 requires an independently accepted v2 custody handoff, exact non-rejected source commit/tree, three SHA-256 receipts, and bounded exact release/controller/custodian identities."
  }
}

locals {
  sai20_database_custody_namespaces = toset(["fs2-system", "fs2-observability"])
  sai20_allowed_authentication_extra_keys = [
    "authentication.kubernetes.io/credential-id",
    "authentication.kubernetes.io/node-name",
    "authentication.kubernetes.io/node-uid",
    "authentication.kubernetes.io/pod-name",
    "authentication.kubernetes.io/pod-uid",
  ]
  sai20_release_writer_cel = join(" || ", [
    for identity in var.sai20_database_network_custody.release_writers : format(
      "(request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && (!has(request.userInfo.extra) || request.userInfo.extra.all(key, key in %s)))",
      jsonencode(identity.username),
      length(identity.groups),
      jsonencode(sort(identity.groups)),
      jsonencode(local.sai20_allowed_authentication_extra_keys),
    )
  ])
  sai20_controller_writer_cel = join(" || ", [
    for identity in var.sai20_database_network_custody.controller_writers : format(
      "(request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && (!has(request.userInfo.extra) || request.userInfo.extra.all(key, key in %s)))",
      jsonencode(identity.username),
      length(identity.groups),
      jsonencode(sort(identity.groups)),
      jsonencode(local.sai20_allowed_authentication_extra_keys),
    )
  ])
  sai20_custodian_cel = format(
    "(request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && (!has(request.userInfo.extra) || request.userInfo.extra.all(key, key in %s)))",
    jsonencode(var.sai20_database_network_custody.custodian.username),
    length(var.sai20_database_network_custody.custodian.groups),
    jsonencode(sort(var.sai20_database_network_custody.custodian.groups)),
    jsonencode(local.sai20_allowed_authentication_extra_keys),
  )
  sai20_custody_object_names = [
    "fs2-control-db-ingress",
    "fs2-database-client-workload-custody",
    "fs2-database-client-workload-custody-binding",
    "fs2-database-network-object-custody",
    "fs2-database-network-object-custody-binding",
    "fs2-database-client-workload-writer",
  ]
}

# This source gate deliberately prevents a workload plan from applying the
# database policy until a distinct reviewer has accepted an admission/RBAC
# successor. Exact SAI-03 cea63190 is rejected above because its label-filtered
# webhook omitted direct Pods and did not inspect controller Pod templates in
# fs2-system or fs2-observability.
resource "terraform_data" "sai20_database_network_custody" {
  input = var.sai20_database_network_custody

  lifecycle {
    precondition {
      condition = (
        var.sai20_database_network_custody.status == "ACCEPTED" &&
        var.sai20_database_network_custody.admission_source_commit != "cea63190aca6548d8be961a9432cc7cc1277721e"
      )
      error_message = "SAI-20 database isolation is blocked until independent review accepts exact Pod, controller-template, RBAC, impersonation and policy-custody coverage."
    }
  }
}

# Exact release writers receive the smallest namespace-local writer grant used
# for rollout/rollback. Existing broad grants cannot bypass the admission deny,
# and this RoleBinding is itself protected by object custody below.
resource "kubernetes_role_v1" "sai20_database_client_workload_writer" {
  for_each = local.sai20_database_custody_namespaces

  metadata {
    name      = "fs2-database-client-workload-writer"
    namespace = each.value
    labels = merge(local.common_labels, {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
    })
  }

  rule {
    api_groups = [""]
    resources  = ["pods", "replicationcontrollers"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments", "statefulsets", "daemonsets", "replicasets"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
  rule {
    api_groups = ["batch"]
    resources  = ["jobs", "cronjobs"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  depends_on = [kubernetes_manifest.sai20_database_object_custody_binding]
}

resource "kubernetes_role_binding_v1" "sai20_database_client_workload_writer" {
  for_each = local.sai20_database_custody_namespaces

  metadata {
    name      = "fs2-database-client-workload-writer"
    namespace = each.value
    labels = merge(local.common_labels, {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
    })
  }

  subject {
    kind      = "Group"
    name      = var.sai20_database_network_custody.release_writer_group
    api_group = "rbac.authorization.k8s.io"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.sai20_database_client_workload_writer[each.value].metadata[0].name
  }

  depends_on = [kubernetes_manifest.sai20_database_object_custody_binding]
}

# Match every Pod and Pod-producing controller in both relevant namespaces.
# There is intentionally no objectSelector: the policy derives the effective
# Pod labels from metadata, spec.template, or CronJob's nested Job template.
resource "kubernetes_manifest" "sai20_database_client_workload_custody" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-client-workload-custody"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
      })
      annotations = {
        "security.fs2.nebius.ai/admission-source-commit"     = var.sai20_database_network_custody.admission_source_commit
        "security.fs2.nebius.ai/admission-source-tree"       = var.sai20_database_network_custody.admission_source_tree
        "security.fs2.nebius.ai/independent-review-receipt"  = var.sai20_database_network_custody.independent_review_receipt_sha256
        "security.fs2.nebius.ai/rbac-census-receipt"         = var.sai20_database_network_custody.rbac_census_receipt_sha256
        "security.fs2.nebius.ai/impersonation-guard-receipt" = var.sai20_database_network_custody.impersonation_guard_receipt_sha256
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
            operations  = ["CREATE", "UPDATE"]
            resources   = ["pods", "replicationcontrollers"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["apps"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["deployments", "statefulsets", "daemonsets", "replicasets"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["batch"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE"]
            resources   = ["jobs", "cronjobs"]
            scope       = "Namespaced"
          },
        ]
      }
      variables = [
        {
          name = "effectivePodLabels"
          expression = join(" ", [
            "request.resource.resource == 'pods' ? (has(object.metadata.labels) ? object.metadata.labels : {}) :",
            "request.resource.resource == 'cronjobs' ? (has(object.spec.jobTemplate.spec.template.metadata.labels) ? object.spec.jobTemplate.spec.template.metadata.labels : {}) :",
            "has(object.spec.template.metadata.labels) ? object.spec.template.metadata.labels : {}",
          ])
        },
        {
          name = "databaseClient"
          expression = join(" ", [
            "(('app.kubernetes.io/name' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/name'] == 'fs2-serve-control-plane' &&",
            "'app.kubernetes.io/instance' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/instance'] == 'fs2-serve-control-plane' &&",
            "'app.kubernetes.io/component' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/component'] in",
            jsonencode(concat(local.sai20_database_client_components, [local.sai20_storage_reconciler_v3_component])),
            ") || ('app.kubernetes.io/name' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/name'] == 'grafana') ||",
            "('app.kubernetes.io/component' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/component'] == 'acceptance' &&",
            "'app.kubernetes.io/managed-by' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/managed-by'] == 'terraform' &&",
            "'app.kubernetes.io/part-of' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/part-of'] == 'fs2-serve' &&",
            "'fs2.nebius.ai/environment' in variables.effectivePodLabels && variables.effectivePodLabels['fs2.nebius.ai/environment'] == 'disposable'))",
          ])
        },
        {
          name       = "releaseWriter"
          expression = local.sai20_release_writer_cel
        },
        {
          name       = "controllerWriter"
          expression = local.sai20_controller_writer_cel
        },
        {
          name = "controllerOwnedChild"
          expression = join(" ", [
            "has(object.metadata.ownerReferences) && object.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller &&",
            "((request.resource.resource == 'pods' && owner.kind in ['ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job', 'ReplicationController']) ||",
            "(request.resource.resource == 'replicasets' && owner.kind == 'Deployment') ||",
            "(request.resource.resource == 'jobs' && owner.kind == 'CronJob')))",
          ])
        },
        {
          name = "v3GenerationBound"
          expression = join(" ", [
            "!('app.kubernetes.io/component' in variables.effectivePodLabels) ||",
            "variables.effectivePodLabels['app.kubernetes.io/component'] != 'storage-reconciler-v3' ||",
            "('fs2.nebius.ai/storage-egress-generation' in variables.effectivePodLabels &&",
            "'fs2.nebius.ai/storage-rollout-generation' in variables.effectivePodLabels)",
          ])
        },
      ]
      validations = [
        {
          expression = "!variables.databaseClient || variables.releaseWriter || (variables.controllerWriter && variables.controllerOwnedChild)"
          message    = "database-client labels require an accepted release writer or an exact controller creating an owned Pod, ReplicaSet or Job"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.databaseClient || variables.v3GenerationBound"
          message    = "storage-reconciler-v3 requires both SAI-08 content-derived generation labels"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_object_custody_binding]
}

resource "kubernetes_manifest" "sai20_database_client_workload_custody_binding" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-client-workload-custody-binding"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
      })
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_client_workload_custody.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = sort(tolist(local.sai20_database_custody_namespaces))
          }]
        }
      }
    }
  }
}

# This API-server-native policy protects the database NetworkPolicy and every
# SAI-20 custody object. The matching decision uses exact names and namespaces,
# never a label that an object writer can omit.
resource "kubernetes_manifest" "sai20_database_object_custody" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-network-object-custody"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
      })
      annotations = {
        "security.fs2.nebius.ai/independent-review-receipt" = var.sai20_database_network_custody.independent_review_receipt_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [
          {
            apiGroups   = ["networking.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["networkpolicies"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["rbac.authorization.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["roles", "rolebindings"]
            scope       = "Namespaced"
          },
          {
            apiGroups   = ["admissionregistration.k8s.io"]
            apiVersions = ["v1"]
            operations  = ["CREATE", "UPDATE", "DELETE"]
            resources   = ["validatingadmissionpolicies", "validatingadmissionpolicybindings"]
            scope       = "Cluster"
          },
        ]
      }
      variables = [
        {
          name       = "targetMetadata"
          expression = "request.operation == 'DELETE' ? oldObject.metadata : object.metadata"
        },
        {
          name = "protectedObject"
          expression = join(" ", [
            "(request.resource.group == 'networking.k8s.io' && request.namespace == 'fs2-data' && variables.targetMetadata.name == 'fs2-control-db-ingress') ||",
            "(request.resource.group == 'rbac.authorization.k8s.io' && request.namespace in ['fs2-system', 'fs2-observability'] && variables.targetMetadata.name == 'fs2-database-client-workload-writer') ||",
            "(request.resource.group == 'admissionregistration.k8s.io' && variables.targetMetadata.name in",
            jsonencode([for name in local.sai20_custody_object_names : name if startswith(name, "fs2-database-") && name != "fs2-database-client-workload-writer"]),
            ")",
          ])
        },
        {
          name       = "custodian"
          expression = local.sai20_custodian_cel
        },
      ]
      validations = [
        {
          expression = "!variables.protectedObject || request.operation != 'DELETE'"
          message    = "SAI-20 database-network custody objects are update-only"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.protectedObject || variables.custodian"
          message    = "SAI-20 database-network custody requires the exact accepted custodian identity"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [terraform_data.sai20_database_network_custody]
}

resource "kubernetes_manifest" "sai20_database_object_custody_binding" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-network-object-custody-binding"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v2"
      })
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_object_custody.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
    }
  }
}

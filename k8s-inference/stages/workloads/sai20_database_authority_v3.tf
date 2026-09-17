variable "sai20_database_authority_v3" {
  description = "Paths to the independently signed SAI-20 authority packet and its out-of-band security-owner Ed25519 trust root. Neither file may contain credentials."
  type = object({
    handoff_path      = string
    public_key_path   = string
    public_key_sha256 = string
    authority_key_id  = string
  })
  nullable = false

  validation {
    condition = (
      startswith(var.sai20_database_authority_v3.handoff_path, "/") &&
      startswith(var.sai20_database_authority_v3.public_key_path, "/") &&
      can(regex("^[a-f0-9]{64}$", var.sai20_database_authority_v3.public_key_sha256)) &&
      can(regex("^sai20-security-owner:[a-z0-9][a-z0-9._-]{2,63}$", var.sai20_database_authority_v3.authority_key_id))
    )
    error_message = "SAI-20 v3 authority inputs require absolute paths, an exact Ed25519 public-key fingerprint, and a security-owner key ID."
  }
}

# This verifier consumes, but never collects, live evidence. The authority
# packet must already contain independently signed complete-list receipts for
# NetworkPolicies, Pods/controllers, RBAC, group membership and impersonation.
# It also binds the packet to the exact clean Git HEAD/tree and required blobs.
data "external" "sai20_database_authority_v3" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority.py"),
  ]

  query = {
    handoff_path               = var.sai20_database_authority_v3.handoff_path
    public_key_path            = var.sai20_database_authority_v3.public_key_path
    public_key_sha256          = var.sai20_database_authority_v3.public_key_sha256
    authority_key_id           = var.sai20_database_authority_v3.authority_key_id
    repository_root            = abspath("${path.module}/../../..")
    expected_project_id        = nonsensitive(var.project_id)
    expected_cluster_id        = var.cluster_id
    expected_policy_source_sha256 = filesha256("${path.module}/sai20_network_isolation.tf")
    legacy_contract_sha256     = sha256(jsonencode(var.sai20_database_network_custody))
    legacy_release_writer_group = var.sai20_database_network_custody.release_writer_group
  }
}

resource "terraform_data" "sai20_database_authority_v3" {
  input = data.external.sai20_database_authority_v3.result

  lifecycle {
    precondition {
      condition = try(
        data.external.sai20_database_authority_v3.result.verified == "true" &&
        can(regex("^[a-f0-9]{64}$", data.external.sai20_database_authority_v3.result.handoff_sha256)) &&
        can(regex("^[a-f0-9]{40}$", data.external.sai20_database_authority_v3.result.source_commit)) &&
        can(regex("^[a-f0-9]{40}$", data.external.sai20_database_authority_v3.result.source_tree)),
        false,
      )
      error_message = "SAI-20 activation requires a current Ed25519-signed, Git-bound, complete policy/workload/RBAC authority packet."
    }
  }
}

locals {
  sai20_authority_v3_release_principals    = jsondecode(data.external.sai20_database_authority_v3.result.release_principals_json)
  sai20_authority_v3_controller_principals = jsondecode(data.external.sai20_database_authority_v3.result.controller_principals_json)
  sai20_authority_v3_custodian             = jsondecode(data.external.sai20_database_authority_v3.result.custodian_json)
  sai20_authority_v3_policy_names          = jsondecode(data.external.sai20_database_authority_v3.result.network_policy_names_json)

  sai20_authority_v3_identity_cel = {
    for principal in concat(
      local.sai20_authority_v3_release_principals,
      local.sai20_authority_v3_controller_principals,
      [local.sai20_authority_v3_custodian],
    ) : principal.id => format(
      "(request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && (!has(request.userInfo.extra) || request.userInfo.extra.all(key, key in %s)))",
      jsonencode(principal.username),
      length(principal.groups),
      jsonencode(sort(principal.groups)),
      jsonencode(local.sai20_allowed_authentication_extra_keys),
    )
  }

  sai20_authority_v3_release_grants = flatten([
    for principal in local.sai20_authority_v3_release_principals : [
      for grant_index, grant in principal.grants : merge(grant, {
        key             = "${principal.id}-${grant.namespace}-${grant.resource}-${grant_index}"
        principal_id    = principal.id
        subject         = principal.subject
        identity_cel    = local.sai20_authority_v3_identity_cel[principal.id]
        api_group       = lookup(local.sai20_authority_v3_resource_api_groups, grant.resource, null)
      })
    ]
  ])
  sai20_authority_v3_release_grants_by_key = {
    for grant in local.sai20_authority_v3_release_grants : grant.key => grant
  }
  sai20_authority_v3_resource_api_groups = {
    replicationcontrollers = ""
    deployments            = "apps"
    statefulsets            = "apps"
    daemonsets              = "apps"
    replicasets             = "apps"
    jobs                    = "batch"
    cronjobs                = "batch"
  }
  sai20_authority_v3_release_mutation_cel = join(" || ", [
    for grant in local.sai20_authority_v3_release_grants : format(
      "(%s && request.namespace == %s && request.resource.resource == %s && request.operation in %s && variables.targetObject.metadata.name in %s)",
      grant.identity_cel,
      jsonencode(grant.namespace),
      jsonencode(grant.resource),
      jsonencode(grant.operations),
      jsonencode(grant.names),
    )
  ])
  sai20_authority_v3_controller_cel = join(" || ", [
    for principal in local.sai20_authority_v3_controller_principals : local.sai20_authority_v3_identity_cel[principal.id]
  ])
  sai20_authority_v3_custodian_cel = local.sai20_authority_v3_identity_cel[local.sai20_authority_v3_custodian.id]

  sai20_authority_v3_protected_names = [
    "fs2-control-db-ingress",
    "fs2-database-client-workload-writer",
    "fs2-database-client-workload-custody",
    "fs2-database-client-workload-custody-binding",
    "fs2-database-network-object-custody",
    "fs2-database-network-object-custody-binding",
    "fs2-database-authorized-workload-writer-v3",
    "fs2-database-workload-custody-v3",
    "fs2-database-workload-custody-binding-v3",
    "fs2-database-policy-set-custody-v3",
    "fs2-database-policy-set-custody-binding-v3",
    "fs2-database-authority-object-custody-v3",
    "fs2-database-authority-object-custody-binding-v3",
  ]
}

# These grants name exact signed principals and existing object names. They do
# not grant Pod mutation, create, delete, escalation, bind or impersonation.
# Existing release authority remains separately enumerated and is constrained
# by the admission policy below for every Pod-producing object, not only an
# object that already carries a database label.
resource "kubernetes_role_v1" "sai20_database_authorized_workload_writer_v3" {
  for_each = local.sai20_authority_v3_release_grants_by_key

  metadata {
    name      = "fs2-db-writer-${substr(sha256(each.key), 0, 12)}"
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
    })
    annotations = {
      "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      "security.fs2.nebius.ai/principal-id"             = each.value.principal_id
    }
  }

  rule {
    api_groups     = [each.value.api_group]
    resources      = [each.value.resource]
    resource_names = each.value.names
    verbs           = ["get", "update", "patch"]
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v3]
}

resource "kubernetes_role_binding_v1" "sai20_database_authorized_workload_writer_v3" {
  for_each = local.sai20_authority_v3_release_grants_by_key

  metadata {
    name      = kubernetes_role_v1.sai20_database_authorized_workload_writer_v3[each.key].metadata[0].name
    namespace = each.value.namespace
    labels = merge(local.common_labels, {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
    })
    annotations = {
      "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      "security.fs2.nebius.ai/principal-id"             = each.value.principal_id
    }
  }

  subject {
    kind      = each.value.subject.kind
    name      = each.value.subject.name
    namespace = each.value.subject.kind == "ServiceAccount" ? each.value.subject.namespace : null
    api_group = each.value.subject.kind == "ServiceAccount" ? "" : "rbac.authorization.k8s.io"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.sai20_database_authorized_workload_writer_v3[each.key].metadata[0].name
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v3]
}

# This successor evaluates every direct Pod and every supported controller
# template in both namespaces. It neutralizes the rejected broad legacy group
# even for innocuously labelled top-level controllers: every mutation must be
# an exact signed principal/object grant or an owned child from an exact
# controller identity.
resource "kubernetes_manifest" "sai20_database_workload_custody_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-workload-custody-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
        "security.fs2.nebius.ai/source-commit"            = terraform_data.sai20_database_authority_v3.output.source_commit
        "security.fs2.nebius.ai/source-tree"              = terraform_data.sai20_database_authority_v3.output.source_tree
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
            "request.resource.resource == 'pods' ? (has(variables.targetObject.metadata.labels) ? variables.targetObject.metadata.labels : {}) :",
            "request.resource.resource == 'cronjobs' ? (has(variables.targetObject.spec.jobTemplate.spec.template.metadata.labels) ? variables.targetObject.spec.jobTemplate.spec.template.metadata.labels : {}) :",
            "has(variables.targetObject.spec.template.metadata.labels) ? variables.targetObject.spec.template.metadata.labels : {}",
          ])
        },
        {
          name       = "releaseMutation"
          expression = local.sai20_authority_v3_release_mutation_cel
        },
        {
          name       = "controllerIdentity"
          expression = local.sai20_authority_v3_controller_cel
        },
        {
          name = "controllerOwnedChild"
          expression = join(" ", [
            "has(variables.targetObject.metadata.ownerReferences) && variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller &&",
            "((request.resource.resource == 'pods' && owner.kind in ['ReplicaSet', 'StatefulSet', 'DaemonSet', 'Job', 'ReplicationController']) ||",
            "(request.resource.resource == 'replicasets' && owner.kind == 'Deployment') ||",
            "(request.resource.resource == 'jobs' && owner.kind == 'CronJob')))",
          ])
        },
        {
          name = "databaseClient"
          expression = join(" ", [
            "(('app.kubernetes.io/name' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/name'] == 'fs2-serve-control-plane' &&",
            "'app.kubernetes.io/instance' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/instance'] == 'fs2-serve-control-plane' &&",
            "'app.kubernetes.io/component' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/component'] in",
            jsonencode(concat(local.sai20_database_client_components, [local.sai20_storage_reconciler_v2_component, local.sai20_storage_reconciler_v3_component])),
            ") || ('app.kubernetes.io/name' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/name'] == 'grafana') ||",
            "('app.kubernetes.io/component' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/component'] == 'acceptance' &&",
            "'app.kubernetes.io/managed-by' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/managed-by'] == 'terraform' &&",
            "'app.kubernetes.io/part-of' in variables.effectivePodLabels && variables.effectivePodLabels['app.kubernetes.io/part-of'] == 'fs2-serve' &&",
            "'fs2.nebius.ai/environment' in variables.effectivePodLabels && variables.effectivePodLabels['fs2.nebius.ai/environment'] == 'disposable'))",
          ])
        },
        {
          name = "storageGenerationBound"
          expression = join(" ", [
            "!('app.kubernetes.io/component' in variables.effectivePodLabels) ||",
            "!(variables.effectivePodLabels['app.kubernetes.io/component'] in ['storage-reconciler-v2', 'storage-reconciler-v3']) ||",
            "('fs2.nebius.ai/storage-egress-generation' in variables.effectivePodLabels &&",
            "(variables.effectivePodLabels['app.kubernetes.io/component'] != 'storage-reconciler-v3' ||",
            "'fs2.nebius.ai/storage-rollout-generation' in variables.effectivePodLabels))",
          ])
        },
      ]
      validations = [
        {
          expression = "variables.releaseMutation || (variables.controllerIdentity && variables.controllerOwnedChild)"
          message    = "workload mutation requires an exact signed principal/object grant or an exact controller-owned child"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.databaseClient || variables.storageGenerationBound"
          message    = "every retained storage-reconciler generation requires its signed content-derived generation labels"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v3]
}

resource "kubernetes_manifest" "sai20_database_workload_custody_binding_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-workload-custody-binding-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_workload_custody_v3.manifest.metadata.name
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

# Kubernetes NetworkPolicy allows are additive. This policy therefore guards
# the complete fs2-data policy set, not just the canonical object's name. A
# signed complete-list inventory must classify every existing selector, and no
# unlisted name can be created or updated.
resource "kubernetes_manifest" "sai20_database_policy_set_custody_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-policy-set-custody-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = ["networking.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["networkpolicies"]
          scope       = "Namespaced"
        }]
      }
      variables = [
        {
          name       = "targetObject"
          expression = "request.operation == 'DELETE' ? oldObject : object"
        },
        {
          name       = "targetMetadata"
          expression = "variables.targetObject.metadata"
        },
        {
          name       = "custodian"
          expression = local.sai20_authority_v3_custodian_cel
        },
        {
          name = "canonicalSelector"
          expression = join(" ", [
            "variables.targetMetadata.name == 'fs2-control-db-ingress' &&",
            "has(variables.targetObject.spec.podSelector.matchLabels) &&",
            "variables.targetObject.spec.podSelector.matchLabels.size() == 1 &&",
            "'cnpg.io/cluster' in variables.targetObject.spec.podSelector.matchLabels &&",
            "variables.targetObject.spec.podSelector.matchLabels['cnpg.io/cluster'] == 'fs2-control-db' &&",
            "(!has(variables.targetObject.spec.podSelector.matchExpressions) || variables.targetObject.spec.podSelector.matchExpressions.size() == 0) &&",
            "has(variables.targetObject.spec.policyTypes) && variables.targetObject.spec.policyTypes.size() == 1 && variables.targetObject.spec.policyTypes[0] == 'Ingress'",
          ])
        },
        {
          name = "explicitDatabaseExclusion"
          expression = join(" ", [
            "variables.targetMetadata.name != 'fs2-control-db-ingress' && (",
            "(has(variables.targetObject.spec.podSelector.matchLabels) && 'cnpg.io/cluster' in variables.targetObject.spec.podSelector.matchLabels && variables.targetObject.spec.podSelector.matchLabels['cnpg.io/cluster'] != 'fs2-control-db') ||",
            "(has(variables.targetObject.spec.podSelector.matchExpressions) && variables.targetObject.spec.podSelector.matchExpressions.exists(expression,",
            "expression.key == 'cnpg.io/cluster' && ((expression.operator == 'In' && !('fs2-control-db' in expression.values)) ||",
            "(expression.operator == 'NotIn' && 'fs2-control-db' in expression.values) || expression.operator == 'DoesNotExist'))))",
          ])
        },
      ]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "fs2-data NetworkPolicies are non-deletable during SAI-20 activation"
          reason     = "Forbidden"
        },
        {
          expression = "variables.custodian"
          message    = "fs2-data NetworkPolicy mutation requires the exact signed custodian"
          reason     = "Forbidden"
        },
        {
          expression = "variables.targetMetadata.name in ${jsonencode(local.sai20_authority_v3_policy_names)}"
          message    = "an unlisted fs2-data NetworkPolicy requires a new complete signed inventory"
          reason     = "Forbidden"
        },
        {
          expression = "variables.canonicalSelector || variables.explicitDatabaseExclusion"
          message    = "only the canonical policy may select fs2-control-db; every other fs2-data policy must explicitly exclude that cluster label"
          reason     = "Forbidden"
        },
        {
          expression = "has(variables.targetMetadata.annotations) && 'security.fs2.nebius.ai/authority-handoff-sha256' in variables.targetMetadata.annotations && variables.targetMetadata.annotations['security.fs2.nebius.ai/authority-handoff-sha256'] == '${terraform_data.sai20_database_authority_v3.output.handoff_sha256}'"
          message    = "fs2-data NetworkPolicy mutation must bind the current signed inventory"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v3]
}

resource "kubernetes_manifest" "sai20_database_policy_set_custody_binding_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-policy-set-custody-binding-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_policy_set_custody_v3.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
        namespaceSelector = {
          matchLabels = {
            "kubernetes.io/metadata.name" = "fs2-data"
          }
        }
      }
    }
  }
}

# Protect every old and new SAI-20 authority object by exact name. This closes
# the bootstrap gap in the rejected name-only policy and prevents mutation of
# the inert legacy group grant without the same signed custodian identity.
resource "kubernetes_manifest" "sai20_database_authority_object_custody_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-authority-object-custody-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [
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
          name       = "protectedObject"
          expression = "variables.targetMetadata.name in ${jsonencode(local.sai20_authority_v3_protected_names)} || variables.targetMetadata.name.startsWith('fs2-db-writer-')"
        },
        {
          name       = "custodian"
          expression = local.sai20_authority_v3_custodian_cel
        },
      ]
      validations = [
        {
          expression = "!variables.protectedObject || request.operation != 'DELETE'"
          message    = "SAI-20 v3 authority objects are non-deletable"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.protectedObject || variables.custodian"
          message    = "SAI-20 v3 authority-object mutation requires the exact signed custodian"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [terraform_data.sai20_database_authority_v3]
}

resource "kubernetes_manifest" "sai20_database_authority_object_custody_binding_v3" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-authority-object-custody-binding-v3"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v3"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-handoff-sha256" = terraform_data.sai20_database_authority_v3.output.handoff_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_authority_object_custody_v3.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
    }
  }
}

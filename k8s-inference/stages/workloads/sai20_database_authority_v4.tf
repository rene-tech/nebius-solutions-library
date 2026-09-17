variable "sai20_database_authority_v4" {
  description = "Paths to the dual-signed SAI-20 v4 authority bundle and the exact reviewed kubectl binary. Trust roots are source-owned and cannot be supplied here."
  type = object({
    authority_bundle_path = string
    kubectl_path          = string
  })
  nullable = false

  validation {
    condition = (
      startswith(var.sai20_database_authority_v4.authority_bundle_path, "/") &&
      !strcontains(var.sai20_database_authority_v4.authority_bundle_path, "..") &&
      startswith(var.sai20_database_authority_v4.kubectl_path, "/") &&
      !strcontains(var.sai20_database_authority_v4.kubectl_path, "..")
    )
    error_message = "SAI-20 v4 requires absolute authority-bundle and kubectl paths without parent traversal."
  }
}

locals {
  sai20_authority_v4_root_registry_path = abspath("${path.module}/../../security/sai20/authority-roots-v1.json")
  sai20_authority_v4_ingress_contract_path = abspath(
    "${path.module}/contracts/sai20-control-db-ingress-v4.json"
  )
  sai20_authority_v4_ingress_contract = jsondecode(replace(
    file(local.sai20_authority_v4_ingress_contract_path),
    "$${run_id}",
    var.run_id,
  ))
  sai20_authority_v4_ingress_spec_sha256 = sha256(jsonencode(local.sai20_authority_v4_ingress_contract))

  sai20_authority_v4_common_query = {
    authority_bundle_path                 = var.sai20_database_authority_v4.authority_bundle_path
    legacy_v3_handoff_path                = var.sai20_database_authority_v3.handoff_path
    root_registry_path                    = local.sai20_authority_v4_root_registry_path
    ingress_contract_path                 = local.sai20_authority_v4_ingress_contract_path
    expected_root_registry_sha256         = filesha256(local.sai20_authority_v4_root_registry_path)
    expected_ingress_contract_file_sha256 = filesha256(local.sai20_authority_v4_ingress_contract_path)
    repository_root                       = abspath("${path.module}/../../..")
    expected_project_id                   = nonsensitive(var.project_id)
    expected_cluster_id                   = var.cluster_id
    run_id                                = var.run_id
  }
}

# Plan-time verification is useful for review, but is never activation
# authority. The verifier accepts only the committed root registry, two
# distinct signatures and raw API/provider response transcripts.
data "external" "sai20_database_authority_v4_plan" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v4.py"),
  ]

  query = merge(local.sai20_authority_v4_common_query, {
    mode = "plan"
  })
}

resource "terraform_data" "sai20_database_authority_v4_plan" {
  input = data.external.sai20_database_authority_v4_plan.result

  lifecycle {
    precondition {
      condition = try(
        terraform_data.sai20_database_authority_v5_plan.output.successor_verified == "true" &&
        terraform_data.sai20_database_authority_v5_plan.output.successor_bundle_sha256 == data.external.sai20_database_authority_v5_plan.result.successor_bundle_sha256 &&
        data.external.sai20_database_authority_v4_plan.result.verified == "true" &&
        data.external.sai20_database_authority_v4_plan.result.apply_reobserved == "false" &&
        data.external.sai20_database_authority_v4_plan.result.ingress_spec_sha256 == local.sai20_authority_v4_ingress_spec_sha256,
        false,
      )
      error_message = "SAI-20 planning requires a current dual-signed raw-evidence bundle rooted only in the committed registry."
    }
  }
}

# This first apply-time read proves that the process about to bootstrap the
# transition guards is the exact signed custodian using the provider's bounded
# kubeconfig/context. It performs no inventory mutation or authorization.
resource "terraform_data" "sai20_database_authority_v4_identity_nonce" {
  input = {
    nonce         = timestamp()
    bundle_sha256 = terraform_data.sai20_database_authority_v4_plan.output.bundle_sha256
  }
}

data "external" "sai20_database_authority_v4_identity" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v4.py"),
  ]

  query = merge(local.sai20_authority_v4_common_query, {
    mode            = "identity"
    kubeconfig_path = var.kubeconfig_path
    kube_context    = var.kube_context
    kubectl_path    = var.sai20_database_authority_v4.kubectl_path
    apply_nonce     = terraform_data.sai20_database_authority_v4_identity_nonce.output.nonce
  })
}

resource "terraform_data" "sai20_database_authority_v4_identity" {
  input = data.external.sai20_database_authority_v4_identity.result

  lifecycle {
    precondition {
      condition = try(
        terraform_data.sai20_database_authority_v5_identity.output.successor_verified == "true" &&
        terraform_data.sai20_database_authority_v5_identity.output.bootstrap_reobserved == "true" &&
        terraform_data.sai20_database_authority_v5_identity.output.source_commit == data.external.sai20_database_authority_v4_identity.result.source_commit &&
        terraform_data.sai20_database_authority_v5_identity.output.source_tree == data.external.sai20_database_authority_v4_identity.result.source_tree &&
        data.external.sai20_database_authority_v4_identity.result.verified == "true" &&
        data.external.sai20_database_authority_v4_identity.result.identity_reobserved == "true" &&
        data.external.sai20_database_authority_v4_identity.result.apply_reobserved == "false" &&
        data.external.sai20_database_authority_v4_identity.result.apply_nonce == terraform_data.sai20_database_authority_v4_identity_nonce.output.nonce &&
        data.external.sai20_database_authority_v4_identity.result.bundle_sha256 == terraform_data.sai20_database_authority_v4_plan.output.bundle_sha256 &&
        data.external.sai20_database_authority_v4_identity.result.source_commit == terraform_data.sai20_database_authority_v4_plan.output.source_commit &&
        data.external.sai20_database_authority_v4_identity.result.source_tree == terraform_data.sai20_database_authority_v4_plan.output.source_tree &&
        data.external.sai20_database_authority_v4_identity.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v4_identity.result.namespace_names_json == terraform_data.sai20_database_authority_v4_plan.output.namespace_names_json &&
        data.external.sai20_database_authority_v4_identity.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v4_identity.result.service_account_names_json == terraform_data.sai20_database_authority_v4_plan.output.service_account_names_json &&
        data.external.sai20_database_authority_v4_identity.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v4_identity.result.secret_names_json == terraform_data.sai20_database_authority_v4_plan.output.secret_names_json &&
        data.external.sai20_database_authority_v4_identity.result.sealed_kubeconfig_sha256 == terraform_data.sai20_database_authority_v5_identity.output.sealed_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v4_identity.result.executor_uid == terraform_data.sai20_database_authority_v4_plan.output.executor_uid &&
        data.external.sai20_database_authority_v4_identity.result.executor_uid == terraform_data.sai20_database_authority_v5_identity.output.executor_uid &&
        data.external.sai20_database_authority_v4_identity.result.executor_username == terraform_data.sai20_database_authority_v4_plan.output.executor_username &&
        data.external.sai20_database_authority_v4_identity.result.executor_groups_json == terraform_data.sai20_database_authority_v4_plan.output.executor_groups_json &&
        data.external.sai20_database_authority_v4_identity.result.executor_extra_json == terraform_data.sai20_database_authority_v4_plan.output.executor_extra_json,
        false,
      )
      error_message = "SAI-20 transition bootstrap requires the actual Terraform executor to be the freshly re-observed signed custodian."
    }
  }
}

# timestamp() is deliberately unknown during planning. Referencing this output
# forces the second external verification to run during every apply, so a
# saved plan cannot replay an expired observation or a changed inventory.
resource "terraform_data" "sai20_database_authority_v4_apply_nonce" {
  input = {
    nonce         = timestamp()
    bundle_sha256 = terraform_data.sai20_database_authority_v4_plan.output.bundle_sha256
  }

  depends_on = [
    kubernetes_manifest.sai20_database_policy_freeze_v4,
    kubernetes_manifest.sai20_database_policy_freeze_binding_v4,
    kubernetes_manifest.sai20_database_exact_owner_v4,
    kubernetes_manifest.sai20_database_exact_owner_binding_v4,
  ]
}

data "external" "sai20_database_authority_v4_apply" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    abspath("${path.module}/../../components/control-plane"),
    "python",
    abspath("${path.module}/scripts/sai20_database_authority_v4.py"),
  ]

  query = merge(local.sai20_authority_v4_common_query, {
    mode            = "apply"
    kubeconfig_path = var.kubeconfig_path
    kube_context    = var.kube_context
    kubectl_path    = var.sai20_database_authority_v4.kubectl_path
    apply_nonce     = terraform_data.sai20_database_authority_v4_apply_nonce.output.nonce
  })
}

resource "terraform_data" "sai20_database_authority_v4_apply" {
  input = data.external.sai20_database_authority_v4_apply.result

  lifecycle {
    precondition {
      condition = try(
        terraform_data.sai20_database_authority_v5_apply.output.successor_verified == "true" &&
        terraform_data.sai20_database_authority_v5_apply.output.bootstrap_reobserved == "true" &&
        terraform_data.sai20_database_authority_v5_apply.output.provider_group_reobserved == "true" &&
        terraform_data.sai20_database_authority_v5_apply.output.source_commit == data.external.sai20_database_authority_v4_apply.result.source_commit &&
        terraform_data.sai20_database_authority_v5_apply.output.source_tree == data.external.sai20_database_authority_v4_apply.result.source_tree &&
        data.external.sai20_database_authority_v4_apply.result.verified == "true" &&
        data.external.sai20_database_authority_v4_apply.result.identity_reobserved == "true" &&
        data.external.sai20_database_authority_v4_apply.result.apply_reobserved == "true" &&
        data.external.sai20_database_authority_v4_apply.result.apply_nonce == terraform_data.sai20_database_authority_v4_apply_nonce.output.nonce &&
        data.external.sai20_database_authority_v4_apply.result.bundle_sha256 == terraform_data.sai20_database_authority_v4_plan.output.bundle_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.payload_sha256 == terraform_data.sai20_database_authority_v4_plan.output.payload_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.source_commit == terraform_data.sai20_database_authority_v4_plan.output.source_commit &&
        data.external.sai20_database_authority_v4_apply.result.source_tree == terraform_data.sai20_database_authority_v4_plan.output.source_tree &&
        data.external.sai20_database_authority_v4_apply.result.ingress_spec_sha256 == local.sai20_authority_v4_ingress_spec_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.namespace_names_json == terraform_data.sai20_database_authority_v4_plan.output.namespace_names_json &&
        data.external.sai20_database_authority_v4_apply.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.service_account_names_json == terraform_data.sai20_database_authority_v4_plan.output.service_account_names_json &&
        data.external.sai20_database_authority_v4_apply.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v4_plan.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.secret_names_json == terraform_data.sai20_database_authority_v4_plan.output.secret_names_json &&
        data.external.sai20_database_authority_v4_apply.result.sealed_kubeconfig_sha256 == terraform_data.sai20_database_authority_v4_identity.output.sealed_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.sealed_kubeconfig_sha256 == terraform_data.sai20_database_authority_v5_apply.output.sealed_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.executor_uid == terraform_data.sai20_database_authority_v4_plan.output.executor_uid &&
        data.external.sai20_database_authority_v4_apply.result.executor_uid == terraform_data.sai20_database_authority_v4_identity.output.executor_uid &&
        data.external.sai20_database_authority_v4_apply.result.executor_uid == terraform_data.sai20_database_authority_v5_apply.output.executor_uid &&
        data.external.sai20_database_authority_v4_apply.result.executor_username == terraform_data.sai20_database_authority_v4_plan.output.executor_username &&
        data.external.sai20_database_authority_v4_apply.result.executor_groups_json == terraform_data.sai20_database_authority_v4_plan.output.executor_groups_json &&
        data.external.sai20_database_authority_v4_apply.result.executor_extra_json == terraform_data.sai20_database_authority_v4_plan.output.executor_extra_json &&
        data.external.sai20_database_authority_v4_apply.result.authorized_parents_json == terraform_data.sai20_database_authority_v4_plan.output.authorized_parents_json &&
        data.external.sai20_database_authority_v4_apply.result.network_policy_specs_json == terraform_data.sai20_database_authority_v4_plan.output.network_policy_specs_json &&
        data.external.sai20_database_authority_v4_apply.result.bundle_sha256 == terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v4_identity.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v4_identity.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v4_identity.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v4_apply.result.secret_names_json == terraform_data.sai20_database_authority_v4_identity.output.secret_names_json &&
        data.external.sai20_database_authority_v4_apply.result.executor_username == terraform_data.sai20_database_authority_v4_identity.output.executor_username &&
        data.external.sai20_database_authority_v4_apply.result.executor_groups_json == terraform_data.sai20_database_authority_v4_identity.output.executor_groups_json &&
        data.external.sai20_database_authority_v4_apply.result.executor_extra_json == terraform_data.sai20_database_authority_v4_identity.output.executor_extra_json,
        false,
      )
      error_message = "SAI-20 apply-time re-observation must match the signed plan bundle, exact executor and complete live inventory."
    }
  }
}

locals {
  sai20_authority_v5_principal_identities = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.principal_identities_json
  )
  sai20_authority_v5_exact_principal_cel = join(" || ", [
    for principal in local.sai20_authority_v5_principal_identities : format(
      "(has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)))",
      jsonencode(principal.uid),
      jsonencode(principal.username),
      length(principal.groups),
      jsonencode(principal.groups),
      jsonencode(principal.extra),
      jsonencode(principal.extra),
    )
  ])
  sai20_authority_v4_transition_policy_specs = jsondecode(
    terraform_data.sai20_database_authority_v4_plan.output.network_policy_specs_json
  )
  sai20_authority_v4_transition_policy_cel = join(" || ", [
    for policy in local.sai20_authority_v4_transition_policy_specs : format(
      "(object.metadata.name == %s && object.spec == %s)",
      jsonencode(policy.name),
      jsonencode(policy.spec),
    )
  ])
  sai20_authority_v4_authorized_parents = jsondecode(
    terraform_data.sai20_database_authority_v4_plan.output.authorized_parents_json
  )
  sai20_authority_v4_owner_child_resource = {
    deployments            = "replicasets"
    statefulsets            = "pods"
    daemonsets              = "pods"
    replicasets             = "pods"
    jobs                    = "pods"
    cronjobs                = "jobs"
    replicationcontrollers = "pods"
  }
  sai20_authority_v4_exact_parent_cel = join(" || ", [
    for parent in local.sai20_authority_v4_authorized_parents : format(
      "(request.namespace == %s && request.resource.resource == %s && request.userInfo.username == %s && has(variables.targetObject.metadata.ownerReferences) && variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller && owner.apiVersion == %s && owner.kind == %s && owner.name == %s && string(owner.uid) == %s))",
      jsonencode(parent.namespace),
      jsonencode(local.sai20_authority_v4_owner_child_resource[parent.resource]),
      jsonencode(parent.controller_username),
      jsonencode(parent.api_version),
      jsonencode(parent.kind),
      jsonencode(parent.name),
      jsonencode(parent.uid),
    )
  ])
  sai20_authority_v4_protected_names = concat(
    local.sai20_authority_v3_protected_names,
    [
      "fs2-sai20-ingress-contract-v4",
      "fs2-database-ingress-exact-spec-v4",
      "fs2-database-ingress-exact-spec-binding-v4",
      "fs2-database-exact-owner-v4",
      "fs2-database-exact-owner-binding-v4",
      "fs2-database-authority-object-custody-v4",
      "fs2-database-authority-object-custody-binding-v4",
      "fs2-database-policy-freeze-v4",
      "fs2-database-policy-freeze-binding-v4",
    ],
  )
}

# This policy is installed first by the freshly authenticated signed custodian.
# Its binding is the only bootstrap transition; every later v4 object depends
# on that binding and is then non-deletable and custodian-only.
resource "kubernetes_manifest" "sai20_database_authority_object_custody_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-authority-object-custody-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
        "security.fs2.nebius.ai/source-commit"           = terraform_data.sai20_database_authority_v4_identity.output.source_commit
        "security.fs2.nebius.ai/source-tree"             = terraform_data.sai20_database_authority_v4_identity.output.source_tree
        "security.fs2.nebius.ai/bootstrap-guard-sha256"  = terraform_data.sai20_database_authority_v5_identity.output.bootstrap_guard_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256" = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
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
          expression = "variables.targetMetadata.name in ${jsonencode(local.sai20_authority_v4_protected_names)} || variables.targetMetadata.name.startsWith('fs2-db-writer-')"
        },
        {
          name = "custodian"
          expression = format(
            "has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s))",
            jsonencode(terraform_data.sai20_database_authority_v4_identity.output.executor_uid),
            jsonencode(terraform_data.sai20_database_authority_v4_identity.output.executor_username),
            length(jsondecode(terraform_data.sai20_database_authority_v4_identity.output.executor_groups_json)),
            terraform_data.sai20_database_authority_v4_identity.output.executor_groups_json,
            terraform_data.sai20_database_authority_v4_identity.output.executor_extra_json,
            terraform_data.sai20_database_authority_v4_identity.output.executor_extra_json,
          )
        },
      ]
      validations = [
        {
          expression = "!variables.protectedObject || request.operation != 'DELETE'"
          message    = "SAI-20 v4 authority objects are non-deletable"
          reason     = "Forbidden"
        },
        {
          expression = "!variables.protectedObject || variables.custodian"
          message    = "SAI-20 v4 authority-object mutation requires the freshly re-observed signed custodian"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [terraform_data.sai20_database_authority_v4_identity]
}

resource "kubernetes_manifest" "sai20_database_authority_object_custody_binding_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-authority-object-custody-binding-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_authority_object_custody_v4.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
      }
    }
  }
}

# Freeze the complete fs2-data NetworkPolicy set after the actual executor is
# proven but before the authoritative apply-time list is re-read. Existing
# objects and the planned canonical object may only retain their signed exact
# specs; alternate creates, mutations and all deletes are denied.
resource "kubernetes_manifest" "sai20_database_policy_freeze_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-policy-freeze-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
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
          name = "custodian"
          expression = format(
            "has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s))",
            jsonencode(terraform_data.sai20_database_authority_v4_identity.output.executor_uid),
            jsonencode(terraform_data.sai20_database_authority_v4_identity.output.executor_username),
            length(jsondecode(terraform_data.sai20_database_authority_v4_identity.output.executor_groups_json)),
            terraform_data.sai20_database_authority_v4_identity.output.executor_groups_json,
            terraform_data.sai20_database_authority_v4_identity.output.executor_extra_json,
            terraform_data.sai20_database_authority_v4_identity.output.executor_extra_json,
          )
        },
        {
          name       = "exactSignedPolicy"
          expression = local.sai20_authority_v4_transition_policy_cel
        },
      ]
      validations = [
        {
          expression = "request.operation != 'DELETE'"
          message    = "fs2-data NetworkPolicies are frozen during apply-time re-observation"
          reason     = "Forbidden"
        },
        {
          expression = "variables.custodian"
          message    = "the transition freeze admits only the freshly re-observed signed custodian"
          reason     = "Forbidden"
        },
        {
          expression = "variables.exactSignedPolicy"
          message    = "the transition freeze admits only exact source-rooted NetworkPolicy specs"
          reason     = "Forbidden"
        },
      ]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v4]
}

resource "kubernetes_manifest" "sai20_database_policy_freeze_binding_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-policy-freeze-binding-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_policy_freeze_v4.manifest.metadata.name
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

resource "kubernetes_config_map_v1" "sai20_database_ingress_contract_v4" {
  metadata {
    name      = "fs2-sai20-ingress-contract-v4"
    namespace = "fs2-data"
    labels = merge(local.common_labels, {
      "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
    })
    annotations = {
      "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_apply.output.bundle_sha256
      "security.fs2.nebius.ai/ingress-spec-sha256"     = local.sai20_authority_v4_ingress_spec_sha256
    }
  }
  immutable = true
  data = {
    "spec.json"   = jsonencode(local.sai20_authority_v4_ingress_contract)
    "spec.sha256" = local.sai20_authority_v4_ingress_spec_sha256
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v4]
}

resource "kubernetes_manifest" "sai20_database_ingress_exact_spec_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-ingress-exact-spec-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_apply.output.bundle_sha256
        "security.fs2.nebius.ai/ingress-spec-sha256"     = local.sai20_authority_v4_ingress_spec_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = ["networking.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE"]
          resources   = ["networkpolicies"]
          scope       = "Namespaced"
        }]
      }
      validations = [{
        expression = join(" ", [
          "object.metadata.name != 'fs2-control-db-ingress' ||",
          "(object.spec == ${jsonencode(local.sai20_authority_v4_ingress_contract)} &&",
          "has(object.metadata.annotations) &&",
          "'security.fs2.nebius.ai/ingress-spec-sha256' in object.metadata.annotations &&",
          "object.metadata.annotations['security.fs2.nebius.ai/ingress-spec-sha256'] == '${local.sai20_authority_v4_ingress_spec_sha256}' &&",
          "'security.fs2.nebius.ai/authority-bundle-v4' in object.metadata.annotations &&",
          "object.metadata.annotations['security.fs2.nebius.ai/authority-bundle-v4'] == '${terraform_data.sai20_database_authority_v4_apply.output.bundle_sha256}' &&",
          "'security.fs2.nebius.ai/ingress-contract-v4' in object.metadata.annotations &&",
          "object.metadata.annotations['security.fs2.nebius.ai/ingress-contract-v4'] == 'fs2-sai20-ingress-contract-v4')",
        ])
        message = "the canonical database NetworkPolicy must equal the source-owned v4 ingress contract"
        reason  = "Forbidden"
      }]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v4]
}

resource "kubernetes_manifest" "sai20_database_ingress_exact_spec_binding_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-ingress-exact-spec-binding-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_apply.output.bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_ingress_exact_spec_v4.manifest.metadata.name
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

# The v3 kind-only owner check remains as rejected historical evidence. This
# additive policy requires the exact signed live parent apiVersion/kind/name/UID
# and its exact controller username for every database-labelled child.
resource "kubernetes_manifest" "sai20_database_exact_owner_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-database-exact-owner-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
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
          name       = "releaseMutation"
          expression = local.sai20_authority_v3_release_mutation_cel
        },
        {
          name       = "controllerIdentity"
          expression = local.sai20_authority_v3_controller_cel
        },
        {
          name       = "exactPrincipalIdentity"
          expression = local.sai20_authority_v5_exact_principal_cel
        },
        {
          name       = "exactLiveParent"
          expression = local.sai20_authority_v4_exact_parent_cel
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
          expression = "variables.exactPrincipalIdentity && (variables.releaseMutation || (variables.controllerIdentity && ((!variables.databaseClient && variables.controllerOwnedChild) || (variables.databaseClient && variables.exactLiveParent))))"
          message    = "workload mutation requires an exact grant; database-labelled controller children additionally require an exact signed live parent name and UID"
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

  depends_on = [kubernetes_manifest.sai20_database_authority_object_custody_binding_v4]
}

resource "kubernetes_manifest" "sai20_database_exact_owner_binding_v4" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-database-exact-owner-binding-v4"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v4"
      })
      annotations = {
        "security.fs2.nebius.ai/authority-bundle-sha256" = terraform_data.sai20_database_authority_v4_identity.output.bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_database_exact_owner_v4.manifest.metadata.name
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

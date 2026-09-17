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
  sai20_authority_v5_debug_authorizer_contract_path = abspath(
    "${path.module}/contracts/sai20-debug-authorizer-v1.json"
  )
  sai20_authority_v5_common_query = merge(local.sai20_authority_v4_common_query, {
    successor_bundle_path                     = var.sai20_database_authority_v5.successor_bundle_path
    enrollment_authorities_path               = local.sai20_authority_v5_enrollment_authorities_path
    expected_enrollment_authorities_sha256     = filesha256(local.sai20_authority_v5_enrollment_authorities_path)
    root_enrollment_receipts_path              = local.sai20_authority_v5_root_enrollment_receipts_path
    expected_root_enrollment_receipts_sha256   = filesha256(local.sai20_authority_v5_root_enrollment_receipts_path)
    bootstrap_guard_contract_path              = local.sai20_authority_v5_bootstrap_guard_contract_path
    expected_bootstrap_guard_contract_sha256   = filesha256(local.sai20_authority_v5_bootstrap_guard_contract_path)
    debug_authorizer_contract_path             = local.sai20_authority_v5_debug_authorizer_contract_path
    expected_debug_authorizer_contract_sha256  = filesha256(local.sai20_authority_v5_debug_authorizer_contract_path)
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
    kubeconfig_path              = var.provider_kubeconfig_path
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
        data.external.sai20_database_authority_v5_identity.result.sealed_kubeconfig_sha256 == local.provider_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.executor_uid == terraform_data.sai20_database_authority_v5_plan.output.executor_uid &&
        data.external.sai20_database_authority_v5_identity.result.principal_identities_json == terraform_data.sai20_database_authority_v5_plan.output.principal_identities_json &&
        data.external.sai20_database_authority_v5_identity.result.rollout_lineages_json == terraform_data.sai20_database_authority_v5_plan.output.rollout_lineages_json &&
        data.external.sai20_database_authority_v5_identity.result.credential_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.credential_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.debug_access_leases_sha256 == terraform_data.sai20_database_authority_v5_plan.output.debug_access_leases_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.debug_authorizer_sha256 == terraform_data.sai20_database_authority_v5_plan.output.debug_authorizer_sha256 &&
        data.external.sai20_database_authority_v5_identity.result.workload_create_contracts_sha256 == terraform_data.sai20_database_authority_v5_plan.output.workload_create_contracts_sha256 &&
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
  sai20_authority_v5_protected_service_accounts = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.protected_service_accounts_json
  )
  sai20_authority_v5_protected_secrets = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.protected_secret_names_json
  )
  sai20_authority_v5_protected_workload_parents = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.protected_workload_parents_json
  )
  sai20_authority_v5_protected_workload_objects = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.protected_workload_objects_json
  )
  sai20_authority_v5_workload_controller_identities = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.workload_controller_identities_json
  )
  sai20_authority_v5_workload_mutation_grants = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.workload_mutation_grants_json
  )
  sai20_authority_v5_workload_create_contracts = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.workload_create_contracts_json
  )
  sai20_authority_v5_principal_identities = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.principal_identities_json
  )
  sai20_authority_v5_debug_access_bindings = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.debug_access_bindings_json
  )
  sai20_authority_v5_debug_authorizer = jsondecode(
    terraform_data.sai20_database_authority_v5_identity.output.debug_authorizer_json
  )
  sai20_authority_v5_debug_broker_identity = one([
    for principal in local.sai20_authority_v5_principal_identities : principal
    if principal.id == terraform_data.sai20_database_authority_v5_identity.output.debug_broker_principal_id
  ])
  sai20_authority_v5_debug_broker_cel = format(
    "has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s))",
    jsonencode(local.sai20_authority_v5_debug_broker_identity.uid),
    jsonencode(local.sai20_authority_v5_debug_broker_identity.username),
    length(local.sai20_authority_v5_debug_broker_identity.groups),
    jsonencode(local.sai20_authority_v5_debug_broker_identity.groups),
    jsonencode(local.sai20_authority_v5_debug_broker_identity.extra),
    jsonencode(local.sai20_authority_v5_debug_broker_identity.extra),
  )
  sai20_authority_v5_service_accounts_by_namespace = {
    for namespace in ["cnpg-system", "fs2-data", "fs2-observability", "fs2-system"] :
    namespace => [
      for item in local.sai20_authority_v5_protected_service_accounts : item.name
      if item.namespace == namespace
    ]
  }
  sai20_authority_v5_secrets_by_namespace = {
    for namespace in ["cnpg-system", "fs2-data", "fs2-observability", "fs2-system"] :
    namespace => [
      for item in local.sai20_authority_v5_protected_secrets : item.name
      if item.namespace == namespace
    ]
  }
  sai20_authority_v5_exact_workload_writer_terms = flatten([
    for principal in local.sai20_authority_v5_workload_mutation_grants : [
      for grant in principal.grants : format(
        "(has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)) && request.namespace == %s && request.resource.resource == %s && request.operation in %s && variables.targetObject.metadata.name in %s)",
        jsonencode(principal.identity.uid),
        jsonencode(principal.identity.username),
        length(principal.identity.groups),
        jsonencode(principal.identity.groups),
        jsonencode(principal.identity.extra),
        jsonencode(principal.identity.extra),
        jsonencode(grant.namespace),
        jsonencode(grant.resource),
        jsonencode(grant.operations),
        jsonencode(grant.names),
      )
    ]
  ])
  sai20_authority_v5_exact_workload_writer_cel = length(local.sai20_authority_v5_exact_workload_writer_terms) == 0 ? "false" : join(" || ", local.sai20_authority_v5_exact_workload_writer_terms)
  sai20_authority_v5_exact_workload_create_terms = [
    for contract in local.sai20_authority_v5_workload_create_contracts : format(
      "(has(request.userInfo.uid) && request.userInfo.uid == %s && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)) && request.operation == 'CREATE' && request.namespace == %s && request.resource.resource == %s && variables.targetObject.metadata.name == %s && variables.targetSpec == %s)",
      jsonencode(contract.identity.uid),
      jsonencode(contract.identity.username),
      length(contract.identity.groups),
      jsonencode(contract.identity.groups),
      jsonencode(contract.identity.extra),
      jsonencode(contract.identity.extra),
      jsonencode(contract.namespace),
      jsonencode(contract.resource),
      jsonencode(contract.name),
      jsonencode(contract.pod_spec),
    )
  ]
  sai20_authority_v5_exact_workload_create_cel = length(local.sai20_authority_v5_exact_workload_create_terms) == 0 ? "false" : join(" || ", local.sai20_authority_v5_exact_workload_create_terms)
  sai20_authority_v5_controller_owned_credential_child_terms = [
    for parent in local.sai20_authority_v5_protected_workload_parents : format(
      "((%s) && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)) && request.operation == 'CREATE' && request.namespace == %s && request.resource.resource == %s && has(variables.targetObject.metadata.ownerReferences) && variables.targetObject.metadata.ownerReferences.exists(owner, has(owner.controller) && owner.controller && owner.apiVersion == %s && owner.kind == %s && owner.name == %s && string(owner.uid) == %s) && variables.targetServiceAccount == %s && variables.targetProtectedSecrets == %s && %s)",
      parent.controller_uid == "" ? "!has(request.userInfo.uid)" : format("has(request.userInfo.uid) && request.userInfo.uid == %s", jsonencode(parent.controller_uid)),
      jsonencode(parent.controller_username),
      length(parent.controller_groups),
      jsonencode(parent.controller_groups),
      jsonencode(parent.controller_extra),
      jsonencode(parent.controller_extra),
      jsonencode(parent.namespace),
      jsonencode(parent.child_resource),
      jsonencode(parent.api_version),
      jsonencode(parent.kind),
      jsonencode(parent.name),
      jsonencode(parent.uid),
      jsonencode(parent.service_account_name),
      jsonencode(parent.protected_secret_names),
      parent.resource == "deployments" ? "has(variables.targetObject.spec.replicas) && variables.targetObject.spec.replicas == 0" : parent.resource == "cronjobs" ? "has(variables.targetObject.spec.suspend) && variables.targetObject.spec.suspend" : "true",
    )
  ]
  sai20_authority_v5_controller_owned_credential_child_cel = length(local.sai20_authority_v5_controller_owned_credential_child_terms) == 0 ? "false" : join(" || ", local.sai20_authority_v5_controller_owned_credential_child_terms)
  sai20_authority_v5_controller_managed_credential_object_terms = [
    for item in local.sai20_authority_v5_protected_workload_objects : format(
      "((%s) && request.userInfo.username == %s && request.userInfo.groups.size() == %d && request.userInfo.groups.all(group, group in %s) && ((%s == {} && !has(request.userInfo.extra)) || (has(request.userInfo.extra) && request.userInfo.extra == %s)) && request.operation == 'UPDATE' && request.namespace == %s && request.resource.resource == %s && variables.targetObject.metadata.name == %s && string(variables.targetObject.metadata.uid) == %s && variables.targetServiceAccount == %s && variables.targetProtectedSecrets == %s && variables.unchangedCredentialSurface)",
      item.controller_uid == "" ? "!has(request.userInfo.uid)" : format("has(request.userInfo.uid) && request.userInfo.uid == %s", jsonencode(item.controller_uid)),
      jsonencode(item.controller_username),
      length(item.controller_groups),
      jsonencode(item.controller_groups),
      jsonencode(item.controller_extra),
      jsonencode(item.controller_extra),
      jsonencode(item.namespace),
      jsonencode(item.resource),
      jsonencode(item.name),
      jsonencode(item.uid),
      jsonencode(item.service_account_name),
      jsonencode(item.protected_secret_names),
    )
  ]
  sai20_authority_v5_controller_managed_credential_object_cel = length(local.sai20_authority_v5_controller_managed_credential_object_terms) == 0 ? "false" : join(" || ", local.sai20_authority_v5_controller_managed_credential_object_terms)
  sai20_authority_v5_exact_debug_lease_cel = length(local.sai20_authority_v5_debug_access_bindings) == 0 ? "false" : join(" || ", [
    for lease in local.sai20_authority_v5_debug_access_bindings : format(
      "(request.namespace == %s && variables.targetObject.metadata.name == %s && has(variables.targetObject.metadata.annotations) && %s.all(key, variables.targetObject.metadata.annotations[key] == %s[key]) && ((request.resource.resource == 'roles' && variables.targetObject.rules == %s) || (request.resource.resource == 'rolebindings' && variables.targetObject.roleRef == %s && variables.targetObject.subjects == %s)))",
      jsonencode(lease.namespace),
      jsonencode(lease.name),
      jsonencode(keys(lease.annotations)),
      jsonencode(lease.annotations),
      jsonencode(lease.rules),
      jsonencode(lease.role_ref),
      jsonencode(lease.subjects),
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

# A writer in a platform namespace must not turn an otherwise innocuous Pod or
# controller into a credential proxy by selecting a protected ServiceAccount or
# mounting a protected same-namespace Secret. Exact release writers may retain,
# but not change, an existing credential surface. Native controllers may create
# only an exact-owner child with the parent's signed surface; Deployment and
# CronJob intermediates are admitted inert and require a fresh signed renewal
# before they can create Pods.
resource "kubernetes_manifest" "sai20_workload_credential_custody_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-workload-credential-custody-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/credential-inventory-sha256" = terraform_data.sai20_database_authority_v5_identity.output.credential_workload_inventory_sha256
        "security.fs2.nebius.ai/debug-leases-sha256"         = terraform_data.sai20_database_authority_v5_identity.output.debug_access_leases_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256"     = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
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
          name       = "targetObject"
          expression = "object"
        },
        {
          name = "targetSpec"
          expression = join(" ", [
            "request.resource.resource == 'pods' ? object.spec :",
            "request.resource.resource == 'cronjobs' ? object.spec.jobTemplate.spec.template.spec :",
            "object.spec.template.spec",
          ])
        },
        {
          name = "oldTargetSpec"
          expression = join(" ", [
            "request.operation != 'UPDATE' ? variables.targetSpec :",
            "request.resource.resource == 'pods' ? oldObject.spec :",
            "request.resource.resource == 'cronjobs' ? oldObject.spec.jobTemplate.spec.template.spec :",
            "oldObject.spec.template.spec",
          ])
        },
        {
          name       = "namespaceProtectedServiceAccounts"
          expression = "${jsonencode(local.sai20_authority_v5_service_accounts_by_namespace)}[request.namespace]"
        },
        {
          name       = "namespaceProtectedSecrets"
          expression = "${jsonencode(local.sai20_authority_v5_secrets_by_namespace)}[request.namespace]"
        },
        {
          name       = "targetServiceAccount"
          expression = "has(variables.targetSpec.serviceAccountName) && variables.targetSpec.serviceAccountName != '' ? variables.targetSpec.serviceAccountName : 'default'"
        },
        {
          name       = "oldServiceAccount"
          expression = "has(variables.oldTargetSpec.serviceAccountName) && variables.oldTargetSpec.serviceAccountName != '' ? variables.oldTargetSpec.serviceAccountName : 'default'"
        },
        {
          name = "targetProtectedSecrets"
          expression = join(" ", [
            "variables.namespaceProtectedSecrets.filter(secret,",
            "(has(variables.targetSpec.imagePullSecrets) && variables.targetSpec.imagePullSecrets.exists(ref, ref.name == secret)) ||",
            "(has(variables.targetSpec.volumes) && variables.targetSpec.volumes.exists(volume,",
            "(has(volume.secret) && volume.secret.secretName == secret) ||",
            "(has(volume.projected) && volume.projected.sources.exists(source, has(source.secret) && source.secret.name == secret)))) ||",
            "variables.targetSpec.containers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret))) ||",
            "(has(variables.targetSpec.initContainers) && variables.targetSpec.initContainers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret)))) ||",
            "(has(variables.targetSpec.ephemeralContainers) && variables.targetSpec.ephemeralContainers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret)))))",
          ])
        },
        {
          name = "oldProtectedSecrets"
          expression = join(" ", [
            "variables.namespaceProtectedSecrets.filter(secret,",
            "(has(variables.oldTargetSpec.imagePullSecrets) && variables.oldTargetSpec.imagePullSecrets.exists(ref, ref.name == secret)) ||",
            "(has(variables.oldTargetSpec.volumes) && variables.oldTargetSpec.volumes.exists(volume,",
            "(has(volume.secret) && volume.secret.secretName == secret) ||",
            "(has(volume.projected) && volume.projected.sources.exists(source, has(source.secret) && source.secret.name == secret)))) ||",
            "variables.oldTargetSpec.containers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret))) ||",
            "(has(variables.oldTargetSpec.initContainers) && variables.oldTargetSpec.initContainers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret)))) ||",
            "(has(variables.oldTargetSpec.ephemeralContainers) && variables.oldTargetSpec.ephemeralContainers.exists(container,",
            "(has(container.envFrom) && container.envFrom.exists(source, has(source.secretRef) && source.secretRef.name == secret)) ||",
            "(has(container.env) && container.env.exists(env, has(env.valueFrom) && has(env.valueFrom.secretKeyRef) && env.valueFrom.secretKeyRef.name == secret)))))",
          ])
        },
        {
          name       = "credentialBearing"
          expression = "variables.targetServiceAccount in variables.namespaceProtectedServiceAccounts || variables.targetProtectedSecrets.size() > 0"
        },
        {
          name       = "unchangedCredentialSurface"
          expression = "request.operation == 'UPDATE' && variables.targetServiceAccount == variables.oldServiceAccount && variables.targetProtectedSecrets == variables.oldProtectedSecrets"
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
          name       = "exactWorkloadWriter"
          expression = local.sai20_authority_v5_exact_workload_writer_cel
        },
        {
          name       = "exactWorkloadCreate"
          expression = local.sai20_authority_v5_exact_workload_create_cel
        },
        {
          name       = "controllerOwnedCredentialChild"
          expression = local.sai20_authority_v5_controller_owned_credential_child_cel
        },
        {
          name       = "controllerManagedCredentialObject"
          expression = local.sai20_authority_v5_controller_managed_credential_object_cel
        },
      ]
      validations = [{
        expression = "!variables.credentialBearing || variables.custodian || variables.exactWorkloadCreate || (variables.exactWorkloadWriter && variables.unchangedCredentialSurface) || variables.controllerOwnedCredentialChild || variables.controllerManagedCredentialObject"
        message    = "protected ServiceAccount or Secret selection requires the exact custodian, an exact signed writer retaining the surface, or an exact native-controller transition"
        reason     = "Forbidden"
      }]
    }
  }

  depends_on = [kubernetes_manifest.sai20_database_peer_identity_binding_v5]
}

resource "kubernetes_manifest" "sai20_workload_credential_custody_binding_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-workload-credential-custody-binding-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/credential-inventory-sha256" = terraform_data.sai20_database_authority_v5_identity.output.credential_workload_inventory_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256"     = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_workload_credential_custody_v6.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = ["cnpg-system", "fs2-data", "fs2-observability", "fs2-system"]
          }]
        }
      }
    }
  }
}

# RBAC in credential-bearing platform namespaces is custodian-only except for
# the source-defined debug broker. The broker can create only an exact-name
# Pod-subresource Role/RoleBinding pair carrying auditable tenant, target UID,
# reason and <=15-minute lease annotations; the signed v5 gate independently
# re-derives each active pair and its exact SAR decisions before activation.
resource "kubernetes_manifest" "sai20_debug_access_custody_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-debug-access-custody-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/debug-leases-sha256"     = terraform_data.sai20_database_authority_v5_identity.output.debug_access_leases_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256" = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = ["rbac.authorization.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["roles", "rolebindings"]
          scope       = "Namespaced"
        }]
      }
      variables = [
        {
          name       = "targetObject"
          expression = "request.operation == 'DELETE' ? oldObject : object"
        },
        {
          name       = "broker"
          expression = local.sai20_authority_v5_debug_broker_cel
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
          name = "leaseMetadata"
          expression = join(" ", [
            "variables.targetObject.metadata.name.startsWith('fs2-debug-lease-') &&",
            "has(variables.targetObject.metadata.annotations) &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-audit-id') &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-tenant-id') &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-pod-uid') &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-reason-sha256') &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-issued-at') &&",
            "variables.targetObject.metadata.annotations.exists(key, key == 'security.fs2.nebius.ai/debug-expires-at') &&",
            "timestamp(variables.targetObject.metadata.annotations['security.fs2.nebius.ai/debug-expires-at']) > timestamp(variables.targetObject.metadata.annotations['security.fs2.nebius.ai/debug-issued-at']) &&",
            "timestamp(variables.targetObject.metadata.annotations['security.fs2.nebius.ai/debug-expires-at']) - timestamp(variables.targetObject.metadata.annotations['security.fs2.nebius.ai/debug-issued-at']) <= duration('900s')",
          ])
        },
        {
          name = "exactDebugRole"
          expression = join(" ", [
            "request.resource.resource == 'roles' && variables.targetObject.rules.size() > 0 &&",
            "variables.targetObject.rules.all(rule, rule.apiGroups == [''] && rule.resources.size() == 1 &&",
            "rule.resources[0] in ['pods/exec','pods/attach','pods/portforward','pods/proxy','pods/ephemeralcontainers'] &&",
            "has(rule.resourceNames) && rule.resourceNames.size() == 1 && rule.verbs.size() > 0 &&",
            "rule.verbs.all(verb, verb in {'pods/exec':['create','get'],'pods/attach':['create','get'],'pods/portforward':['create','get'],'pods/proxy':['create','delete','get','patch','update'],'pods/ephemeralcontainers':['patch','update']}[rule.resources[0]]))",
          ])
        },
        {
          name = "exactDebugBinding"
          expression = join(" ", [
            "request.resource.resource == 'rolebindings' &&",
            "variables.targetObject.roleRef.apiGroup == 'rbac.authorization.k8s.io' && variables.targetObject.roleRef.kind == 'Role' &&",
            "variables.targetObject.roleRef.name == variables.targetObject.metadata.name && variables.targetObject.subjects.size() == 1 &&",
            "variables.targetObject.subjects[0].kind in ['User','ServiceAccount'] &&",
            "(!has(variables.targetObject.subjects[0].namespace) || variables.targetObject.subjects[0].namespace == request.namespace)",
          ])
        },
        {
          name       = "exactSignedDebugLease"
          expression = local.sai20_authority_v5_exact_debug_lease_cel
        },
      ]
      validations = [{
        expression = "variables.custodian || (variables.broker && variables.leaseMetadata && variables.exactSignedDebugLease && (variables.exactDebugRole || variables.exactDebugBinding))"
        message    = "platform-namespace RBAC requires the exact custodian or the source-rendered bounded audited exact-target debug lease from the exact broker"
        reason     = "Forbidden"
      }]
    }
  }

  depends_on = [kubernetes_manifest.sai20_workload_credential_custody_binding_v6]
}

resource "kubernetes_manifest" "sai20_debug_access_custody_binding_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-debug-access-custody-binding-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/debug-leases-sha256"     = terraform_data.sai20_database_authority_v5_identity.output.debug_access_leases_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256" = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_debug_access_custody_v6.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources = {
        matchPolicy = "Equivalent"
        namespaceSelector = {
          matchExpressions = [{
            key      = "kubernetes.io/metadata.name"
            operator = "In"
            values   = ["cnpg-system", "fs2-data", "fs2-observability", "fs2-system"]
          }]
        }
      }
    }
  }
}

# RBAC annotations are not an authorization clock. Every protected Pod
# subresource request therefore also crosses this independently operated,
# dual-signed HTTPS authorizer. It validates the exact target, identity, epoch
# and expiry on every request; failure or timeout denies access.
resource "kubernetes_manifest" "sai20_debug_request_authorizer_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingWebhookConfiguration"
    metadata = {
      name = "fs2-debug-request-authorizer-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/debug-authorizer-sha256" = terraform_data.sai20_database_authority_v5_identity.output.debug_authorizer_sha256
        "security.fs2.nebius.ai/debug-leases-sha256"     = terraform_data.sai20_database_authority_v5_identity.output.debug_access_leases_sha256
        "security.fs2.nebius.ai/protected-pods-sha256"   = local.sai20_authority_v5_debug_authorizer.protected_pod_targets_sha256
        "security.fs2.nebius.ai/server-spki-sha256"      = local.sai20_authority_v5_debug_authorizer.server_spki_sha256
      }
    }
    webhooks = [{
      name                    = "debug-request.sai20.security.fs2.nebius.ai"
      admissionReviewVersions = ["v1"]
      failurePolicy           = "Fail"
      matchPolicy             = "Equivalent"
      reinvocationPolicy      = "Never"
      sideEffects             = "None"
      timeoutSeconds          = 2
      clientConfig = {
        url      = local.sai20_authority_v5_debug_authorizer.url
        caBundle = local.sai20_authority_v5_debug_authorizer.ca_bundle_base64
      }
      namespaceSelector = {
        matchExpressions = [{
          key      = "kubernetes.io/metadata.name"
          operator = "In"
          values   = ["cnpg-system", "fs2-data", "fs2-observability", "fs2-system"]
        }]
      }
      rules = [
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CONNECT"]
          resources   = ["pods/attach", "pods/exec", "pods/portforward", "pods/proxy"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["UPDATE"]
          resources   = ["pods/ephemeralcontainers"]
          scope       = "Namespaced"
        },
      ]
    }]
  }

  depends_on = [kubernetes_manifest.sai20_debug_access_custody_binding_v6]
}

# Cluster-scoped RBAC is an alternate path to every namespaced Pod
# subresource. Freeze all future ClusterRole and ClusterRoleBinding mutation
# behind the same freshly re-observed exact custodian; ordinary namespace-local
# tenant RBAC remains outside this policy.
resource "kubernetes_manifest" "sai20_cluster_rbac_custody_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicy"
    metadata = {
      name = "fs2-cluster-rbac-custody-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/cluster-authority-sha256" = terraform_data.sai20_database_authority_v5_identity.output.cluster_authority_review_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256"  = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
      }
    }
    spec = {
      failurePolicy = "Fail"
      matchConstraints = {
        matchPolicy = "Equivalent"
        resourceRules = [{
          apiGroups   = ["rbac.authorization.k8s.io"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["clusterroles", "clusterrolebindings"]
          scope       = "Cluster"
        }]
      }
      variables = [{
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
      }]
      validations = [{
        expression = "variables.custodian"
        message    = "cluster-scoped RBAC mutation requires the freshly re-observed exact custodian"
        reason     = "Forbidden"
      }]
    }
  }

  depends_on = [kubernetes_manifest.sai20_debug_request_authorizer_v6]
}

resource "kubernetes_manifest" "sai20_cluster_rbac_custody_binding_v6" {
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingAdmissionPolicyBinding"
    metadata = {
      name = "fs2-cluster-rbac-custody-binding-v6"
      labels = merge(local.common_labels, {
        "security.fs2.nebius.ai/database-network-custody" = "sai20-v6"
      })
      annotations = {
        "security.fs2.nebius.ai/cluster-authority-sha256" = terraform_data.sai20_database_authority_v5_identity.output.cluster_authority_review_sha256
        "security.fs2.nebius.ai/successor-bundle-sha256"  = terraform_data.sai20_database_authority_v5_identity.output.successor_bundle_sha256
      }
    }
    spec = {
      policyName        = kubernetes_manifest.sai20_cluster_rbac_custody_v6.manifest.metadata.name
      validationActions = ["Deny"]
      matchResources    = { matchPolicy = "Equivalent" }
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
    kubernetes_manifest.sai20_workload_credential_custody_v6,
    kubernetes_manifest.sai20_workload_credential_custody_binding_v6,
    kubernetes_manifest.sai20_debug_access_custody_v6,
    kubernetes_manifest.sai20_debug_access_custody_binding_v6,
    kubernetes_manifest.sai20_debug_request_authorizer_v6,
    kubernetes_manifest.sai20_cluster_rbac_custody_v6,
    kubernetes_manifest.sai20_cluster_rbac_custody_binding_v6,
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
    kubeconfig_path              = var.provider_kubeconfig_path
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
      kubernetes_manifest.sai20_workload_credential_custody_v6.manifest,
      kubernetes_manifest.sai20_workload_credential_custody_binding_v6.manifest,
      kubernetes_manifest.sai20_debug_access_custody_v6.manifest,
      kubernetes_manifest.sai20_debug_access_custody_binding_v6.manifest,
      kubernetes_manifest.sai20_debug_request_authorizer_v6.manifest,
      kubernetes_manifest.sai20_cluster_rbac_custody_v6.manifest,
      kubernetes_manifest.sai20_cluster_rbac_custody_binding_v6.manifest,
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
        data.external.sai20_database_authority_v5_apply.result.sealed_kubeconfig_sha256 == local.provider_kubeconfig_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_transition_mode == terraform_data.sai20_database_authority_v5_identity.output.successor_transition_mode &&
        data.external.sai20_database_authority_v5_apply.result.successor_old_objects_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_old_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_planned_objects_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.successor_activation_sha256 == terraform_data.sai20_database_authority_v5_identity.output.successor_planned_objects_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.executor_uid == terraform_data.sai20_database_authority_v5_identity.output.executor_uid &&
        data.external.sai20_database_authority_v5_apply.result.principal_identities_json == terraform_data.sai20_database_authority_v5_identity.output.principal_identities_json &&
        data.external.sai20_database_authority_v5_apply.result.rollout_lineages_json == terraform_data.sai20_database_authority_v5_identity.output.rollout_lineages_json &&
        data.external.sai20_database_authority_v5_apply.result.credential_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.credential_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.debug_access_leases_sha256 == terraform_data.sai20_database_authority_v5_identity.output.debug_access_leases_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.debug_authorizer_sha256 == terraform_data.sai20_database_authority_v5_identity.output.debug_authorizer_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.workload_create_contracts_sha256 == terraform_data.sai20_database_authority_v5_identity.output.workload_create_contracts_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.peer_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_identity.output.peer_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.cluster_authority_review_sha256 == terraform_data.sai20_database_authority_v5_plan.output.cluster_authority_review_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.namespace_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.namespace_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.service_account_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.service_account_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.secret_metadata_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.secret_metadata_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.secret_names_json == terraform_data.sai20_database_authority_v5_plan.output.secret_names_json &&
        data.external.sai20_database_authority_v5_apply.result.credential_workload_inventory_sha256 == terraform_data.sai20_database_authority_v5_plan.output.credential_workload_inventory_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.debug_access_leases_sha256 == terraform_data.sai20_database_authority_v5_plan.output.debug_access_leases_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.debug_authorizer_sha256 == terraform_data.sai20_database_authority_v5_plan.output.debug_authorizer_sha256 &&
        data.external.sai20_database_authority_v5_apply.result.workload_create_contracts_sha256 == terraform_data.sai20_database_authority_v5_plan.output.workload_create_contracts_sha256 &&
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

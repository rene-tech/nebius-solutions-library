data "external" "authority" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_authority_ledger.py",
  ]
  query = {
    authority_manifest_json = var.authority_manifest_json
  }
}

data "external" "provider_identity" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_provider_identity.py",
  ]
  query = {
    profile = var.security_owner_nebius_profile
  }
}

data "external" "backend_custody" {
  program = [
    "uv",
    "run",
    "--frozen",
    "--project",
    "${path.module}/../../components/control-plane",
    "python",
    "${path.module}/verify_backend_custody.py",
  ]
  query = { module_path = path.module }
}

locals {
  authority            = jsondecode(data.external.authority.result.manifest_json)
  generations          = jsondecode(data.external.authority.result.generations_json)
  retained_generations = jsondecode(data.external.authority.result.retained_generations_json)
  all_generations      = merge(local.retained_generations, local.generations)
  controller_identities = jsondecode(data.external.authority.result.controller_identities_json)
  legacy_provider_generations = {
    for generation, value in local.retained_generations : generation => value
    if !can(value.provisioning_generation)
  }
  # Stable lanes are created only by the separately signed provisioning root.
  # Keep the never-activated in-root blocks below inert so this successor does
  # not erase source while also preventing an attestation/provisioning cycle.
  stable_provider_generations = {}
  scheduling_keys = merge(
    {
      for generation in keys(local.retained_generations) :
      generation => try(
        local.retained_generations[generation].scheduling_key,
        "workload.fs2.nebius/customer-storage-egress",
      )
    },
    {
      for generation, value in local.generations :
      generation => value.scheduling_key
    },
  )
  scheduling_values = merge(
    {
      for generation in keys(local.retained_generations) :
      generation => try(local.retained_generations[generation].lane_id, generation)
    },
    {
      for generation, value in local.generations :
      generation => value.lane_id
    },
  )
  retained_authority_gate_generations = setunion(
    toset(jsondecode(data.external.authority.result.prior_authority_gate_generations_json)),
    toset(keys(local.generations)),
  )
  common_labels = {
    "managed-by"         = "fs2-security-owner"
    "security-boundary"  = "customer-storage-egress"
    "authority-manifest" = substr(data.external.authority.result.manifest_sha256, 0, 16)
  }
}

resource "terraform_data" "external_authority" {
  input = {
    manifest_sha256               = data.external.authority.result.manifest_sha256
    prior_head_receipt_sha256     = data.external.authority.result.prior_head_receipt_sha256
    authority_project_id          = data.external.authority.result.authority_project_id
    authority_service_account_id  = data.external.authority.result.authority_service_account_id
    authority_group_id            = data.external.authority.result.authority_group_id
    workloads_service_account_id  = data.external.authority.result.workloads_service_account_id
    kubernetes_identity_inventory = data.external.authority.result.kubernetes_identity_inventory_sha256
    kubernetes_service_accounts   = data.external.authority.result.kubernetes_service_account_inventory_sha256
    kubernetes_system_subjects    = data.external.authority.result.kubernetes_system_subject_inventory_sha256
    controller_identities         = data.external.authority.result.controller_identities_json
    controller_audit_receipt      = data.external.authority.result.controller_audit_receipt_sha256
    daemonset_inventory           = data.external.authority.result.daemonset_inventory_sha256
    daemonset_list_resource_version = data.external.authority.result.daemonset_list_resource_version
    daemonset_admission_fence     = data.external.authority.result.daemonset_admission_fence_receipt_sha256
    daemonset_snapshot_ledger     = data.external.authority.result.daemonset_snapshot_ledger_head_sha256
    kubernetes_rbac_inventory     = data.external.authority.result.kubernetes_rbac_inventory_sha256
    kubernetes_rbac_receipt       = data.external.authority.result.kubernetes_rbac_inventory_receipt_sha256
    accepted_sai10_commit         = data.external.authority.result.accepted_sai10_commit
    accepted_sai10_tree           = data.external.authority.result.accepted_sai10_tree
    sai10_review_receipt_sha256   = data.external.authority.result.sai10_independent_review_receipt_sha256
    provider_identity_sha256      = data.external.provider_identity.result.provider_identity_sha256
    provider_authority_graph      = data.external.authority.result.provider_effective_authority_graph_receipt_sha256
    provider_state_custody        = data.external.authority.result.provider_state_custody_sha256
    boundary_state_custody        = data.external.authority.result.boundary_state_custody_sha256
    provider_backend_config       = data.external.backend_custody.result.backend_config_sha256
    provider_backend_lineage      = data.external.backend_custody.result.backend_lineage
    provider_state_lineage        = data.external.backend_custody.result.state_lineage
    provider_state_serial         = data.external.backend_custody.result.state_serial
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
    precondition {
      condition     = data.external.authority.result.authorized == "true"
      error_message = "The root-owned external provider authority did not accept this exact signed ledger."
    }
    precondition {
      condition     = data.external.provider_identity.result.authorized == "true"
      error_message = "The Nebius provider profile is not the exact narrow external authority."
    }
    precondition {
      condition     = data.external.backend_custody.result.authorized == "true"
      error_message = "The initialized Terraform backend is not the externally custodied provider-state lineage."
    }
    precondition {
      condition     = local.authority.authority_project_id == data.external.authority.result.authority_project_id
      error_message = "The provider authority project differs from the root-owned approval registry."
    }
    precondition {
      condition = (
        local.authority.accepted_custody.sai10_commit == data.external.authority.result.accepted_sai10_commit &&
        local.authority.accepted_custody.sai10_tree == data.external.authority.result.accepted_sai10_tree &&
        local.authority.accepted_custody.independent_review_receipt_sha256 == data.external.authority.result.sai10_independent_review_receipt_sha256
      )
      error_message = "The provider authority manifest does not carry the externally accepted SAI-10 custody."
    }
  }
}

# Preserve the original singleton address without updating it. Every successor
# gets a content-named custody gate of its own, so later manifests add state
# instead of rewriting the authority record used by an earlier generation.
resource "terraform_data" "external_authority_v4" {
  for_each = local.retained_authority_gate_generations

  input = {
    generation                                 = each.key
    generation_sha256                          = sha256(jsonencode(local.all_generations[each.key]))
    manifest_sha256                            = data.external.authority.result.manifest_sha256
    prior_head_receipt_sha256                  = data.external.authority.result.prior_head_receipt_sha256
    provider_identity_sha256                   = data.external.provider_identity.result.provider_identity_sha256
    provider_authority_graph_receipt_sha256    = data.external.authority.result.provider_effective_authority_graph_receipt_sha256
    provider_authority_adapter_sha256          = data.external.authority.result.provider_authority_adapter_sha256
    kubernetes_rbac_inventory_receipt_sha256   = data.external.authority.result.kubernetes_rbac_inventory_receipt_sha256
    kubernetes_rbac_effective_authority_sha256 = data.external.authority.result.kubernetes_rbac_effective_authority_sha256
    provider_backend_config_sha256             = data.external.backend_custody.result.backend_config_sha256
    provider_backend_lineage                   = data.external.backend_custody.result.backend_lineage
    provider_state_lineage                     = data.external.backend_custody.result.state_lineage
    provider_state_serial                      = data.external.backend_custody.result.state_serial
    provider_state_version_id                  = data.external.backend_custody.result.state_version_id
    provider_state_snapshot_sha256             = data.external.backend_custody.result.state_snapshot_sha256
    provider_state_managed_addresses_sha256    = data.external.backend_custody.result.managed_addresses_sha256
    accepted_sai10_commit                      = data.external.authority.result.accepted_sai10_commit
    accepted_sai10_tree                        = data.external.authority.result.accepted_sai10_tree
    sai10_independent_review_receipt_sha256    = data.external.authority.result.sai10_independent_review_receipt_sha256
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.external_authority]
}

resource "nebius_vpc_v1_security_group" "generation" {
  for_each = local.legacy_provider_generations

  parent_id  = local.authority.authority_project_id
  network_id = local.authority.network_id
  name       = "fs2-storage-egress-${each.key}"
  labels     = merge(local.common_labels, { generation = each.key })

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.external_authority_v4]
}

resource "nebius_vpc_v1_security_rule" "private_ingress" {
  for_each = local.legacy_provider_generations

  parent_id = nebius_vpc_v1_security_group.generation[each.key].id
  name      = "fs2-storage-private-${each.key}"
  labels    = merge(local.common_labels, { generation = each.key, purpose = "private-ingress" })
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100
  ingress = {
    source_cidrs      = each.value.private_cidrs
    destination_ports = []
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "dns_egress" {
  for_each = local.legacy_provider_generations

  parent_id = nebius_vpc_v1_security_group.generation[each.key].id
  name      = "fs2-storage-dns-${each.key}"
  labels    = merge(local.common_labels, { generation = each.key, purpose = "dns-egress" })
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = each.value.private_cidrs
    destination_ports = [53]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "database_egress" {
  for_each = local.legacy_provider_generations

  parent_id = nebius_vpc_v1_security_group.generation[each.key].id
  name      = "fs2-storage-db-${each.key}"
  labels    = merge(local.common_labels, { generation = each.key, purpose = "database-egress" })
  access    = "ALLOW"
  protocol  = "TCP"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = each.value.private_cidrs
    destination_ports = [5432]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "provider_egress" {
  for_each = local.legacy_provider_generations

  parent_id = nebius_vpc_v1_security_group.generation[each.key].id
  name      = "fs2-storage-provider-${each.key}"
  labels    = merge(local.common_labels, { generation = each.key, purpose = "provider-egress" })
  access    = "ALLOW"
  protocol  = "TCP"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = sort(distinct(concat(
      each.value.provider_api_cidrs,
      each.value.kubernetes_api_cidrs,
      each.value.bootstrap_https_cidrs,
    )))
    destination_ports = [443]
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_mk8s_v1_node_group" "generation" {
  for_each = local.legacy_provider_generations

  parent_id        = local.authority.cluster_id
  name             = "fs2-storage-egress-${each.key}"
  labels           = merge(local.common_labels, { generation = each.key })
  version          = local.authority.kubernetes_version
  fixed_node_count = null
  autoscaling = {
    min_node_count = try(each.value.min_node_count, 0)
    max_node_count = try(each.value.max_node_count, 1)
  }

  strategy = {
    max_surge       = { count = 0 }
    max_unavailable = { count = 0 }
    drain_timeout   = "30m"
  }

  template = {
    metadata = {
      labels = merge({
        "fs2.nebius.ai/authority-manifest-sha256" = data.external.authority.result.manifest_sha256
      }, { (local.scheduling_keys[each.key]) = local.scheduling_values[each.key] })
    }
    taints = [{
      key    = local.scheduling_keys[each.key]
      value  = local.scheduling_values[each.key]
      effect = "NO_SCHEDULE"
    }]
    boot_disk = {
      size_gibibytes = each.value.boot_disk_gib
      type           = each.value.boot_disk_type
    }
    network_interfaces = [{
      subnet_id = local.authority.subnet_id
      security_groups = [{
        id = nebius_vpc_v1_security_group.generation[each.key].id
      }]
    }]
    os                 = "ubuntu24.04"
    reservation_policy = { policy = "FORBID" }
    resources = {
      platform = each.value.platform
      preset   = each.value.preset
    }
    service_account_id = local.authority.node_service_account_id
    underlay_required  = false
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [
    nebius_vpc_v1_security_rule.private_ingress,
    nebius_vpc_v1_security_rule.dns_egress,
    nebius_vpc_v1_security_rule.database_egress,
    nebius_vpc_v1_security_rule.provider_egress,
  ]
}

# Stable provider provisioning is deliberately independent from the signed
# post-creation Node/controller/agent attestation generation.  Retained legacy
# addresses above remain untouched; every v6+ lane is keyed by the digest of
# provider inputs only, so refreshing live attestations never replaces a VPC
# security group or NodeGroup.
resource "nebius_vpc_v1_security_group" "stable_lane" {
  for_each = local.stable_provider_generations

  parent_id  = local.authority.authority_project_id
  network_id = local.authority.network_id
  name       = "fs2-storage-lane-${each.key}"
  labels = {
    "managed-by"              = "fs2-security-owner"
    "security-boundary"       = "customer-storage-egress"
    "provisioning-generation" = each.key
    "lane-id"                 = each.value.lane_id
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }

  depends_on = [terraform_data.external_authority_v4]
}

resource "nebius_vpc_v1_security_rule" "stable_private_ingress" {
  for_each = local.stable_provider_generations

  parent_id = nebius_vpc_v1_security_group.stable_lane[each.key].id
  name      = "fs2-storage-private-${each.key}"
  labels    = { "provisioning-generation" = each.key, purpose = "private-ingress" }
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100
  ingress = {
    source_cidrs       = each.value.private_cidrs
    destination_ports = []
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "stable_dns_egress" {
  for_each = local.stable_provider_generations

  parent_id = nebius_vpc_v1_security_group.stable_lane[each.key].id
  name      = "fs2-storage-dns-${each.key}"
  labels    = { "provisioning-generation" = each.key, purpose = "dns-egress" }
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = each.value.private_cidrs
    destination_ports = [53]
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "stable_database_egress" {
  for_each = local.stable_provider_generations

  parent_id = nebius_vpc_v1_security_group.stable_lane[each.key].id
  name      = "fs2-storage-db-${each.key}"
  labels    = { "provisioning-generation" = each.key, purpose = "database-egress" }
  access    = "ALLOW"
  protocol  = "TCP"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = each.value.private_cidrs
    destination_ports = [5432]
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_vpc_v1_security_rule" "stable_provider_egress" {
  for_each = local.stable_provider_generations

  parent_id = nebius_vpc_v1_security_group.stable_lane[each.key].id
  name      = "fs2-storage-provider-${each.key}"
  labels    = { "provisioning-generation" = each.key, purpose = "provider-egress" }
  access    = "ALLOW"
  protocol  = "TCP"
  type      = "STATEFUL"
  priority  = 100
  egress = {
    destination_cidrs = sort(distinct(concat(
      each.value.provider_api_cidrs,
      each.value.kubernetes_api_cidrs,
      each.value.bootstrap_https_cidrs,
    )))
    destination_ports = [443]
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "nebius_mk8s_v1_node_group" "stable_lane" {
  for_each = local.stable_provider_generations

  parent_id        = local.authority.cluster_id
  name             = "fs2-storage-lane-${each.key}"
  labels           = { "provisioning-generation" = each.key, "lane-id" = each.value.lane_id }
  version          = local.authority.kubernetes_version
  fixed_node_count = null
  autoscaling = {
    min_node_count = each.value.min_node_count
    max_node_count = each.value.max_node_count
  }
  strategy = {
    max_surge       = { count = 0 }
    max_unavailable = { count = 0 }
    drain_timeout   = "30m"
  }
  template = {
    metadata = {
      labels = {
        (each.value.scheduling_key)                 = each.value.lane_id
        "fs2.nebius.ai/provisioning-generation" = each.key
      }
    }
    taints = [{
      key    = each.value.scheduling_key
      value  = each.value.lane_id
      effect = "NO_SCHEDULE"
    }]
    boot_disk = {
      size_gibibytes = each.value.boot_disk_gib
      type           = each.value.boot_disk_type
    }
    network_interfaces = [{
      subnet_id = local.authority.subnet_id
      security_groups = [{
        id = nebius_vpc_v1_security_group.stable_lane[each.key].id
      }]
    }]
    os                 = "ubuntu24.04"
    reservation_policy = { policy = "FORBID" }
    resources = {
      platform = each.value.platform
      preset   = each.value.preset
    }
    service_account_id = local.authority.node_service_account_id
    underlay_required  = false
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [
    nebius_vpc_v1_security_rule.stable_private_ingress,
    nebius_vpc_v1_security_rule.stable_dns_egress,
    nebius_vpc_v1_security_rule.stable_database_egress,
    nebius_vpc_v1_security_rule.stable_provider_egress,
  ]
}

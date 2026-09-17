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
  authority   = jsondecode(data.external.authority.result.manifest_json)
  generations = jsondecode(data.external.authority.result.generations_json)
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
    generation_sha256                          = try(sha256(jsonencode(local.generations[each.key])), "retained-by-prior-state-custody")
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
  for_each = local.generations

  parent_id  = local.authority.authority_project_id
  network_id = local.authority.network_id
  name       = "fs2-storage-egress-${each.key}"
  labels     = merge(local.common_labels, { generation = each.key })

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [terraform_data.external_authority_v4]
}

resource "nebius_vpc_v1_security_rule" "private_ingress" {
  for_each = local.generations

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
  }
}

resource "nebius_vpc_v1_security_rule" "dns_egress" {
  for_each = local.generations

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
  }
}

resource "nebius_vpc_v1_security_rule" "database_egress" {
  for_each = local.generations

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
  }
}

resource "nebius_vpc_v1_security_rule" "provider_egress" {
  for_each = local.generations

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
  }
}

resource "nebius_mk8s_v1_node_group" "generation" {
  for_each = local.generations

  parent_id        = local.authority.cluster_id
  name             = "fs2-storage-egress-${each.key}"
  labels           = merge(local.common_labels, { generation = each.key })
  version          = local.authority.kubernetes_version
  fixed_node_count = 1

  strategy = {
    max_surge       = { count = 1 }
    max_unavailable = { count = 0 }
    drain_timeout   = "30m"
  }

  template = {
    metadata = {
      labels = {
        "workload.fs2.nebius/customer-storage-egress" = each.key
        "fs2.nebius.ai/authority-manifest-sha256"     = data.external.authority.result.manifest_sha256
      }
    }
    taints = [{
      key    = "workload.fs2.nebius/customer-storage-egress"
      value  = each.key
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
  }

  depends_on = [
    nebius_vpc_v1_security_rule.private_ingress,
    nebius_vpc_v1_security_rule.dns_egress,
    nebius_vpc_v1_security_rule.database_egress,
    nebius_vpc_v1_security_rule.provider_egress,
  ]
}

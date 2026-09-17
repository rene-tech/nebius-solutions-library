data "external" "provisioning" {
  program = [
    "uv", "run", "--frozen", "--project",
    "${path.module}/../../components/control-plane", "python",
    "${path.module}/verify_provisioning_manifest.py",
  ]
  query = { manifest_json = var.provisioning_manifest_json }
}

data "external" "provider_identity" {
  program = [
    "uv", "run", "--frozen", "--project",
    "${path.module}/../../components/control-plane", "python",
    "${path.module}/../customer-storage-egress-authority/verify_provider_identity.py",
  ]
  query = { profile = var.security_owner_nebius_profile }
}

locals {
  lanes              = jsondecode(data.external.provisioning.result.generations_json)
  current_generation = data.external.provisioning.result.current_generation
  labels = {
    for generation, lane in local.lanes : generation => {
      "managed-by"              = "fs2-lane-security-owner"
      "security-boundary"       = "customer-storage-egress"
      "provisioning-generation" = generation
      "lane-id"                 = lane.lane_id
    }
  }
}

resource "terraform_data" "signed_provisioning" {
  for_each = local.lanes
  input = {
    manifest_sha256         = data.external.provisioning.result.manifest_sha256
    provisioning_generation = each.key
    provider_identity_sha256 = data.external.provider_identity.result.provider_identity_sha256
    daemonset_admission_fence_receipt_sha256 = data.external.provisioning.result.daemonset_admission_fence_receipt_sha256
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
    precondition {
      condition     = data.external.provisioning.result.authorized == "true"
      error_message = "The external lane owner did not authorize this provider-only generation."
    }
    precondition {
      condition     = data.external.provider_identity.result.authorized == "true"
      error_message = "The lane provisioning profile is not the exact narrow external authority."
    }
  }
}

resource "nebius_vpc_v1_security_group" "lane" {
  for_each = local.lanes

  parent_id  = each.value.authority_project_id
  network_id = each.value.network_id
  name       = "fs2-storage-lane-${each.key}"
  labels     = local.labels[each.key]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
  depends_on = [terraform_data.signed_provisioning]
}

resource "nebius_vpc_v1_security_rule" "private_ingress" {
  for_each = local.lanes

  parent_id = nebius_vpc_v1_security_group.lane[each.key].id
  name      = "fs2-storage-private-${each.key}"
  labels    = merge(local.labels[each.key], { purpose = "private-ingress" })
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

resource "nebius_vpc_v1_security_rule" "dns_egress" {
  for_each = local.lanes

  parent_id = nebius_vpc_v1_security_group.lane[each.key].id
  name      = "fs2-storage-dns-${each.key}"
  labels    = merge(local.labels[each.key], { purpose = "dns-egress" })
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
  for_each = local.lanes

  parent_id = nebius_vpc_v1_security_group.lane[each.key].id
  name      = "fs2-storage-db-${each.key}"
  labels    = merge(local.labels[each.key], { purpose = "database-egress" })
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
  for_each = local.lanes

  parent_id = nebius_vpc_v1_security_group.lane[each.key].id
  name      = "fs2-storage-provider-${each.key}"
  labels    = merge(local.labels[each.key], { purpose = "provider-egress" })
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

resource "nebius_mk8s_v1_node_group" "lane" {
  for_each = local.lanes

  parent_id        = each.value.cluster_id
  name             = "fs2-storage-lane-${each.key}"
  labels           = local.labels[each.key]
  version          = each.value.kubernetes_version
  fixed_node_count = null
  autoscaling = {
    min_node_count = each.value.min_node_count
    max_node_count = each.value.max_node_count
  }
  strategy = {
    # One generation never repairs or replaces its attested member in place.
    # A new lane/NodeGroup may be prepared additively, but cutover and
    # predecessor retirement require a separately reviewed lifecycle protocol.
    max_surge       = { count = 0 }
    max_unavailable = { count = 0 }
    drain_timeout   = "30m"
  }
  template = {
    metadata = {
      labels = {
        (each.value.scheduling_key)              = each.value.lane_id
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
      subnet_id = each.value.subnet_id
      security_groups = [{ id = nebius_vpc_v1_security_group.lane[each.key].id }]
    }]
    os                 = "ubuntu24.04"
    reservation_policy = { policy = "FORBID" }
    resources = {
      platform = each.value.platform
      preset   = each.value.preset
    }
    service_account_id = each.value.node_service_account_id
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

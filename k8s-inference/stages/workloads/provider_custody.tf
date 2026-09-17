# This provider read is the authoritative network-side half of custody. A
# signed JSON assertion cannot substitute for it: every live phase refreshes
# the managed-cluster object and proves the API server admits only the external
# gateway hosts. The gateway itself supplies the independently challenged
# object-level freeze verified by inference-stack.
data "nebius_mk8s_v1_cluster" "model_network_provider_custody" {
  count = var.model_network_provider_trust_root_sha256 == "" ? 0 : 1
  id    = var.cluster_id
}

data "nebius_compute_v1_instance" "model_network_provider_gateway" {
  for_each = var.model_network_provider_trust_root_sha256 == "" ? {} : var.model_network_provider_gateway_members
  id       = each.value.instance_id
}

data "nebius_vpc_v1_security_group" "model_network_provider_gateway" {
  for_each = var.model_network_provider_trust_root_sha256 == "" ? {} : var.model_network_provider_gateway_members
  id       = each.value.security_group_id
}

locals {
  model_network_provider_security_rule_ids = {
    for item in flatten([
      for member_id, member in var.model_network_provider_gateway_members : [
        for id in member.security_rule_ids : { id = id, member_id = member_id }
      ]
    ]) : item.id => item
  }
  model_network_provider_access_permit_ids = {
    for item in flatten([
      for member_id, member in var.model_network_provider_gateway_members : [
        for id in member.iam_access_permit_ids : { id = id, member_id = member_id }
      ]
    ]) : item.id => item
  }
}

data "nebius_vpc_v1_security_rule" "model_network_provider_gateway" {
  for_each = var.model_network_provider_trust_root_sha256 == "" ? {} : local.model_network_provider_security_rule_ids
  id       = each.key
}

data "nebius_iam_v1_access_permit" "model_network_provider_gateway" {
  for_each = var.model_network_provider_trust_root_sha256 == "" ? {} : local.model_network_provider_access_permit_ids
  id       = each.key
}

locals {
  live_model_network_cluster_resource_version = (
    var.model_network_provider_trust_root_sha256 == "" ? null :
    data.nebius_mk8s_v1_cluster.model_network_provider_custody[0].metadata.resource_version
  )
  live_model_network_control_plane_allowed_cidrs = (
    var.model_network_provider_trust_root_sha256 == "" ? [] :
    sort(tolist(data.nebius_mk8s_v1_cluster.model_network_provider_custody[0].control_plane.endpoints.public_endpoint.allowed_cidrs))
  )
  live_model_network_provider_gateway_members = var.model_network_provider_trust_root_sha256 == "" ? [] : [
    for member_id in sort(keys(var.model_network_provider_gateway_members)) : {
      member_id                 = member_id
      status_url                = var.model_network_provider_gateway_members[member_id].status_url
      host_cidr                 = var.model_network_provider_gateway_members[member_id].host_cidr
      listener_address          = var.model_network_provider_gateway_members[member_id].listener_address
      listener_port             = var.model_network_provider_gateway_members[member_id].listener_port
      server_certificate_sha256 = var.model_network_provider_gateway_members[member_id].server_certificate_sha256
      iam_principal_id           = var.model_network_provider_gateway_members[member_id].iam_principal_id
      instance = {
        id               = var.model_network_provider_gateway_members[member_id].instance_id
        resource_version = data.nebius_compute_v1_instance.model_network_provider_gateway[member_id].metadata.resource_version
        semantic_sha256  = sha256(jsonencode(data.nebius_compute_v1_instance.model_network_provider_gateway[member_id]))
      }
      security_group = {
        id               = var.model_network_provider_gateway_members[member_id].security_group_id
        resource_version = data.nebius_vpc_v1_security_group.model_network_provider_gateway[member_id].metadata.resource_version
        semantic_sha256  = sha256(jsonencode(data.nebius_vpc_v1_security_group.model_network_provider_gateway[member_id]))
      }
      security_rules = [
        for id in sort(tolist(var.model_network_provider_gateway_members[member_id].security_rule_ids)) : {
          id               = id
          resource_version = data.nebius_vpc_v1_security_rule.model_network_provider_gateway[id].metadata.resource_version
          semantic_sha256  = sha256(jsonencode(data.nebius_vpc_v1_security_rule.model_network_provider_gateway[id]))
        }
      ]
      access_permits = [
        for id in sort(tolist(var.model_network_provider_gateway_members[member_id].iam_access_permit_ids)) : {
          id               = id
          resource_version = data.nebius_iam_v1_access_permit.model_network_provider_gateway[id].metadata.resource_version
          semantic_sha256  = sha256(jsonencode(data.nebius_iam_v1_access_permit.model_network_provider_gateway[id]))
        }
      ]
    }
  ]
  live_model_network_provider_inventory_sha256 = var.model_network_provider_trust_root_sha256 == "" ? null : sha256(jsonencode({
    cluster = {
      id               = var.cluster_id
      resource_version = local.live_model_network_cluster_resource_version
      allowed_cidrs    = local.live_model_network_control_plane_allowed_cidrs
    }
    members = local.live_model_network_provider_gateway_members
  }))
}

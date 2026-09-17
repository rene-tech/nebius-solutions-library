locals {
  edge_client_identity_receipt_path = "${local.normalized_run_root}/${var.public_edge_client_identity_receipt.filename}"
  edge_client_identity_expected_subject = {
    schema     = "fs2-serve.nebius.ai/edge-client-identity-terraform-subject/v1"
    provider   = "nebius"
    project_id = nonsensitive(var.project_id)
    cluster_id = var.cluster_id
    allocation = {
      id           = var.public_edge_contract.allocation_id
      ipv4_address = var.public_edge_contract.public_ipv4_address
    }
    network = {
      id        = var.public_edge_contract.network_id
      subnet_id = var.public_edge_contract.subnet_id
    }
    security_group = {
      id                = var.public_edge_contract.worker_security_group_id
      ingress_rule_id   = var.public_edge_contract.public_edge_ingress_rule_id
      source_cidrs      = sort(var.public_edge_contract.security_group_source_cidrs)
      destination_ports = var.public_edge_contract.security_group_destination_ports
    }
    gateway = {
      namespace  = "fs2-system"
      name       = "public"
      class_name = "fs2-serve-public"
      listeners = [
        { name = "acme-http", protocol = "HTTP", port = 80 },
        { name = "public-https", protocol = "HTTPS", port = 443 },
      ]
    }
    service_ports = var.public_edge_contract.service_ports
  }
}

# Public client identity is derived only from a freshly authenticated receipt.
# The program owns the source-pinned trust registry; neither this query nor any
# deployment variable can provide a key, a verification verdict, a hop count,
# or a direct-access assertion.
data "external" "edge_client_identity_receipt" {
  count = local.public_edge_enabled ? 1 : 0

  program = [
    local.public_edge_gate_launcher_path,
    "edge-client-identity-verifier",
    "external",
  ]

  query = {
    receipt_path          = local.edge_client_identity_receipt_path
    expected_subject_json = jsonencode(local.edge_client_identity_expected_subject)
  }
}

locals {
  verified_edge_client_identity = local.public_edge_enabled ? {
    verified                 = try(one(data.external.edge_client_identity_receipt[*].result.verified) == "true", false)
    trusted_hops             = try(tonumber(one(data.external.edge_client_identity_receipt[*].result.trusted_hops)), 0)
    provider_contract_sha256 = try(one(data.external.edge_client_identity_receipt[*].result.payload_sha256), "")
    receipt_sha256           = try(one(data.external.edge_client_identity_receipt[*].result.receipt_sha256), "")
    issuer_key_id            = try(one(data.external.edge_client_identity_receipt[*].result.issuer_key_id), "")
    provider_load_balancer_id = try(
      one(data.external.edge_client_identity_receipt[*].result.provider_load_balancer_id),
      "",
    )
    direct_access_excluded = try(
      one(data.external.edge_client_identity_receipt[*].result.direct_access_excluded) == "true",
      false,
    )
    per_source_connection_limit = try(
      tonumber(one(data.external.edge_client_identity_receipt[*].result.per_source_connection_limit)),
      0,
    )
  } : {
    verified                  = false
    trusted_hops              = 0
    provider_contract_sha256  = ""
    receipt_sha256            = ""
    issuer_key_id             = ""
    provider_load_balancer_id = ""
    direct_access_excluded    = false
    per_source_connection_limit = 0
  }
}

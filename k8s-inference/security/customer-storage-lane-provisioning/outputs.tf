output "receipt_seed" {
  description = "Non-secret provider-only facts for the external owner to verify and sign before any Node attestation generation."
  value = {
    schema                  = "fs2-serve.nebius.ai/protected-lane-provisioning-receipt/v2"
    provisioning_generation = local.current_generation
    authority_project_id     = local.lanes[local.current_generation].authority_project_id
    cluster_id               = local.lanes[local.current_generation].cluster_id
    security_group_id        = nebius_vpc_v1_security_group.lane[local.current_generation].id
    node_group_id            = nebius_mk8s_v1_node_group.lane[local.current_generation].id
    bootstrap_node_count     = 1
    custody_capture_command  = "capture_provisioning_receipt.py --manifest <signed-manifest> --generation ${local.current_generation}"
  }
}

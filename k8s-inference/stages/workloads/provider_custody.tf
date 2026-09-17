# This provider read is the authoritative network-side half of custody. A
# signed JSON assertion cannot substitute for it: every live phase refreshes
# the managed-cluster object and proves the API server admits only the external
# gateway hosts. The gateway itself supplies the independently challenged
# object-level freeze verified by inference-stack.
data "nebius_mk8s_v1_cluster" "model_network_provider_custody" {
  count = var.model_network_provider_trust_root_sha256 == "" ? 0 : 1
  id    = var.cluster_id
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
}

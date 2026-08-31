resource "nebius_vpc_v1_security_group" "workers" {
  parent_id  = var.project_id
  network_id = data.nebius_vpc_v1_network.target.id
  name       = "${local.resource_name}-workers"
  labels     = merge(local.common_labels, { purpose = "worker-nodes" })

  depends_on = [terraform_data.target_contract]
}

resource "nebius_vpc_v1_security_rule" "workers_private_ingress" {
  parent_id = nebius_vpc_v1_security_group.workers.id
  name      = "${local.resource_name}-private"
  labels    = merge(local.common_labels, { purpose = "private-ingress" })
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100

  ingress = {
    source_cidrs      = sort(tolist(local.target_subnet_private_cidrs))
    destination_ports = []
  }
}

resource "nebius_vpc_v1_security_rule" "workers_public_edge_ingress" {
  count = var.public_edge_mode == "public" ? 1 : 0

  parent_id = nebius_vpc_v1_security_group.workers.id
  name      = "${local.resource_name}-public-edge"
  labels    = merge(local.common_labels, { purpose = "public-edge" })
  access    = "ALLOW"
  protocol  = "TCP"
  type      = "STATEFUL"
  priority  = 90

  ingress = {
    source_cidrs = var.public_edge_source_cidrs
    destination_ports = [
      var.public_edge_service_ports.http.listener_port,
      var.public_edge_service_ports.https.listener_port,
      var.public_edge_service_ports.http.target_port,
      var.public_edge_service_ports.https.target_port,
      var.public_edge_service_ports.http.node_port,
      var.public_edge_service_ports.https.node_port,
    ]
  }
}

resource "nebius_vpc_v1_security_rule" "workers_egress" {
  parent_id = nebius_vpc_v1_security_group.workers.id
  name      = "${local.resource_name}-egress"
  labels    = merge(local.common_labels, { purpose = "worker-egress" })
  access    = "ALLOW"
  protocol  = "ANY"
  type      = "STATEFUL"
  priority  = 100

  egress = {
    destination_cidrs = ["0.0.0.0/0"]
    destination_ports = []
  }
}


resource "nebius_vpc_v1_allocation" "gateway" {
  count = var.public_edge_mode == "public" ? 1 : 0

  parent_id = var.project_id
  name      = "${local.resource_name}-gateway-public"
  labels    = merge(local.common_labels, { purpose = "gateway" })

  ipv4_public = {
    cidr      = "/32"
    subnet_id = data.nebius_vpc_v1_subnet.target.id
  }

  depends_on = [terraform_data.target_contract]
}

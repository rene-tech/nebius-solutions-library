mock_provider "nebius" {
  mock_data "nebius_iam_v2_project" {
    defaults = {
      id        = "project-syntheticlocal"
      parent_id = "tenant-syntheticlocal"
      name      = "synthetic-local-project"
      region    = "us-north1"
      status    = { project_state = "ACTIVE" }
    }
  }
  mock_data "nebius_vpc_v1_network" {
    defaults = {
      id     = "vpcnetwork-syntheticlocal"
      name   = "synthetic-network"
      status = { state = "READY" }
    }
  }
  mock_data "nebius_vpc_v1_subnet" {
    defaults = {
      id         = "vpcsubnet-syntheticlocal"
      name       = "synthetic-subnet"
      network_id = "vpcnetwork-syntheticlocal"
      status = {
        state              = "READY"
        ipv4_private_cidrs = ["10.104.0.0/13"]
        ipv4_private_pools = {
          cidrs   = ["10.104.0.0/13"]
          pool_id = "vpcpool-syntheticlocal"
        }
      }
    }
  }
  mock_resource "nebius_mk8s_v1_cluster" {
    defaults = { id = "mk8scluster-syntheticlocal" }
  }
  mock_resource "nebius_mk8s_v1_node_group" {
    defaults = { id = "mk8snodegroup-syntheticlocal" }
  }
  mock_resource "nebius_compute_v1_filesystem" {
    defaults = { id = "computefilesystem-syntheticlocal" }
  }
  mock_resource "nebius_storage_v1_bucket" {
    defaults = { id = "storagebucket-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_service_account" {
    defaults = { id = "serviceaccount-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_group" {
    defaults = { id = "group-syntheticlocal" }
  }
  mock_resource "nebius_iam_v1_group_membership" {
    defaults = { id = "groupmembership-syntheticlocal" }
  }
  mock_resource "nebius_iam_v2_access_key" {
    defaults = { id = "accesskey-syntheticlocal" }
  }
  mock_resource "nebius_vpc_v1_security_group" {
    defaults = { id = "vpcsecuritygroup-syntheticlocal" }
  }
  mock_resource "nebius_registry_v1_registry" {
    defaults = { id = "registry-syntheticlocal" }
  }
}

variables {
  project_id                  = "project-syntheticlocal"
  source_commit               = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id                      = "systest1"
  control_plane_allowed_cidrs = ["192.0.2.1/32"]

  target_binding = {
    project_id          = "project-syntheticlocal"
    project_name        = "synthetic-local-project"
    region              = "us-north1"
    network_name        = "synthetic-network"
    subnet_name         = "synthetic-subnet"
    private_subnet_cidr = "10.104.0.0/13"
    system_update_strategy = {
      max_surge       = 1
      max_unavailable = 0
    }
  }

  public_edge_mode         = "internal-only"
  public_edge_source_cidrs = []
}

run "system_nodes_persist_and_load_the_default_inotify_ceiling" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.system]
  }

  assert {
    condition = (
      strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "path: /etc/sysctl.d/99-fs2-system-inotify.conf") &&
      strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "fs.inotify.max_user_instances = 8192") &&
      strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "- [sysctl, -p, /etc/sysctl.d/99-fs2-system-inotify.conf]")
    )
    error_message = "System-node cloud-init must persist the default inotify ceiling under /etc/sysctl.d and load that exact file during first boot."
  }

}

run "system_nodes_honor_a_bounded_tfvars_override" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.system]
  }

  variables {
    system_pool = {
      node_count                             = 3
      inotify_max_user_instances             = 16384
      three_node_ha_cost_review_acknowledged = true
    }
  }

  assert {
    condition = (
      local.effective_system_pool.inotify_max_user_instances == 16384 &&
      strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "fs.inotify.max_user_instances = 16384")
    )
    error_message = "A valid system_pool.inotify_max_user_instances tfvars override must reach the rendered node template."
  }
}

run "an_unsafe_low_inotify_ceiling_is_rejected" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.system]
  }

  variables {
    system_pool = {
      node_count                             = 3
      inotify_max_user_instances             = 128
      three_node_ha_cost_review_acknowledged = true
    }
  }

  expect_failures = [var.system_pool]
}

run "a_universal_control_plane_allowlist_is_rejected" {
  command = plan

  variables {
    control_plane_allowed_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.control_plane_allowed_cidrs]
}

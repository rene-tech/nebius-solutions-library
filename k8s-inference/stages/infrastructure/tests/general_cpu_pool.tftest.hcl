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
  project_id    = "project-syntheticlocal"
  source_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  run_id        = "gputest1"

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
  cpu_pools = {
    general-cpu-8x = {
      platform      = "cpu-d3"
      preset        = "8vcpu-32gb"
      capacity_type = "preemptible"
      min_nodes     = 0
      max_nodes     = 4
      elastic       = true
      schedulable_capacity = {
        cpu_millicores        = 7000
        memory_mib            = 28672
        ephemeral_storage_mib = 114688
      }
      boot_disk = {
        type     = "NETWORK_SSD"
        size_gib = 160
      }
      shared_filesystem = false
      node_labels       = {}
      max_surge         = 1
      max_unavailable   = 0
      drain_timeout     = "15m"
    }
  }
}

run "an_elastic_pool_scales_from_zero_and_stays_off_the_reference_plane" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.general_cpu]
  }

  # Elastic capacity is an autoscaling envelope, not a fixed count.
  assert {
    condition = (
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].autoscaling.min_node_count == 0 &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].autoscaling.max_node_count == 4 &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].fixed_node_count == null
    )
    error_message = "An elastic general CPU pool must plan a zero-floor autoscaling envelope."
  }

  # Its own taint and pool identity, so nothing else lands here by accident and
  # the admitted ResourceFlavor can name the actual node group.
  assert {
    condition = (
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.taints[0].key == "workload.fs2.nebius/general-cpu" &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.taints[0].effect == "NO_SCHEDULE" &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.metadata.labels["capacity.fs2.nebius/pool"] == "general-cpu" &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.metadata.labels["capacity.fs2.nebius/pool-id"] == "general-cpu-8x"
    )
    error_message = "A general CPU pool must carry its own NoSchedule taint and pool identity labels."
  }

  # The reference-data plane is a separate owner: no mount, no label, and the
  # preemptible capacity mode is honoured.
  assert {
    condition = (
      length(nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.filesystems) == 0 &&
      !contains(keys(nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.metadata.labels), "storage.fs2.nebius/reference-data") &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.preemptible != null
    )
    error_message = "A general CPU pool must never mount or advertise the reference-data filesystem, and must honour its capacity mode."
  }

  # Sizing comes only from tfvars.
  assert {
    condition = (
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.resources.platform == "cpu-d3" &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.resources.preset == "8vcpu-32gb" &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-8x"].template.boot_disk.size_gibibytes == 160
    )
    error_message = "Platform, preset and boot disk must come from tfvars alone."
  }
}

run "a_fixed_pool_pins_one_node_count" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.general_cpu]
  }

  variables {
    cpu_pools = {
      general-cpu-fixed = {
        platform      = "cpu-d3"
        preset        = "8vcpu-32gb"
        capacity_type = "regular"
        min_nodes     = 2
        max_nodes     = 2
        elastic       = false
        schedulable_capacity = {
          cpu_millicores        = 7000
          memory_mib            = 28672
          ephemeral_storage_mib = 114688
        }
        boot_disk = {
          type     = "NETWORK_SSD"
          size_gib = 160
        }
        shared_filesystem = false
        node_labels       = {}
        max_surge         = 1
        max_unavailable   = 0
        drain_timeout     = "15m"
      }
    }
  }

  assert {
    condition = (
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-fixed"].fixed_node_count == 2 &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-fixed"].autoscaling == null &&
      nebius_mk8s_v1_node_group.general_cpu["general-cpu-fixed"].template.preemptible == null
    )
    error_message = "A fixed regular pool must plan one pinned node count and no preemptible template."
  }
}

run "no_cpu_pool_creates_no_node_group" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.general_cpu]
  }

  variables {
    cpu_pools = {}
  }

  # The general CPU pool is entirely opt-in: an unset map creates nothing, and
  # the reference-data and system pools are untouched.
  assert {
    condition     = length(nebius_mk8s_v1_node_group.general_cpu) == 0
    error_message = "A deployment without general CPU pools must create no general CPU node group."
  }
}

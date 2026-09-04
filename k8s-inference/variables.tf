variable "deployment" {
  description = <<-EOT
    Complete customer-facing FS2 deployment configuration. Hardware and model
    IDs resolve through checked-in qualification catalogs; provider facts,
    chart pins, generated cluster identities, and credentials are intentionally
    not customer inputs.
  EOT

  type = object({
    schema_version = optional(number, 1)
    name           = string

    profiles = optional(object({
      capacity     = optional(string, "minimal")
      accelerators = optional(string)
      models       = optional(string, "minimal")
    }), {})

    target = object({
      project_id   = string
      project_name = optional(string)
      region       = string
      network = optional(object({
        network_name        = optional(string)
        subnet_name         = optional(string)
        private_subnet_cidr = optional(string)
      }), {})
      system_update_strategy = optional(object({
        max_surge       = number
        max_unavailable = number
      }))
    })

    cluster = optional(object({
      kubernetes_version          = optional(string, "1.35")
      control_plane_allowed_cidrs = optional(set(string), [])
      system_pool = optional(object({
        capacity        = optional(string, "regular")
        platform        = optional(string, "cpu-d3")
        preset          = optional(string, "8vcpu-32gb")
        node_count      = optional(number)
        boot_disk_type  = optional(string, "NETWORK_SSD")
        boot_disk_gib   = optional(number, 160)
        max_surge       = optional(number)
        max_unavailable = optional(number)
        drain_timeout   = optional(string, "15m")
      }))
    }), {})

    accelerator_pool_capacity = optional(map(object({
      min_nodes = number
      max_nodes = number
    })), {})

    accelerator_pools = optional(map(object({
      platform          = string
      preset            = string
      accelerator_class = string
      gpus_per_node     = number
      resource_name     = optional(string, "nvidia.com/gpu")
      # Measured per-node Kubernetes allocatable cpu and memory, after the
      # kubelet, system and DaemonSet reserve. Required for every selected
      # pool once scheduling.core_capacity is set, because a preset's nominal
      # size is not schedulable capacity and a quota derived from it
      # over-admits. Declared per pool, the same way the CPU pools declare it.
      schedulable_capacity = optional(object({
        cpu_millicores = number
        memory_mib     = number
        # Where the number came from, so a reviewer can check it rather than
        # take it on trust. A bare pair of integers in tfvars is a claim; this
        # is a claim with an origin, a time and a digest of the payload it was
        # read from.
        evidence = object({
          pool_id        = string
          node_group_id  = optional(string)
          source         = string
          captured_at    = string
          payload_sha256 = string
        })
      }))
      gpu_memory_gb     = optional(number)
      host_architecture = optional(string, "amd64")
      capacity_type     = optional(string, "preemptible")
      min_nodes         = optional(number, 0)
      max_nodes         = optional(number, 1)
      reservation_policy = optional(object({
        policy          = optional(string, "STRICT")
        reservation_ids = optional(list(string), [])
      }))
      os = optional(string, "ubuntu24.04")
      driver = optional(object({
        mode   = optional(string, "managed")
        preset = optional(string)
      }), {})
      boot_disk = optional(object({
        type     = optional(string, "NETWORK_SSD")
        size_gib = optional(number, 320)
      }), {})
      local_nvme                = optional(bool, false)
      local_nvme_mode           = optional(string, "raw")
      shared_filesystem         = optional(bool, true)
      reference_data_filesystem = optional(bool, false)
      drain_timeout             = optional(string, "30m")
      topology = optional(object({
        mode              = optional(string, "standalone")
        infiniband_fabric = optional(string)
        rack_count        = optional(number, 0)
        nodes_per_rack    = optional(number, 18)
      }), {})
      mig = optional(object({
        strategy = optional(string, "none")
        config   = optional(string)
      }), {})
    })), {})

    # Elastic general-purpose CPU pools for scientific preprocessing and
    # aggregation stages that do not need the dedicated reference-data nodes.
    # Each pool is provisioned entirely from this contract: no project, region,
    # accelerator or live node identity is ever hard-coded. A pool declares
    # either fixed capacity (fixed_nodes) or an autoscaling envelope, and its
    # measured per-node schedulable capacity after Kubernetes/DaemonSet reserve;
    # the general-cpu Kueue lane derives its quotas from exactly these numbers.
    # General pools never mount or label the reference-data filesystem and are
    # never a fallback for the reference-data or system pools.
    cpu_pools = optional(map(object({
      platform      = string
      preset        = string
      capacity_type = optional(string, "preemptible")
      # Exactly one of fixed_nodes or autoscaling selects the capacity mode.
      fixed_nodes = optional(number)
      autoscaling = optional(object({
        min_nodes = optional(number, 0)
        max_nodes = number
      }))
      # Conservative measured per-node capacity after system reserve. There is
      # no invented preset-derived default: an operator states what one node of
      # this class actually schedules, and the lane quotas stay truthful.
      schedulable_capacity = object({
        cpu_millicores        = number
        memory_mib            = number
        ephemeral_storage_mib = number
      })
      boot_disk = optional(object({
        type     = optional(string, "NETWORK_SSD")
        size_gib = optional(number, 160)
      }), {})
      # Opt-in mount of the general shared cache filesystem. The reference-data
      # filesystem is deliberately not an option here.
      shared_filesystem = optional(bool, false)
      # Extra node labels. Reserved fs2 scheduling prefixes are rejected so a
      # general pool can never masquerade as reference-data, system or
      # accelerator capacity.
      node_labels     = optional(map(string), {})
      max_surge       = optional(number, 1)
      max_unavailable = optional(number, 0)
      drain_timeout   = optional(string, "15m")
    })), {})

    models = optional(object({
      selection       = optional(string, "profile")
      enabled         = optional(set(string), [])
      image_overrides = optional(map(string), {})
      pool_overrides  = optional(map(string), {})
      scaling = optional(object({
        mode                     = optional(string, "keda")
        hot                      = optional(set(string), [])
        polling_interval_seconds = optional(number, 5)
        cooldown_period_seconds  = optional(number, 300)
        overrides = optional(map(object({
          min_replicas             = number
          max_replicas             = number
          target_queue_depth       = number
          polling_interval_seconds = number
          cooldown_seconds         = number
        })), {})
      }), {})
      cold_start_keepers = optional(bool, true)
    }), {})

    dynamic_models = optional(object({
      enabled                                    = optional(bool, false)
      writes_enabled                             = optional(bool, false)
      workload_owner                             = optional(string, "terraform")
      bootstrap_model_ids                        = optional(set(string), [])
      fresh_install                              = optional(bool, false)
      handoff_receipt                            = optional(string)
      fast_start_evidence_file                   = optional(string)
      fast_start_environment_qualifications_file = optional(string)
      fast_start_measurement_contracts_file      = optional(string)
      fast_start_mechanisms_file                 = optional(string)
      fast_start_wait_second_value               = optional(number, 0.01)
      fast_start_mechanism_hourly_costs          = optional(map(number), {})
      priority_classes = optional(map(number), {
        interactive = 100
        standard    = 0
        batch       = -100
      })
    }), {})

    scheduling = optional(object({
      cohort = optional(object({
        enabled             = optional(bool, true)
        name                = optional(string, "inference-shared")
        fair_sharing_weight = optional(number, 1)
      }), {})
      # Kueue orders LocalQueues by decayed fair-share usage before it compares
      # WorkloadPriorityClass, so a higher class in a different LocalQueue is not
      # categorically admitted first. Set this to accept that ordering on the
      # stable ClusterQueue once it serves more than one lane.
      fair_share_precedence_acknowledged = optional(bool, false)
      # Whether licensed academic models run their own raw CPU data stages.
      # They read the shared reference databases on a tainted pool, so this
      # requires the reference-data plane and core-resource admission. Left
      # false, that lane is absent and those models accept enriched inputs only.
      academic_raw_data_stages = optional(bool, false)
      # Pools a model is qualified for when it has no serving placement, such
      # as a scientific-only model. Merged with the placements derived from
      # the authoritative model contract; a routed model with neither fails.
      model_eligible_pool_ids = optional(map(list(string)), {})
      # Measured per-node Kubernetes allocatable cpu and memory for pools this
      # deployment selects from a catalog profile, keyed by pool ID. The
      # catalog states a preset's nominal size, which is not what a node can
      # schedule after the kubelet, system and DaemonSet reserve, and no
      # producer here measures it. Required for every selected pool once
      # core_capacity is set; a custom pool declares the same facts inline.
      accelerator_schedulable_capacity = optional(map(object({
        cpu_millicores = number
        memory_mib     = number
        # Where the number came from, so a reviewer can check it rather than
        # take it on trust. A bare pair of integers in tfvars is a claim; this
        # is a claim with an origin, a time and a digest of the payload it was
        # read from.
        evidence = object({
          pool_id        = string
          node_group_id  = optional(string)
          source         = string
          captured_at    = string
          payload_sha256 = string
        })
      })), {})
      # Per-resource weights for usage-based admission fair sharing. Kueue
      # v0.17 sums resource.Quantity magnitudes with a default weight of 1 per
      # resource and applies no unit normalization, so once core admission is
      # on a Workload's memory in bytes dwarfs any GPU count and lane ordering
      # tracks memory rather than accelerator demand. Setting a weight per
      # resource is the only control Kueue offers. Keys must be resources this
      # deployment actually budgets. Empty keeps upstream behaviour unchanged
      # rather than substituting a guess.
      fair_share_resource_weights = optional(map(number), {})
      # Order Kueue tries ResourceFlavors in on the stable ClusterQueue.
      # Empty derives a deterministic warm-first order from preemptibility and
      # the node floor. An explicit order may reorder equally stable pools but
      # cannot put a colder tier ahead of a warmer one.
      default_queue_pool_order = optional(list(string), [])
      # Turn core-resource admission on. This removes cpu and memory from
      # Kueue's exclusions, which is what makes any cpu/memory quota in the
      # cluster enforceable, and it couples core capacity to each accelerator
      # pool: cpu and memory join that pool's own resourceGroup, so a Workload
      # reserves cores on the pool that granted its accelerators rather than
      # against an aggregate that may sit on other machines entirely.
      #
      # There is no operator-supplied total. Each pool's budget is derived
      # from the measured per-node capacity declared for it, multiplied by its
      # maximum node count, so the numbers cannot drift from the pools that
      # are actually created. A pool with no measurement fails the plan.
      budget_core_resources = optional(bool, false)
      # Largest per-Pod cpu/memory request each CPU stage class must run,
      # checked against that class's per-node schedulable capacity.
      cpu_stage_requests = optional(map(object({
        cpu_millicores = number
        memory_mib     = number
      })), {})
      # Count cpu and memory in Kueue admission. While they are excluded, any
      # cpu/memory nominalQuota in the cluster is inert.
      cluster_queues = optional(map(object({
        namespace              = optional(string, "fs2-models")
        namespaces             = optional(list(string), [])
        queueing_strategy      = optional(string, "BestEffortFIFO")
        fair_sharing_weight    = optional(number, 1)
        admission_fair_sharing = optional(bool, true)
        flavor_order           = optional(list(string), [])
        flavor_fungibility = optional(object({
          when_can_borrow  = optional(string, "MayStopSearch")
          when_can_preempt = optional(string, "TryNextFlavor")
          preference       = optional(string)
        }), {})
        admission_checks = optional(list(object({
          name       = string
          on_flavors = optional(list(string), [])
        })), [])
        pool_quotas = optional(map(object({
          nominal_quota   = optional(number, 0)
          borrowing_limit = optional(number)
          lending_limit   = optional(number)
        })), {})
        fair_share_precedence_acknowledged = optional(bool, false)
        preemption = optional(object({
          reclaim_within_cohort = optional(string, "Never")
          within_cluster_queue  = optional(string, "Never")
        }), {})
      })), {})
      local_queues = optional(map(object({
        namespace           = optional(string, "fs2-models")
        cluster_queue       = string
        fair_sharing_weight = optional(number, 1)
        model_ids           = optional(set(string), [])
        tenant_ids          = optional(set(string), [])
        service_classes     = optional(set(string), [])
      })), {})
      service_classes = optional(map(object({
        workload_priority_class = string
        priority                = number
        default_local_queue     = optional(string)
        preemption_mode         = optional(string, "restartable")
        pool_preference         = optional(list(string), [])
        max_queue_seconds       = optional(number)
        max_execution_seconds   = optional(number)
        description             = optional(string)
        })), {
        platform-critical = {
          workload_priority_class = "platform-critical"
          priority                = 10000
          preemption_mode         = "restartable"
        }
        presentation = {
          workload_priority_class = "presentation"
          priority                = 1000
          preemption_mode         = "restartable"
        }
        interactive = {
          workload_priority_class = "interactive"
          priority                = 100
          preemption_mode         = "restartable"
        }
        customer-batch = {
          workload_priority_class = "standard"
          priority                = 0
          preemption_mode         = "restartable"
        }
        bulk-backfill = {
          workload_priority_class = "batch"
          priority                = -100
          preemption_mode         = "restartable"
        }
      })
      # The general CPU admission lane over deployment.cpu_pools. One dedicated
      # ClusterQueue admits CPU-class scientific stages from exactly one
      # execution namespace through one namespace-local LocalQueue. v1 is
      # deliberately single-namespace and single-pool: the assembled scheduling
      # contract maps a class to one LocalQueue by bare name and Kueue reports
      # one admitted flavor, so a second namespace or pool could not be frozen
      # or identified. The lane never joins the accelerator cohort and never
      # lends or borrows reference-data capacity; controllers resolve the
      # portable class "general-cpu" against exactly this lane, fail-closed.
      general_cpu = optional(object({
        cluster_queue       = optional(string, "general-cpu")
        local_queue         = optional(string, "general-cpu")
        queueing_strategy   = optional(string, "BestEffortFIFO")
        fair_sharing_weight = optional(number, 1)
        # One execution namespace, and only a namespace this stack owns:
        # fs2-models, which the platform always provisions, or the academic
        # tenant namespace when academic assets are enabled. A namespace no
        # owner creates would leave the LocalQueue dangling and break the
        # self-contained one-tfvars contract.
        namespace = optional(string)
      }), {})
    }), {})

    acceleration = optional(object({
      model_express = optional(object({
        enabled          = optional(bool, false)
        deployment_mode  = optional(string, "managed")
        endpoint         = optional(string)
        metadata_backend = optional(string, "kubernetes")
        namespace        = optional(string, "fs2-modelexpress")
        server_image = optional(object({
          repository = string
          digest     = string
        }))
        cache = optional(object({
          enabled       = optional(bool, true)
          size_gib      = optional(number, 100)
          storage_class = optional(string)
        }), {})
        external_network = optional(object({
          coordinator_namespace  = optional(string)
          coordinator_pod_labels = optional(map(string), {})
          coordinator_cidrs      = optional(set(string), [])
        }), {})
        models = optional(map(object({
          runtime_adapter        = string
          client_package_version = optional(string, "0.5.1")
          transport = optional(object({
            mode                   = optional(string, "fallback")
            rdma_resource_name     = optional(string)
            rdma_resource_quantity = optional(number, 1)
            nixl_backend           = optional(string, "UCX")
            nic_pin                = optional(string, "auto")
          }), {})
          pool_transports = optional(map(object({
            mode                   = optional(string, "fallback")
            rdma_resource_name     = optional(string)
            rdma_resource_quantity = optional(number, 1)
            nixl_backend           = optional(string, "UCX")
            nic_pin                = optional(string, "auto")
          })), {})
        })), {})
      }), {})
    }), {})

    storage = optional(object({
      shared_cache = optional(object({
        size_gib         = optional(number)
        type             = optional(string, "NETWORK_SSD")
        block_size_bytes = optional(number, 4096)
        forbid_deletion  = optional(bool, false)
      }))
      # The mechanism declaration remains the source of truth for claim names,
      # namespaces and compile-cache byte limits. This deployment-wide block
      # selects whether Terraform owns those declared claims and how they are
      # realized on the shared filesystem installed by the foundation stage.
      fast_start_claims = optional(object({
        manage                     = optional(bool, true)
        storage_class              = optional(string, "csi-mounted-fs-path-sc")
        compile_cache_min_size_gib = optional(number, 16)
        residency_receipt_size_gib = optional(number, 1)
      }), {})
      reference_data = optional(object({
        enabled = optional(bool, false)
        lifecycle = optional(object({
          retention_mode = optional(string, "disposable")
        }), {})
        cpu_pool = optional(object({
          platform   = optional(string, "cpu-d3")
          preset     = optional(string, "8vcpu-32gb")
          node_count = optional(number, 1)
          schedulable_capacity = optional(object({
            cpu_millicores        = optional(number, 7000)
            memory_mib            = optional(number, 28672)
            ephemeral_storage_mib = optional(number, 114688)
          }), {})
          boot_disk_type  = optional(string, "NETWORK_SSD")
          boot_disk_gib   = optional(number, 160)
          max_surge       = optional(number, 1)
          max_unavailable = optional(number, 0)
          drain_timeout   = optional(string, "15m")
        }), {})
        filesystem = optional(object({
          size_gib         = optional(number, 2048)
          type             = optional(string, "NETWORK_SSD")
          block_size_bytes = optional(number, 4096)
          forbid_deletion  = optional(bool, false)
        }), {})
        object_storage = optional(object({
          bucket_name  = optional(string)
          max_size_gib = optional(number, 2048)
        }), {})
        namespace = optional(string, "fs2-reference-data")
        queue = optional(object({
          resource_flavor = optional(string, "reference-data-cpu")
          cluster_queue   = optional(string, "reference-data-cpu")
          local_queue     = optional(string, "reference-data")
          nominal_cpu     = optional(string, "6")
          nominal_memory  = optional(string, "24Gi")
        }), {})
        network = optional(object({
          allow_public_source_staging = optional(bool, false)
          allow_public_msa_opt_in     = optional(bool, false)
        }), {})
        status = optional(object({
          enabled                 = optional(bool, false)
          image                   = optional(string)
          replicas                = optional(number, 1)
          service_monitor_enabled = optional(bool, true)
        }), {})
        pipeline = optional(object({
          enabled                 = optional(bool, false)
          bundle_id               = optional(string, "alphafold3-public-databases-v3.0")
          image                   = optional(string)
          generation              = optional(number, 1)
          cpu                     = optional(string, "6")
          memory                  = optional(string, "24Gi")
          ephemeral_storage       = optional(string, "2Gi")
          active_deadline_seconds = optional(number, 604800)
          backoff_limit           = optional(number, 6)
        }), {})
        # The AlphaFold3 data pipeline, not the bulk reference-data stager.
        # It must meet reference-data/model-requirements.json; the 8vcpu-32gb
        # reference pool stages the databases but cannot run this lane.
        preprocess = optional(object({
          cpu                     = optional(string, "16")
          memory                  = optional(string, "64Gi")
          ephemeral_storage       = optional(string, "32Gi")
          active_deadline_seconds = optional(number, 21600)
          backoff_limit           = optional(number, 2)
          threads                 = optional(number, 16)
        }), {})
      }), {})

      # Dedicated result store for the staged scientific batch controller. It is
      # a separate bucket, identity and key from reference_data above: results
      # and immutable public inputs have different retention and different blast
      # radius, so neither store is ever widened to serve the other.
      scientific_artifacts = optional(object({
        enabled = optional(bool, false)
        lifecycle = optional(object({
          retention_mode = optional(string, "disposable")
        }), {})
        object_storage = optional(object({
          # Derived from the deployment name and run id when left null.
          bucket_name  = optional(string)
          max_size_gib = optional(number, 4096)
        }), {})
        # How long the application keeps a committed artifact. Storage-side
        # rules never expire a current object; deletion stays an application
        # decision made against the durable result record.
        retention_days = optional(number, 90)
        # Lifetime of one signed upload or download handle. Workers receive
        # these handles and never a static S3 credential.
        handle_ttl_seconds = optional(number, 600)
        max_artifact_bytes = optional(number, 1099511627776)
        # Exact object-storage addresses, /32 or /128 only, that the control
        # plane may reach on 443 to issue handles and stream a stored object
        # back for digest verification.
        egress_cidrs = optional(set(string), [])
        # Operator-driven rotation. Bumping this rewrites the credential Secret
        # and moves the control plane's rollout annotation even when the cloud
        # key itself is unchanged. Replacing the key rotates it too, because the
        # rollout identity also covers the key's own non-secret identifiers.
        credential_generation = optional(number, 1)
        media_types = optional(set(string), [
          "application/gzip",
          "application/json",
          "application/octet-stream",
          "application/vnd.fs2.scientific-manifest+json",
          "application/vnd.fs2.scientific-validation+json",
          "application/x-tar",
          "chemical/x-cif",
          "chemical/x-mmcif",
          "chemical/x-pdb",
          "text/csv",
          "text/plain",
          "text/x-fasta",
        ])
      }), {})
    }), {})

    # Staged scientific batch execution. The execution map defaults to the
    # exact generated contract committed with this module. Supplying one is an
    # advanced override; root preconditions bind it to the same committed
    # profile qualification and execution identities before any stage runs.
    # Enabling the feature also installs and qualifies the pinned JobSet API,
    # because a true-gang stage has no other executable API.
    scientific_batch = optional(object({
      enabled        = optional(bool, false)
      writes_enabled = optional(bool, false)
      namespace      = optional(string, "fs2-models")

      execution_map = optional(any)

      workers                  = optional(number, 2)
      poll_seconds             = optional(string, "0.25")
      lease_seconds            = optional(string, "30")
      api_timeout_seconds      = optional(string, "5")
      token_expiration_seconds = optional(number, 600)
    }), {})

    artifacts = optional(object({
      external_registry_ids = optional(set(string), [])
      registry_policy = optional(object({
        mode              = optional(string, "regional-mirror")
        repository_prefix = optional(string, "")
      }), {})
    }), {})

    edge = optional(object({
      mode             = optional(string, "internal-only")
      source_cidrs     = optional(set(string), [])
      acme_email       = optional(string)
      acme_environment = optional(string, "production")
      port_forward_ports = optional(object({
        control_plane  = optional(number, 18080)
        admin_console  = optional(number, 18081)
        operator_proxy = optional(number, 18082)
      }), {})
    }), {})

    observability = optional(object({
      dcgm_cold_start_campaign = optional(bool, false)
      grafana = optional(object({
        publish_external = optional(bool, false)
      }), {})
      alertmanager = optional(object({
        enabled   = optional(bool, false)
        retention = optional(string, "120h")
        storage = optional(object({
          storage_class_name = optional(string, "compute-csi-default-sc")
          size_gib           = optional(number, 10)
        }), {})
      }), {})
    }), {})

    applications = object({
      control_plane = object({
        repository             = string
        digest                 = string
        catalog_rollout_digest = string
      })
      admin_console = object({
        repository = string
        digest     = string
        provenance = object({
          source_commit = string
          source_tree   = string
          sbom_sha256   = string
          sbom_format   = optional(string, "cyclonedx-json")
        })
        replica_count = optional(number, 2)
      })
    })

    secrets = optional(object({
      grafana_admin_secret = optional(object({
        name         = optional(string, "fs2-grafana-admin")
        user_key     = optional(string, "admin-user")
        password_key = optional(string, "admin-password")
      }), {})
      grafana_username_env  = optional(string, "FS2_GRAFANA_ADMIN_USERNAME")
      grafana_password_env  = optional(string, "FS2_GRAFANA_ADMIN_PASSWORD")
      ngc_api_key_env       = optional(string, "FS2_NGC_API_KEY")
      nvcr_dockerconfig_env = optional(string, "FS2_NVCR_DOCKERCONFIGJSON")
    }), {})

    acceptance = optional(object({
      create_probe_job = optional(bool, false)
    }), {})
  })

  nullable = false

  validation {
    condition     = var.deployment.schema_version == 1
    error_message = "deployment.schema_version must be 1."
  }

  validation {
    condition = (
      can(regex("^[1-9][0-9]*(?:ms|s|m|h)$", var.deployment.observability.alertmanager.retention)) &&
      length(var.deployment.observability.alertmanager.retention) <= 16 &&
      length(var.deployment.observability.alertmanager.storage.storage_class_name) >= 1 &&
      length(var.deployment.observability.alertmanager.storage.storage_class_name) <= 253 &&
      can(regex(
        "^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$",
        var.deployment.observability.alertmanager.storage.storage_class_name,
      )) &&
      floor(var.deployment.observability.alertmanager.storage.size_gib) == var.deployment.observability.alertmanager.storage.size_gib &&
      var.deployment.observability.alertmanager.storage.size_gib >= 1 &&
      var.deployment.observability.alertmanager.storage.size_gib <= 1024
    )
    error_message = "deployment.observability.alertmanager requires a positive bounded Go duration, a DNS-style storage class, and a whole 1-1024 GiB persistent volume."
  }

  validation {
    condition = try(
      length(split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v"))) >= 2 &&
      length(split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v"))) <= 3 &&
      alltrue([
        for component in split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v")) :
        tostring(tonumber(component)) == component
      ]) &&
      tonumber(split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v"))[0]) == 1 &&
      contains(
        [33, 34, 35],
        tonumber(split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v"))[1]),
      ) &&
      (!var.deployment.scientific_batch.enabled || contains(
        [33, 34, 35],
        tonumber(split(".", trimprefix(var.deployment.cluster.kubernetes_version, "v"))[1]),
      )),
      false,
    )
    error_message = "Kueue v0.17.8's upstream end-to-end matrix covers Kubernetes 1.33-1.35. JobSet v0.12.0's published matrix covers 1.32-1.34 and FS2 qualifies its exact pinned chart/image with Kueue v0.17.8 on Kubernetes 1.35. A cluster must use a numeric 1.<minor>[.<patch>] version in the resulting 1.33-1.35 intersection. No wider minor is claimed."
  }

  validation {
    condition = try(
      (!var.deployment.scheduling.cohort.enabled || (
        length(var.deployment.scheduling.cohort.name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.deployment.scheduling.cohort.name))
      )) &&
      var.deployment.scheduling.cohort.fair_sharing_weight > 0.000000001 &&
      alltrue([
        for queue_name, queue in var.deployment.scheduling.cluster_queues :
        length(queue_name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue_name)) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue.namespace)) &&
        contains(["BestEffortFIFO", "StrictFIFO"], queue.queueing_strategy) &&
        queue.fair_sharing_weight > 0.000000001 &&
        contains(["Never", "LowerPriority", "Any"], queue.preemption.reclaim_within_cohort) &&
        contains(["Never", "LowerPriority", "LowerOrNewerEqualPriority"], queue.preemption.within_cluster_queue) &&
        contains(["MayStopSearch", "TryNextFlavor"], queue.flavor_fungibility.when_can_borrow) &&
        contains(["MayStopSearch", "TryNextFlavor"], queue.flavor_fungibility.when_can_preempt) &&
        (
          try(queue.flavor_fungibility.preference, null) == null || (
            queue.flavor_fungibility.when_can_borrow == "TryNextFlavor" &&
            queue.flavor_fungibility.when_can_preempt == "TryNextFlavor" &&
            contains(
              ["BorrowingOverPreemption", "PreemptionOverBorrowing"],
              queue.flavor_fungibility.preference,
            )
          )
        ) &&
        length(queue.admission_checks) <= 64 &&
        length(queue.admission_checks) == length(distinct([
          for check in queue.admission_checks : check.name
        ])) &&
        alltrue([
          for check in queue.admission_checks :
          length(check.name) <= 63 &&
          can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", check.name)) &&
          length(check.on_flavors) <= 64 &&
          length(check.on_flavors) == length(distinct(check.on_flavors))
        ])
      ]) &&
      alltrue([
        for queue_name, queue in var.deployment.scheduling.local_queues :
        length(queue_name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue_name)) &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", queue.namespace)) &&
        queue.fair_sharing_weight > 0.000000001 &&
        alltrue([
          for tenant_id in queue.tenant_ids :
          length(tenant_id) <= 63 &&
          can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", tenant_id))
        ]) &&
        (length(queue.model_ids) == 0 ? length(queue.service_classes) == 0 : length(queue.service_classes) > 0) &&
        length(setsubtract(queue.service_classes, toset(keys(var.deployment.scheduling.service_classes)))) == 0
      ]) &&
      alltrue([
        for class_name, class in var.deployment.scheduling.service_classes :
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", class_name)) &&
        (class.description == null || (
          # Kueue and Kubernetes bound the rendered description in bytes.
          (length(base64encode(class.description)) / 4 * 3) - length(regexall("=", base64encode(class.description))) >= 1 &&
          (length(base64encode(class.description)) / 4 * 3) - length(regexall("=", base64encode(class.description))) <= 500
        )) &&
        length(class.workload_priority_class) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", class.workload_priority_class)) &&
        floor(class.priority) == class.priority &&
        class.priority >= -2147483648 && class.priority <= 2147483647 &&
        class.preemption_mode == "restartable" &&
        (class.max_queue_seconds == null || (
          floor(class.max_queue_seconds) == class.max_queue_seconds &&
          class.max_queue_seconds >= 1 && class.max_queue_seconds <= 2147483647
        )) &&
        (class.max_execution_seconds == null || (
          floor(class.max_execution_seconds) == class.max_execution_seconds &&
          class.max_execution_seconds >= 1 && class.max_execution_seconds <= 2147483647
        ))
        ]) && toset(keys(var.deployment.scheduling.service_classes)) == toset([
        "platform-critical", "presentation", "interactive", "customer-batch", "bulk-backfill"
      ]) &&
      var.deployment.scheduling.service_classes["platform-critical"].priority > var.deployment.scheduling.service_classes["presentation"].priority &&
      var.deployment.scheduling.service_classes["presentation"].priority > var.deployment.scheduling.service_classes["interactive"].priority &&
      var.deployment.scheduling.service_classes["interactive"].priority > var.deployment.scheduling.service_classes["customer-batch"].priority &&
      var.deployment.scheduling.service_classes["customer-batch"].priority > var.deployment.scheduling.service_classes["bulk-backfill"].priority,
      false,
    )
    error_message = "scheduling must use label-safe LocalQueue/priority names, strict platform-critical > presentation > interactive > customer-batch > bulk-backfill signed-int32 priorities, explicit service-class routes, restartable-only scientific execution, signed-int32 queue/execution ceilings, supported Kueue queue policies, and fair-sharing weights greater than 1e-9; non-preemptible/checkpointable execution is blocked pending separate enforcement."
  }

  validation {
    condition     = !var.deployment.scientific_batch.writes_enabled || var.deployment.scientific_batch.enabled
    error_message = "scientific_batch writes require enabled=true."
  }

  validation {
    condition = try(
      contains(["managed", "external"], var.deployment.acceleration.model_express.deployment_mode) &&
      contains(["kubernetes", "redis"], var.deployment.acceleration.model_express.metadata_backend) &&
      can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.deployment.acceleration.model_express.namespace)) &&
      (
        !var.deployment.acceleration.model_express.enabled ||
        (
          length(var.deployment.acceleration.model_express.models) > 0 &&
          var.deployment.dynamic_models.enabled &&
          var.deployment.dynamic_models.writes_enabled &&
          var.deployment.dynamic_models.workload_owner == "controller"
        )
      ) &&
      (
        !var.deployment.acceleration.model_express.enabled ||
        (
          var.deployment.acceleration.model_express.deployment_mode == "managed" ? (
            var.deployment.acceleration.model_express.endpoint == null &&
            var.deployment.acceleration.model_express.metadata_backend == "kubernetes" &&
            var.deployment.acceleration.model_express.server_image != null &&
            can(regex("^[a-zA-Z0-9._:/-]+$", var.deployment.acceleration.model_express.server_image.repository)) &&
            can(regex("^sha256:[0-9a-f]{64}$", var.deployment.acceleration.model_express.server_image.digest))
            ) : (
            var.deployment.acceleration.model_express.endpoint != null &&
            can(regex("^[A-Za-z0-9.-]+:[0-9]{1,5}$", var.deployment.acceleration.model_express.endpoint)) &&
            try(tonumber(element(split(":", var.deployment.acceleration.model_express.endpoint), 1)) >= 1, false) &&
            try(tonumber(element(split(":", var.deployment.acceleration.model_express.endpoint), 1)) <= 65535, false)
          )
        )
      ) &&
      floor(var.deployment.acceleration.model_express.cache.size_gib) == var.deployment.acceleration.model_express.cache.size_gib &&
      var.deployment.acceleration.model_express.cache.size_gib >= 1 &&
      var.deployment.acceleration.model_express.cache.size_gib <= 65536 &&
      (
        var.deployment.acceleration.model_express.deployment_mode == "managed" ? (
          var.deployment.acceleration.model_express.external_network.coordinator_namespace == null &&
          length(var.deployment.acceleration.model_express.external_network.coordinator_pod_labels) == 0 &&
          length(var.deployment.acceleration.model_express.external_network.coordinator_cidrs) == 0
          ) : (
          (
            var.deployment.acceleration.model_express.external_network.coordinator_namespace != null &&
            length(var.deployment.acceleration.model_express.external_network.coordinator_pod_labels) > 0 &&
            length(var.deployment.acceleration.model_express.external_network.coordinator_cidrs) == 0
            ) || (
            var.deployment.acceleration.model_express.external_network.coordinator_namespace == null &&
            length(var.deployment.acceleration.model_express.external_network.coordinator_pod_labels) == 0 &&
            length(var.deployment.acceleration.model_express.external_network.coordinator_cidrs) > 0
          )
        )
      ) &&
      alltrue([
        for cidr in var.deployment.acceleration.model_express.external_network.coordinator_cidrs :
        try("${cidrhost(cidr, 0)}/${element(split("/", cidr), 1)}" == cidr, false)
      ]) &&
      (
        var.deployment.acceleration.model_express.external_network.coordinator_namespace == null ||
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.deployment.acceleration.model_express.external_network.coordinator_namespace))
      ) &&
      alltrue([
        for key, value in var.deployment.acceleration.model_express.external_network.coordinator_pod_labels :
        length(key) >= 1 && length(key) <= 253 && length(value) <= 63
      ]) &&
      length(setsubtract(
        toset(keys(var.deployment.acceleration.model_express.models)),
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      alltrue([
        for model_id, config in var.deployment.acceleration.model_express.models :
        config.runtime_adapter == "vllm" &&
        config.client_package_version == "0.5.1" &&
        try(jsondecode(file("${path.module}/catalog/runtime/models/${model_id}.json")).runtime.kind, null) == "vllm" &&
        contains(["fallback", "nixl-rdma"], config.transport.mode) &&
        contains(["UCX", "LIBFABRIC"], config.transport.nixl_backend) &&
        can(regex("^[A-Za-z0-9][A-Za-z0-9_.:,-]*$", config.transport.nic_pin)) &&
        length(config.transport.nic_pin) <= 256 &&
        floor(config.transport.rdma_resource_quantity) == config.transport.rdma_resource_quantity &&
        config.transport.rdma_resource_quantity >= 1 &&
        config.transport.rdma_resource_quantity <= 64 &&
        (
          config.transport.mode == "nixl-rdma" ? (
            config.transport.rdma_resource_name != null &&
            length(config.transport.rdma_resource_name) <= 317 &&
            length(split("/", config.transport.rdma_resource_name)) == 2 &&
            length(split("/", config.transport.rdma_resource_name)[0]) <= 253 &&
            length(split("/", config.transport.rdma_resource_name)[1]) <= 63 &&
            can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", config.transport.rdma_resource_name))
            ) : (
            config.transport.rdma_resource_name == null
          )
        ) &&
        alltrue([
          for pool_id, transport in config.pool_transports :
          can(regex("^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$", pool_id)) &&
          length(pool_id) <= 63 &&
          contains(["fallback", "nixl-rdma"], transport.mode) &&
          contains(["UCX", "LIBFABRIC"], transport.nixl_backend) &&
          can(regex("^[A-Za-z0-9][A-Za-z0-9_.:,-]*$", transport.nic_pin)) &&
          length(transport.nic_pin) <= 256 &&
          floor(transport.rdma_resource_quantity) == transport.rdma_resource_quantity &&
          transport.rdma_resource_quantity >= 1 &&
          transport.rdma_resource_quantity <= 64 &&
          (
            transport.mode == "nixl-rdma" ? (
              transport.rdma_resource_name != null &&
              length(transport.rdma_resource_name) <= 317 &&
              length(split("/", transport.rdma_resource_name)) == 2 &&
              length(split("/", transport.rdma_resource_name)[0]) <= 253 &&
              length(split("/", transport.rdma_resource_name)[1]) <= 63 &&
              can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", transport.rdma_resource_name))
              ) : (
              transport.rdma_resource_name == null
            )
          )
        ])
      ]),
      false,
    )
    error_message = "acceleration.model_express must use controller-owned dynamic models, be an opt-in managed Kubernetes-backend or explicit external service with one Kubernetes namespace/Pod selector or CIDR route, use a digest-pinned managed image, select explicit vLLM catalog models with the supported 0.5.1 client adapter, configure a bounded cache, and declare a portable fallback or explicit qualified RDMA extended resource."
  }

  validation {
    condition = (
      can(regex("^[a-zA-Z0-9._:/-]+$", var.deployment.applications.control_plane.repository)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.control_plane.digest)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.control_plane.catalog_rollout_digest)) &&
      can(regex("^[a-zA-Z0-9._:/-]+$", var.deployment.applications.admin_console.repository)) &&
      can(regex("^sha256:[0-9a-f]{64}$", var.deployment.applications.admin_console.digest)) &&
      can(regex("^[0-9a-f]{40}$", var.deployment.applications.admin_console.provenance.source_commit)) &&
      can(regex("^[0-9a-f]{40}$", var.deployment.applications.admin_console.provenance.source_tree)) &&
      can(regex("^[0-9a-f]{64}$", var.deployment.applications.admin_console.provenance.sbom_sha256)) &&
      var.deployment.applications.admin_console.provenance.sbom_format == "cyclonedx-json" &&
      floor(var.deployment.applications.admin_console.replica_count) == var.deployment.applications.admin_console.replica_count &&
      var.deployment.applications.admin_console.replica_count >= 1
    )
    error_message = "applications must provide immutable control-plane and admin-console OCI repositories/digests plus provenance."
  }

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,39}$", var.deployment.name))
    error_message = "deployment.name must be 5-40 lowercase alphanumeric or hyphen characters and start with a letter."
  }

  validation {
    condition = try(
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/approved-targets.json")).targets), var.deployment.target.project_id) ?
      jsondecode(file("${path.module}/catalog/profiles/approved-targets.json")).targets[var.deployment.target.project_id].region == var.deployment.target.region :
      (
        var.deployment.target.project_name != null &&
        var.deployment.target.network.network_name != null &&
        var.deployment.target.network.subnet_name != null &&
        can(cidrhost(var.deployment.target.network.private_subnet_cidr, 0)) &&
        var.deployment.target.system_update_strategy != null
      ),
      false,
    )
    error_message = "A catalog target needs the catalog region; a new target must provide project_name, network/subnet/CIDR, and update strategy for provider verification."
  }

  validation {
    condition = alltrue([
      for cidr in var.deployment.cluster.control_plane_allowed_cidrs : can(cidrhost(cidr, 0))
    ])
    error_message = "Every cluster.control_plane_allowed_cidrs entry must be a valid CIDR."
  }

  validation {
    condition = var.deployment.target.system_update_strategy == null || try(
      floor(var.deployment.target.system_update_strategy.max_surge) == var.deployment.target.system_update_strategy.max_surge &&
      floor(var.deployment.target.system_update_strategy.max_unavailable) == var.deployment.target.system_update_strategy.max_unavailable &&
      var.deployment.target.system_update_strategy.max_surge >= 0 &&
      var.deployment.target.system_update_strategy.max_unavailable >= 0 &&
      var.deployment.target.system_update_strategy.max_surge + var.deployment.target.system_update_strategy.max_unavailable >= 1,
      false,
    )
    error_message = "target.system_update_strategy must use nonnegative whole values with at least one of max_surge or max_unavailable nonzero."
  }

  validation {
    condition = var.deployment.cluster.system_pool == null || try(
      var.deployment.cluster.system_pool.capacity == "regular" &&
      can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.cluster.system_pool.platform)) &&
      can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.cluster.system_pool.preset)) &&
      (
        var.deployment.cluster.system_pool.node_count == null ||
        (
          floor(var.deployment.cluster.system_pool.node_count) == var.deployment.cluster.system_pool.node_count &&
          var.deployment.cluster.system_pool.node_count >= 1 &&
          var.deployment.cluster.system_pool.node_count <= 32
        )
      ) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.deployment.cluster.system_pool.boot_disk_type) &&
      floor(var.deployment.cluster.system_pool.boot_disk_gib) == var.deployment.cluster.system_pool.boot_disk_gib &&
      var.deployment.cluster.system_pool.boot_disk_gib >= 32 &&
      var.deployment.cluster.system_pool.boot_disk_gib <= 4096 &&
      (
        var.deployment.cluster.system_pool.max_surge == null ||
        floor(var.deployment.cluster.system_pool.max_surge) == var.deployment.cluster.system_pool.max_surge
        && var.deployment.cluster.system_pool.max_surge >= 0
      ) &&
      (
        var.deployment.cluster.system_pool.max_unavailable == null ||
        floor(var.deployment.cluster.system_pool.max_unavailable) == var.deployment.cluster.system_pool.max_unavailable
        && var.deployment.cluster.system_pool.max_unavailable >= 0
      ) &&
      (
        var.deployment.cluster.system_pool.max_surge == null ||
        var.deployment.cluster.system_pool.max_unavailable == null ||
        var.deployment.cluster.system_pool.max_surge + var.deployment.cluster.system_pool.max_unavailable >= 1
      ) &&
      can(regex("^[1-9][0-9]*m$", var.deployment.cluster.system_pool.drain_timeout)),
      false,
    )
    error_message = "cluster.system_pool must match the bounded regular CPU-pool, disk, rollout, and drain-time contract consumed by infrastructure."
  }

  validation {
    condition = var.deployment.storage.shared_cache == null || try(
      (
        var.deployment.storage.shared_cache.size_gib == null ||
        (
          floor(var.deployment.storage.shared_cache.size_gib) == var.deployment.storage.shared_cache.size_gib &&
          var.deployment.storage.shared_cache.size_gib >= 32 &&
          var.deployment.storage.shared_cache.size_gib <= 65536
        )
      ) &&
      contains(["NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED", "NETWORK_SSD_IO_M3"], var.deployment.storage.shared_cache.type) &&
      contains([4096, 8192, 16384, 32768, 65536], var.deployment.storage.shared_cache.block_size_bytes),
      false,
    )
    error_message = "storage.shared_cache must use a positive whole-GiB size and a supported network filesystem type/block size."
  }

  validation {
    condition = try(
      length(var.deployment.storage.fast_start_claims.storage_class) <= 253 &&
      can(regex(
        "^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$",
        var.deployment.storage.fast_start_claims.storage_class,
      )) &&
      floor(var.deployment.storage.fast_start_claims.compile_cache_min_size_gib) == var.deployment.storage.fast_start_claims.compile_cache_min_size_gib &&
      var.deployment.storage.fast_start_claims.compile_cache_min_size_gib >= 1 &&
      var.deployment.storage.fast_start_claims.compile_cache_min_size_gib <= 65536 &&
      floor(var.deployment.storage.fast_start_claims.residency_receipt_size_gib) == var.deployment.storage.fast_start_claims.residency_receipt_size_gib &&
      var.deployment.storage.fast_start_claims.residency_receipt_size_gib >= 1 &&
      var.deployment.storage.fast_start_claims.residency_receipt_size_gib <= 1024,
      false,
    )
    error_message = "storage.fast_start_claims requires a DNS-subdomain storage class, a whole 1-65536 GiB compile-cache minimum, and a whole 1-1024 GiB residency-receipt claim."
  }

  validation {
    condition = try(
      !var.deployment.storage.reference_data.enabled || (
        contains(["retain", "disposable"], var.deployment.storage.reference_data.lifecycle.retention_mode) &&
        (
          var.deployment.storage.reference_data.lifecycle.retention_mode == "retain" ?
          var.deployment.storage.reference_data.filesystem.forbid_deletion :
          !var.deployment.storage.reference_data.filesystem.forbid_deletion
        ) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.storage.reference_data.cpu_pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,63}$", var.deployment.storage.reference_data.cpu_pool.preset)) &&
        floor(var.deployment.storage.reference_data.cpu_pool.node_count) == var.deployment.storage.reference_data.cpu_pool.node_count &&
        var.deployment.storage.reference_data.cpu_pool.node_count >= 1 &&
        var.deployment.storage.reference_data.cpu_pool.node_count <= 32 &&
        floor(var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.cpu_millicores) == var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.cpu_millicores &&
        var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.cpu_millicores >= 1000 &&
        floor(var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.memory_mib) == var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.memory_mib &&
        var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.memory_mib >= 1024 &&
        floor(var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib) == var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib &&
        var.deployment.storage.reference_data.cpu_pool.schedulable_capacity.ephemeral_storage_mib >= 1024 &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.deployment.storage.reference_data.cpu_pool.boot_disk_type) &&
        floor(var.deployment.storage.reference_data.cpu_pool.boot_disk_gib) == var.deployment.storage.reference_data.cpu_pool.boot_disk_gib &&
        var.deployment.storage.reference_data.cpu_pool.boot_disk_gib >= 32 &&
        var.deployment.storage.reference_data.cpu_pool.boot_disk_gib <= 4096 &&
        floor(var.deployment.storage.reference_data.cpu_pool.max_surge) == var.deployment.storage.reference_data.cpu_pool.max_surge &&
        floor(var.deployment.storage.reference_data.cpu_pool.max_unavailable) == var.deployment.storage.reference_data.cpu_pool.max_unavailable &&
        var.deployment.storage.reference_data.cpu_pool.max_surge >= 0 &&
        var.deployment.storage.reference_data.cpu_pool.max_unavailable >= 0 &&
        var.deployment.storage.reference_data.cpu_pool.max_surge + var.deployment.storage.reference_data.cpu_pool.max_unavailable >= 1 &&
        can(regex("^[1-9][0-9]*m$", var.deployment.storage.reference_data.cpu_pool.drain_timeout)) &&
        floor(var.deployment.storage.reference_data.filesystem.size_gib) == var.deployment.storage.reference_data.filesystem.size_gib &&
        var.deployment.storage.reference_data.filesystem.size_gib >= 1611 &&
        var.deployment.storage.reference_data.filesystem.size_gib <= 65536 &&
        contains(["NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED", "NETWORK_SSD_IO_M3"], var.deployment.storage.reference_data.filesystem.type) &&
        contains([4096, 8192, 16384, 32768, 65536], var.deployment.storage.reference_data.filesystem.block_size_bytes) &&
        floor(var.deployment.storage.reference_data.object_storage.max_size_gib) == var.deployment.storage.reference_data.object_storage.max_size_gib &&
        var.deployment.storage.reference_data.object_storage.max_size_gib >= 1611 &&
        var.deployment.storage.reference_data.object_storage.max_size_gib <= 65536 &&
        (
          var.deployment.storage.reference_data.object_storage.bucket_name == null ||
          can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.deployment.storage.reference_data.object_storage.bucket_name))
        ) &&
        var.deployment.storage.reference_data.namespace == "fs2-reference-data" &&
        alltrue([
          for name in [
            var.deployment.storage.reference_data.queue.resource_flavor,
            var.deployment.storage.reference_data.queue.cluster_queue,
            var.deployment.storage.reference_data.queue.local_queue,
          ] : can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name)) && length(name) <= 63
        ]) &&
        can(regex("^[1-9][0-9]*(?:m)?$", var.deployment.storage.reference_data.queue.nominal_cpu)) &&
        can(regex("^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$", var.deployment.storage.reference_data.queue.nominal_memory))
      ),
      false,
    )
    error_message = "enabled storage.reference_data requires the dedicated fs2-reference-data namespace (never the live fs2-data database namespace), the default disposable+deletable lifecycle or explicit retain+forbid_deletion semantics, a bounded dedicated regular CPU pool with positive conservative schedulable CPU/memory/ephemeral capacity, DNS-safe names and dedicated filesystem/object capacities of at least 1611 GiB (the 630 GB official AlphaFold3 expansion estimate plus 1 TiB headroom)."
  }

  validation {
    condition = try(
      !var.deployment.storage.reference_data.status.enabled || (
        var.deployment.storage.reference_data.enabled &&
        can(regex("^[^@[:space:]]+@sha256:[0-9a-f]{64}$", var.deployment.storage.reference_data.status.image)) &&
        floor(var.deployment.storage.reference_data.status.replicas) == var.deployment.storage.reference_data.status.replicas &&
        var.deployment.storage.reference_data.status.replicas >= 1 &&
        var.deployment.storage.reference_data.status.replicas <= 3
      ),
      false,
    )
    error_message = "storage.reference_data.status requires the data plane, a digest-pinned image, and 1-3 replicas."
  }

  validation {
    condition = try(
      !var.deployment.storage.reference_data.pipeline.enabled || (
        var.deployment.storage.reference_data.enabled &&
        var.deployment.storage.reference_data.network.allow_public_source_staging &&
        var.deployment.storage.reference_data.pipeline.bundle_id == "alphafold3-public-databases-v3.0" &&
        can(regex("^[^@[:space:]]+@sha256:[0-9a-f]{64}$", var.deployment.storage.reference_data.pipeline.image)) &&
        floor(var.deployment.storage.reference_data.pipeline.generation) == var.deployment.storage.reference_data.pipeline.generation &&
        var.deployment.storage.reference_data.pipeline.generation >= 1 &&
        can(regex("^[1-9][0-9]*(?:m)?$", var.deployment.storage.reference_data.pipeline.cpu)) &&
        can(regex("^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$", var.deployment.storage.reference_data.pipeline.memory)) &&
        can(regex("^[1-9][0-9]*(?:Ki|Mi|Gi|Ti)$", var.deployment.storage.reference_data.pipeline.ephemeral_storage)) &&
        floor(var.deployment.storage.reference_data.pipeline.active_deadline_seconds) == var.deployment.storage.reference_data.pipeline.active_deadline_seconds &&
        var.deployment.storage.reference_data.pipeline.active_deadline_seconds >= 3600 &&
        var.deployment.storage.reference_data.pipeline.active_deadline_seconds <= 1209600 &&
        floor(var.deployment.storage.reference_data.pipeline.backoff_limit) == var.deployment.storage.reference_data.pipeline.backoff_limit &&
        var.deployment.storage.reference_data.pipeline.backoff_limit >= 0 &&
        var.deployment.storage.reference_data.pipeline.backoff_limit <= 20
      ),
      false,
    )
    error_message = "storage.reference_data.pipeline requires the exact official AlphaFold3 bundle, public-source staging opt-in, a digest-pinned image, and bounded CPU-only retry resources."
  }

  validation {
    condition = try(
      !var.deployment.storage.scientific_artifacts.enabled || (
        contains(["retain", "disposable"], var.deployment.storage.scientific_artifacts.lifecycle.retention_mode) &&
        (
          var.deployment.storage.scientific_artifacts.object_storage.bucket_name == null ||
          can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.deployment.storage.scientific_artifacts.object_storage.bucket_name))
        ) &&
        floor(var.deployment.storage.scientific_artifacts.object_storage.max_size_gib) == var.deployment.storage.scientific_artifacts.object_storage.max_size_gib &&
        var.deployment.storage.scientific_artifacts.object_storage.max_size_gib >= 16 &&
        var.deployment.storage.scientific_artifacts.object_storage.max_size_gib <= 65536 &&
        floor(var.deployment.storage.scientific_artifacts.retention_days) == var.deployment.storage.scientific_artifacts.retention_days &&
        var.deployment.storage.scientific_artifacts.retention_days >= 1 &&
        var.deployment.storage.scientific_artifacts.retention_days <= 3650 &&
        floor(var.deployment.storage.scientific_artifacts.handle_ttl_seconds) == var.deployment.storage.scientific_artifacts.handle_ttl_seconds &&
        var.deployment.storage.scientific_artifacts.handle_ttl_seconds >= 30 &&
        var.deployment.storage.scientific_artifacts.handle_ttl_seconds <= 900 &&
        floor(var.deployment.storage.scientific_artifacts.max_artifact_bytes) == var.deployment.storage.scientific_artifacts.max_artifact_bytes &&
        var.deployment.storage.scientific_artifacts.max_artifact_bytes >= 1024 &&
        var.deployment.storage.scientific_artifacts.max_artifact_bytes <= 1099511627776
      ),
      false,
    )
    error_message = "enabled storage.scientific_artifacts requires an explicit retain or disposable lifecycle, an optional valid bucket name, 16-65536 whole GiB of capacity, a 1-3650 day application retention window, a 30-900 second signed-handle lifetime and a 1 KiB-1 TiB maximum artifact size."
  }

  validation {
    condition = try(
      !var.deployment.storage.scientific_artifacts.enabled || (
        length(var.deployment.storage.scientific_artifacts.media_types) > 0 &&
        alltrue([
          for media_type in var.deployment.storage.scientific_artifacts.media_types :
          can(regex("^[a-z0-9][a-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+_-]*$", media_type)) && length(media_type) <= 128
        ]) &&
        # alltrue over an empty collection is true, so the allowlist needs its
        # own length check or an enabled store would get no egress at all.
        length(var.deployment.storage.scientific_artifacts.egress_cidrs) > 0 &&
        alltrue([
          for cidr in var.deployment.storage.scientific_artifacts.egress_cidrs :
          can(cidrhost(cidr, 0)) && (endswith(cidr, "/32") || endswith(cidr, "/128"))
        ]) &&
        floor(var.deployment.storage.scientific_artifacts.credential_generation) == var.deployment.storage.scientific_artifacts.credential_generation &&
        var.deployment.storage.scientific_artifacts.credential_generation >= 1 &&
        var.deployment.storage.scientific_artifacts.credential_generation <= 1000
      ),
      false,
    )
    error_message = "enabled storage.scientific_artifacts requires at least one exact approved media type, at least one exact /32 or /128 object-storage egress address, and a whole credential_generation between 1 and 1000; an empty, subnet-wide or malformed allowlist is never accepted."
  }

  validation {
    condition = try(
      (
        !var.deployment.scientific_batch.enabled ||
        var.deployment.storage.scientific_artifacts.enabled
        ) && (
        !var.deployment.scientific_batch.writes_enabled ||
        var.deployment.scientific_batch.enabled
      ) && can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", var.deployment.scientific_batch.namespace)),
      false,
    )
    error_message = "scientific_batch.enabled requires storage.scientific_artifacts.enabled, scientific_batch.writes_enabled requires scientific_batch.enabled, and the batch namespace must be a DNS label."
  }

  validation {
    condition = try(alltrue([
      for pool_id, pool in var.deployment.cpu_pools : (
        can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", pool_id)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.preset)) &&
        contains(["regular", "preemptible"], pool.capacity_type) &&
        # Exactly one capacity mode. A pool that declares both, or neither, has
        # no single answer for how many nodes the lane may count on.
        ((pool.fixed_nodes == null) != (pool.autoscaling == null)) &&
        (
          pool.fixed_nodes == null ? true : (
            floor(pool.fixed_nodes) == pool.fixed_nodes &&
            pool.fixed_nodes >= 1 &&
            pool.fixed_nodes <= 64
          )
        ) &&
        (
          pool.autoscaling == null ? true : (
            floor(pool.autoscaling.min_nodes) == pool.autoscaling.min_nodes &&
            floor(pool.autoscaling.max_nodes) == pool.autoscaling.max_nodes &&
            pool.autoscaling.min_nodes >= 0 &&
            pool.autoscaling.max_nodes >= 1 &&
            pool.autoscaling.max_nodes >= pool.autoscaling.min_nodes &&
            pool.autoscaling.max_nodes <= 64
          )
        ) &&
        floor(pool.schedulable_capacity.cpu_millicores) == pool.schedulable_capacity.cpu_millicores &&
        pool.schedulable_capacity.cpu_millicores >= 1000 &&
        floor(pool.schedulable_capacity.memory_mib) == pool.schedulable_capacity.memory_mib &&
        pool.schedulable_capacity.memory_mib >= 1024 &&
        floor(pool.schedulable_capacity.ephemeral_storage_mib) == pool.schedulable_capacity.ephemeral_storage_mib &&
        pool.schedulable_capacity.ephemeral_storage_mib >= 1024 &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], pool.boot_disk.type) &&
        floor(pool.boot_disk.size_gib) == pool.boot_disk.size_gib &&
        pool.boot_disk.size_gib >= 32 &&
        pool.boot_disk.size_gib <= 4096 &&
        floor(pool.max_surge) == pool.max_surge &&
        floor(pool.max_unavailable) == pool.max_unavailable &&
        pool.max_surge >= 0 &&
        pool.max_unavailable >= 0 &&
        pool.max_surge + pool.max_unavailable >= 1 &&
        can(regex("^[1-9][0-9]*m$", pool.drain_timeout)) &&
        # Reserved scheduling prefixes stay owned by the pools that define them.
        # A general pool must never be able to label itself as reference-data,
        # system or accelerator capacity and silently absorb their workloads.
        alltrue([
          for key, value in pool.node_labels : (
            # The same qualified-name and label-value grammars every other
            # layer uses. The previous rule allowed several slashes, an
            # underscore inside a DNS prefix, and any 63-character value
            # including one with a space; the API rejects all of those, so a
            # plan would succeed and the node group would fail at apply.
            length(key) <= 317 &&
            length(split("/", key)) <= 2 &&
            length(split("/", key)[0]) <= (length(split("/", key)) == 2 ? 253 : 63) &&
            (length(split("/", key)) == 1 || length(element(split("/", key), 1)) <= 63) &&
            can(regex("^([a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*/)?[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", key)) &&
            length(value) <= 63 &&
            can(regex("^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", value)) &&
            !startswith(key, "accelerator.fs2.nebius/") &&
            !startswith(key, "capacity.fs2.nebius/") &&
            !startswith(key, "lifecycle.fs2.nebius/") &&
            !startswith(key, "storage.fs2.nebius/") &&
            !startswith(key, "workload.fs2.nebius/")
          )
        ])
      )
    ]), false)
    error_message = "Every deployment.cpu_pools entry needs a DNS-safe ID, an open-ended platform/preset, regular or preemptible capacity, exactly one of fixed_nodes or an autoscaling envelope within 0-64 nodes, positive measured schedulable CPU/memory/ephemeral capacity, a whole 32-4096 GiB supported boot disk, a valid rollout and drain contract, and node labels that never reuse the reserved accelerator, capacity, lifecycle, storage or workload fs2 prefixes."
  }

  validation {
    condition = try(
      contains(["BestEffortFIFO", "StrictFIFO"], var.deployment.scheduling.general_cpu.queueing_strategy) &&
      var.deployment.scheduling.general_cpu.fair_sharing_weight > 0 &&
      alltrue([
        for name in [
          var.deployment.scheduling.general_cpu.cluster_queue,
          var.deployment.scheduling.general_cpu.local_queue,
        ] : can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name)) && length(name) <= 63
      ]) &&
      # The general lane is a distinct owner. Reusing a reference-data queue
      # identity would put general aggregation and reference preprocessing in
      # one quota and defeat the separation this pool exists for.
      !contains([
        var.deployment.storage.reference_data.queue.cluster_queue,
        var.deployment.storage.reference_data.queue.resource_flavor,
      ], var.deployment.scheduling.general_cpu.cluster_queue) &&
      !contains(keys(var.deployment.scheduling.cluster_queues), var.deployment.scheduling.general_cpu.cluster_queue) &&
      (
        var.deployment.scheduling.general_cpu.namespace == null ||
        contains(
          concat(
            ["fs2-models"],
            var.academic_assets.enabled ? [var.academic_assets.namespace] : [],
          ),
          var.deployment.scheduling.general_cpu.namespace,
        )
      ) &&
      # A lane with no capacity is a lane that silently never admits. Naming an
      # execution namespace without a pool is a configuration error, not a queue.
      (
        length(var.deployment.cpu_pools) > 0 ||
        var.deployment.scheduling.general_cpu.namespace == null
      ) &&
      # v1 binds one class to one pool so the admitted flavor identifies the
      # actual node group.
      length(var.deployment.cpu_pools) <= 1,
      false,
    )
    error_message = "scheduling.general_cpu must use DNS-safe queue names distinct from the reference-data flavor/queue and every accelerator ClusterQueue, a supported queueing strategy, a positive fair-sharing weight, an execution namespace this stack owns (fs2-models, or the academic tenant namespace when academic assets are enabled), and a declared cpu_pools entry; v1 accepts exactly one general CPU pool, because one flavor spanning several pools cannot identify the node group that ran a stage."
  }

  validation {
    condition = alltrue([
      for registry_id in var.deployment.artifacts.external_registry_ids :
      can(regex("^registry-[a-z0-9]+$", registry_id))
    ])
    error_message = "artifacts.external_registry_ids must contain only Nebius registry IDs."
  }

  validation {
    condition = (
      contains(
        ["regional-mirror", "direct-source"],
        var.deployment.artifacts.registry_policy.mode,
      ) &&
      can(regex(
        "^(?:|[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?)$",
        var.deployment.artifacts.registry_policy.repository_prefix,
      ))
    )
    error_message = "artifacts.registry_policy must select regional-mirror or direct-source and use a bounded OCI repository prefix."
  }

  validation {
    condition = (
      var.deployment.artifacts.registry_policy.mode != "regional-mirror" ||
      alltrue([
        for image in values(var.deployment.models.image_overrides) :
        can(regex("@sha256:[0-9a-f]{64}$", image))
      ])
    )
    error_message = "regional-mirror requires every models.image_overrides value to end in an immutable @sha256 digest."
  }

  validation {
    condition = (
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/capacity-profiles.json")).capacity_profiles), var.deployment.profiles.capacity) &&
      contains(
        keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles),
        coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity),
      ) &&
      contains(keys(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles), var.deployment.profiles.models)
    )
    error_message = "Each deployment.profiles selection must exist in its corresponding capacity, accelerator-pool, or model catalog."
  }

  validation {
    condition = try(alltrue([
      for pool_id, bounds in var.deployment.accelerator_pool_capacity : (
        contains(
          keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools),
          pool_id,
        ) &&
        floor(bounds.min_nodes) == bounds.min_nodes &&
        floor(bounds.max_nodes) == bounds.max_nodes &&
        bounds.min_nodes >= 0 &&
        bounds.max_nodes >= 1 &&
        bounds.max_nodes >= bounds.min_nodes &&
        bounds.max_nodes <= jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools[pool_id].max_nodes
      )
    ]), false)
    error_message = "accelerator_pool_capacity must use pool IDs in the selected profile and satisfy 0 <= min_nodes <= max_nodes <= the reviewed ceiling."
  }

  validation {
    condition = try(length(var.deployment.accelerator_pools) <= 32 && alltrue([
      for pool_id, pool in var.deployment.accelerator_pools : (
        # One canonical pool-ID grammar across the facade, the scheduling
        # module, and catalog/runtime/schema/cpu-stage-classes.schema.json: a
        # lowercase Kubernetes label value of 1 to 63 characters. Pool IDs are
        # label values, not DNS labels, and real ones carry dots and
        # underscores; a narrower rule here would reject an ID every other
        # layer accepts.
        length(pool_id) <= 63 &&
        can(regex("^[a-z0-9](?:[-_a-z0-9.]{0,61}[a-z0-9])?$", pool_id)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.platform)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$", pool.preset)) &&
        can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", pool.accelerator_class)) &&
        length(split("/", pool.resource_name)) == 2 &&
        length(split("/", pool.resource_name)[0]) <= 253 &&
        length(split("/", pool.resource_name)[1]) <= 63 &&
        length(pool.resource_name) <= 317 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*/[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$", pool.resource_name)) &&
        floor(pool.gpus_per_node) == pool.gpus_per_node && pool.gpus_per_node >= 1 &&
        contains(["amd64", "arm64"], pool.host_architecture) &&
        contains(["regular", "preemptible"], pool.capacity_type) &&
        contains(["raw", "kubelet-ephemeral"], pool.local_nvme_mode) &&
        contains(["NETWORK_SSD", "NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], pool.boot_disk.type) &&
        floor(pool.boot_disk.size_gib) == pool.boot_disk.size_gib &&
        pool.boot_disk.size_gib >= 32 && pool.boot_disk.size_gib <= 4096 &&
        floor(pool.min_nodes) == pool.min_nodes && pool.min_nodes >= 0 &&
        floor(pool.max_nodes) == pool.max_nodes && pool.max_nodes >= pool.min_nodes &&
        (pool.reservation_policy == null ? true : (
          pool.capacity_type == "regular" &&
          pool.min_nodes >= 1 &&
          pool.min_nodes == pool.max_nodes &&
          contains(["AUTO", "STRICT"], pool.reservation_policy.policy) &&
          length(pool.reservation_policy.reservation_ids) >= 1 &&
          length(distinct(pool.reservation_policy.reservation_ids)) == length(pool.reservation_policy.reservation_ids) &&
          alltrue([
            for reservation_id in pool.reservation_policy.reservation_ids :
            can(regex("^capacityblockgroup-[a-z0-9]+$", reservation_id))
          ])
        )) &&
        contains(["managed", "operator"], pool.driver.mode) &&
        (pool.driver.mode == "managed" ? pool.driver.preset != null : pool.driver.preset == null) &&
        contains(["standalone", "gpu_cluster", "nvlink_rack"], pool.topology.mode) &&
        (pool.topology.mode == "gpu_cluster" ? pool.topology.infiniband_fabric != null : true) &&
        (pool.topology.mode == "nvlink_rack" ? (
          pool.capacity_type == "regular" &&
          pool.min_nodes == pool.max_nodes &&
          pool.topology.rack_count >= 1 &&
          pool.topology.nodes_per_rack >= 1 &&
          pool.max_nodes == pool.topology.rack_count * pool.topology.nodes_per_rack
        ) : pool.topology.rack_count == 0) &&
        contains(["none", "single", "mixed"], pool.mig.strategy) &&
        (pool.mig.strategy == "none" || (
          pool.driver.mode == "operator" &&
          pool.mig.config != null &&
          pool.resource_name != "nvidia.com/gpu"
        ))
      )
    ]), false)
    error_message = "accelerator_pools is limited to 32 entries and must use label-safe pool and accelerator-class IDs of at most 63 characters plus structurally valid provider pools, including a whole 32-4096 GiB supported boot disk; reservations require fixed regular capacity, AUTO or STRICT policy, and unique capacity-block-group IDs; platform and preset stay open-ended so current and future Nebius GPUs pass through to provider validation."
  }

  validation {
    condition = try(
      contains(["profile", "explicit"], var.deployment.models.selection) &&
      (var.deployment.models.selection == "profile" ? length(var.deployment.models.enabled) == 0 : true) &&
      length(setsubtract(
        var.deployment.models.enabled,
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      (
        var.deployment.dynamic_models.fast_start_evidence_file == null ? true :
        startswith(pathexpand(var.deployment.dynamic_models.fast_start_evidence_file), "/") &&
        can(jsondecode(file(pathexpand(var.deployment.dynamic_models.fast_start_evidence_file))))
      ) &&
      alltrue([
        for path in [
          var.deployment.dynamic_models.fast_start_environment_qualifications_file,
          var.deployment.dynamic_models.fast_start_measurement_contracts_file,
          var.deployment.dynamic_models.fast_start_mechanisms_file,
        ] : path == null ? true : startswith(pathexpand(path), "/") && can(jsondecode(file(pathexpand(path))))
      ]) &&
      var.deployment.dynamic_models.fast_start_wait_second_value >= 0 &&
      var.deployment.dynamic_models.fast_start_wait_second_value <= 1000000 &&
      length(var.deployment.dynamic_models.fast_start_mechanism_hourly_costs) <= 128 &&
      alltrue([
        for name, cost in var.deployment.dynamic_models.fast_start_mechanism_hourly_costs :
        can(regex("^[a-z][a-z0-9-]{0,63}$", name)) && cost >= 0
      ]),
      false,
    )
    error_message = "models.selection must be profile or explicit; explicit IDs must belong to the profile; fast-start evidence, qualification, measurement, and mechanism contracts must be readable JSON at absolute paths; and bounded economic inputs must be valid."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.image_overrides)),
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      alltrue([
        for image in values(var.deployment.models.image_overrides) :
        can(regex("^[a-zA-Z0-9._:/@-]+$", image)) && !endswith(split("/", image)[0], ".invalid")
      ]),
      false,
    )
    error_message = "models.image_overrides keys must belong to the selected model profile and values must be non-placeholder OCI image references."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.pool_overrides)),
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes),
      )) == 0 &&
      alltrue([
        for pool_id in values(var.deployment.models.pool_overrides) : contains(
          length(var.deployment.accelerator_pools) > 0 ?
          toset(keys(var.deployment.accelerator_pools)) :
          toset(keys(jsondecode(file("${path.module}/catalog/profiles/accelerator-pool-profiles.json")).profiles[coalesce(var.deployment.profiles.accelerators, var.deployment.profiles.capacity)].pools)),
          pool_id,
        )
      ]),
      false,
    )
    error_message = "models.pool_overrides must map selected-profile model IDs to accelerator pools declared by this deployment."
  }

  validation {
    condition = try(
      length(setsubtract(
        var.deployment.models.scaling.hot,
        var.deployment.models.selection == "profile" ?
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes) :
        var.deployment.models.enabled,
      )) == 0,
      false,
    )
    error_message = "models.scaling.hot must be a subset of the enabled model set."
  }

  validation {
    condition = try(
      length(setsubtract(
        toset(keys(var.deployment.models.scaling.overrides)),
        var.deployment.models.selection == "profile" ?
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes) :
        var.deployment.models.enabled,
      )) == 0 &&
      alltrue([
        for scaling in values(var.deployment.models.scaling.overrides) : (
          floor(scaling.min_replicas) == scaling.min_replicas &&
          floor(scaling.max_replicas) == scaling.max_replicas &&
          scaling.min_replicas >= 0 &&
          scaling.max_replicas >= scaling.min_replicas &&
          scaling.max_replicas <= 128 &&
          scaling.target_queue_depth >= 1 &&
          scaling.polling_interval_seconds >= 1 &&
          scaling.polling_interval_seconds <= 60 &&
          scaling.cooldown_seconds >= 5 &&
          scaling.cooldown_seconds <= 7200
        )
      ]),
      false,
    )
    error_message = "models.scaling.overrides must target enabled models and use bounded KEDA settings; the deployment contract further limits replicas to compatible accelerator capacity."
  }

  validation {
    condition = (
      contains(["static", "keda"], var.deployment.models.scaling.mode) &&
      (var.deployment.models.scaling.mode == "keda" || length(var.deployment.models.scaling.hot) == 0) &&
      floor(var.deployment.models.scaling.polling_interval_seconds) == var.deployment.models.scaling.polling_interval_seconds &&
      var.deployment.models.scaling.polling_interval_seconds >= 1 &&
      var.deployment.models.scaling.polling_interval_seconds <= 60 &&
      floor(var.deployment.models.scaling.cooldown_period_seconds) == var.deployment.models.scaling.cooldown_period_seconds &&
      var.deployment.models.scaling.cooldown_period_seconds >= 5 &&
      var.deployment.models.scaling.cooldown_period_seconds <= 7200
    )
    error_message = "models.scaling contains an invalid mode, hot floor, polling, or cooldown value."
  }

  validation {
    condition = try(
      contains(["terraform", "released", "controller"], var.deployment.dynamic_models.workload_owner) &&
      (
        var.deployment.dynamic_models.enabled || (
          !var.deployment.dynamic_models.writes_enabled &&
          var.deployment.dynamic_models.workload_owner == "terraform" &&
          length(var.deployment.dynamic_models.bootstrap_model_ids) == 0 &&
          !var.deployment.dynamic_models.fresh_install &&
          var.deployment.dynamic_models.handoff_receipt == null
        )
      ) &&
      (
        var.deployment.dynamic_models.workload_owner == "terraform" ? (
          !var.deployment.dynamic_models.writes_enabled &&
          length(var.deployment.dynamic_models.bootstrap_model_ids) == 0 &&
          !var.deployment.dynamic_models.fresh_install &&
          var.deployment.dynamic_models.handoff_receipt == null
          ) : var.deployment.dynamic_models.workload_owner == "released" ? (
          var.deployment.dynamic_models.enabled &&
          !var.deployment.dynamic_models.writes_enabled &&
          length(var.deployment.dynamic_models.bootstrap_model_ids) == 0 &&
          !var.deployment.dynamic_models.fresh_install &&
          var.deployment.dynamic_models.handoff_receipt == null
          ) : (
          var.deployment.dynamic_models.enabled &&
          var.deployment.dynamic_models.writes_enabled &&
          var.deployment.models.scaling.mode == "keda" &&
          (var.deployment.dynamic_models.fresh_install != (var.deployment.dynamic_models.handoff_receipt != null)) &&
          (var.deployment.dynamic_models.handoff_receipt == null || can(regex("^sha256:[0-9a-f]{64}$", var.deployment.dynamic_models.handoff_receipt)))
        )
      ) &&
      length(setsubtract(
        var.deployment.dynamic_models.bootstrap_model_ids,
        var.deployment.models.selection == "profile" ?
        toset(jsondecode(file("${path.module}/catalog/profiles/model-profiles.json")).profiles[var.deployment.profiles.models].canonical_routes) :
        var.deployment.models.enabled,
      )) == 0,
      false,
    )
    error_message = "dynamic_models must use one exclusive ownership mode: terraform (read-only controller), released (first cutover apply), or controller (write mode with KEDA and exactly one of fresh_install or a release handoff_receipt); bootstrap IDs must be selected models."
  }

  validation {
    condition = try(
      length(var.deployment.dynamic_models.priority_classes) > 0 &&
      contains(keys(var.deployment.dynamic_models.priority_classes), "standard") &&
      alltrue([
        for name, value in var.deployment.dynamic_models.priority_classes :
        length(name) <= 63 &&
        can(regex("^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$", name)) &&
        floor(value) == value && value >= -2147483648 && value <= 2147483647
      ]),
      false,
    )
    error_message = "dynamic_models.priority_classes must contain standard and map DNS label names of at most 63 characters to signed 32-bit integer Kueue priorities."
  }

  validation {
    condition = (
      (!var.deployment.observability.dcgm_cold_start_campaign || var.deployment.profiles.models == "full_catalog") &&
      (!var.deployment.acceptance.create_probe_job || length(var.deployment.models.enabled) > 0 || var.deployment.models.selection == "profile")
    )
    error_message = "The DCGM cold-start campaign requires the full_catalog model profile, and an acceptance probe requires at least one selected model."
  }

  validation {
    condition = (
      contains(["public", "internal-only"], var.deployment.edge.mode) &&
      contains(["staging", "production"], var.deployment.edge.acme_environment) &&
      (
        var.deployment.edge.mode == "public" ?
        length(var.deployment.edge.source_cidrs) > 0 &&
        length(var.deployment.edge.source_cidrs) <= 8 &&
        alltrue([
          for cidr in var.deployment.edge.source_cidrs :
          can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$", cidr)) && can(cidrhost(cidr, 0))
        ]) &&
        var.deployment.edge.acme_email != null && can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.deployment.edge.acme_email)) :
        length(var.deployment.edge.source_cidrs) == 0 && var.deployment.edge.acme_email == null
      )
    )
    error_message = "Public edge mode requires one to eight IPv4 source CIDRs, an ACME email, and a staging or production ACME environment; internal-only mode requires neither CIDRs nor email."
  }

  validation {
    condition = (
      alltrue([
        for port in values(var.deployment.edge.port_forward_ports) :
        floor(port) == port && port >= 1024 && port <= 65535
      ]) &&
      length(toset(values(var.deployment.edge.port_forward_ports))) == 3
    )
    error_message = "edge.port_forward_ports must contain three distinct whole TCP ports from 1024 through 65535."
  }

  validation {
    condition = alltrue([
      for name in [
        var.deployment.secrets.grafana_username_env,
        var.deployment.secrets.grafana_password_env,
        var.deployment.secrets.ngc_api_key_env,
        var.deployment.secrets.nvcr_dockerconfig_env,
      ] : can(regex("^[A-Z][A-Z0-9_]{2,127}$", name))
    ])
    error_message = "Secret environment-variable references must be uppercase shell variable names."
  }
}
variable "academic_assets" {
  description = <<-EOT
    Tenant-private delivery of licensed academic assets (AlphaFold 3 parameters and the
    BindCraft PyRosetta prerequisite). Licensed bytes are mounted from a tenant-private
    volume and are never embedded in an image or placed in a general shared cache.

    Project and region are inherited from deployment.target, so this block stays portable
    across projects and regions. institution_id is intentionally nullable: the operational
    proof-of-concept path must not require invented institution metadata, and the formal
    institutional licence acceptance is tracked separately from this infrastructure.
  EOT
  type = object({
    enabled             = optional(bool, false)
    tenant_id           = optional(string, "tenant-academic")
    institution_id      = optional(string, null)
    namespace           = optional(string, "fs2-academic-poc")
    runtime_pvc_name    = optional(string, "academic-assets-runtime-rwx")
    runtime_storage_gib = optional(number, 128)

    # retained    the claim holds licensed bytes that must survive; Terraform refuses
    #             to destroy or replace it.
    # disposable  the claim belongs to a throwaway acceptance environment and must
    #             tear down cleanly with the rest of it.
    runtime_claim_lifecycle = optional(string, "disposable")
    storage_class           = optional(string, "csi-mounted-fs-path-sc")
    access_mode             = optional(string, "ReadWriteMany")
    mount_root              = optional(string, "/opt/fs2/academic")

    # Licensed bytes are staged under this shared non-root group and read through a
    # supplemental group, so a runtime image running as its own uid can read them
    # without the bytes ever becoming world-readable.
    asset_gid                             = optional(number, 65532)
    deny_egress_during_offline_validation = optional(bool, false)

    # Optional migration binding for a historical quarantine claim created before
    # the canonical volume. Fresh deployments leave it disabled; retention is an
    # explicit operator choice rather than a default guardrail.
    legacy_quarantine = optional(object({
      enabled     = optional(bool, false)
      namespace   = optional(string, "fs2-models")
      pvc_name    = optional(string, "cancer-immunotherapy-academic-assets-rwx-v1")
      storage_gib = optional(number, 128)
      retain      = optional(bool, false)
    }), {})

    # Non-secret readiness digest emitted by academic-assets/scripts/academic_assets.py.
    readiness_manifest_sha256 = optional(string, null)

    execution = optional(object({
      enabled                    = optional(bool, true)
      local_queue                = optional(string, "academic-scientific")
      cluster_queue              = optional(string, "inference-accelerators")
      service_account            = optional(string, "fs2-academic-runner")
      controller_namespace       = optional(string, "fs2-system")
      controller_service_account = optional(string, "fs2-serve-control-plane-runtime")
    }), {})

    assets = optional(map(object({
      model_id              = string
      relative_path         = string
      install_relative_path = optional(string, null)
      read_only             = optional(bool, true)

      # How model onboarding addresses this object. Declared here so the rendered
      # mount is the contracted one rather than a path derived from the asset key.
      runtime_binding = optional(object({
        artifact_id                = string
        source_sub_path            = string
        consumer_path              = string
        mechanism                  = string
        content_identity_kind      = optional(string, "file-digest")
        content_manifest_algorithm = optional(string, null)
        content_digest_sha256      = optional(string, null)
        size_bytes                 = optional(number, null)
        source_artifact = optional(object({
          filename   = string
          sha256     = string
          size_bytes = number
        }), null)
      }), null)
    })), {})
  })
  default = {}

  validation {
    # The academic tenant is projected into a Kueue LocalQueue route and onto
    # Kubernetes labels, both of which bound a value at 63 characters.
    condition = (
      length(var.academic_assets.tenant_id) <= 63 &&
      can(regex("^[a-z0-9][a-z0-9._-]{2,62}$", var.academic_assets.tenant_id))
    )
    error_message = "academic_assets.tenant_id must be a lowercase DNS-safe tenant identifier of at most 63 characters, because it becomes a Kueue route tenant identity and a Kubernetes label value."
  }

  validation {
    condition = alltrue([
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.academic_assets.namespace)),
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.academic_assets.runtime_pvc_name)),
    ])
    error_message = "academic_assets namespace and runtime_pvc_name must be DNS labels."
  }

  validation {
    condition = (
      var.academic_assets.namespace != var.academic_assets.legacy_quarantine.namespace ||
      var.academic_assets.runtime_pvc_name != var.academic_assets.legacy_quarantine.pvc_name
    )
    error_message = "The canonical runtime claim must be distinct from the historical quarantine claim."
  }

  validation {
    condition = (
      startswith(var.academic_assets.mount_root, "/") &&
      !strcontains(var.academic_assets.mount_root, "..")
    )
    error_message = "academic_assets.mount_root must be an absolute path without parent traversal."
  }

  validation {
    condition     = var.academic_assets.runtime_storage_gib >= 16
    error_message = "The academic runtime volume needs at least 16 GiB for the pinned parameters, wheel and installed tree."
  }

  validation {
    condition     = contains(["retained", "disposable"], var.academic_assets.runtime_claim_lifecycle)
    error_message = "academic_assets.runtime_claim_lifecycle must be \"retained\" or \"disposable\"."
  }

  validation {
    condition     = var.academic_assets.asset_gid > 0 && var.academic_assets.asset_gid < 65536
    error_message = "academic_assets.asset_gid must be a non-root group id below 65536."
  }

  validation {
    condition     = var.academic_assets.access_mode == "ReadWriteMany"
    error_message = "Licensed academic assets are shared read-only across runtime pods and require ReadWriteMany."
  }

  validation {
    condition = alltrue([
      for key, asset in var.academic_assets.assets :
      !startswith(asset.relative_path, "/") && !strcontains(asset.relative_path, "..")
    ])
    error_message = "Every academic asset relative_path must be a safe relative path inside the tenant volume."
  }

  validation {
    condition = (
      var.academic_assets.readiness_manifest_sha256 == null ||
      can(regex("^[0-9a-f]{64}$", var.academic_assets.readiness_manifest_sha256))
    )
    error_message = "academic_assets.readiness_manifest_sha256 must be a lowercase SHA-256 digest."
  }
}

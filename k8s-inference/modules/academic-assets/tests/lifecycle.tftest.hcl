# Provider-mocked contract tests for claim lifecycle selection.
#
# The point of these is that a long-lived cluster cannot discard verified
# licensed bytes, while a throwaway acceptance cluster still tears down cleanly.
# Terraform's own teardown runs a destroy after each apply run, so the disposable
# cases below fail if a destroy guard is ever reintroduced on that path.

mock_provider "kubernetes" {}

variables {
  academic_network_policy = {
    internal_api_namespace = "fs2-system"
    internal_api_pod_labels = {
      "app.kubernetes.io/name"      = "fs2-serve-control-plane"
      "app.kubernetes.io/instance"  = "fs2-serve-control-plane"
      "app.kubernetes.io/component" = "gateway"
    }
    internal_api_port  = 8080
    object_store_cidrs = ["203.0.113.10/32", "2001:db8::10/128"]
  }

  academic_assets = {
    enabled        = true
    project_id     = "project-test"
    region         = "eu-north1"
    tenant_id      = "tenant-academic"
    institution_id = null
    namespace      = "fs2-academic-poc"
    runtime_claim = {
      name          = "academic-assets-runtime-rwx"
      storage_gib   = 128
      storage_class = "csi-mounted-fs-path-sc"
      access_mode   = "ReadWriteMany"
      lifecycle     = "retained"
    }
    legacy_quarantine_claim = {
      enabled     = true
      namespace   = "fs2-models"
      name        = "cancer-immunotherapy-academic-assets-rwx-v1"
      storage_gib = 128
      retain      = true
    }
    delivery = {
      mode                    = "tenant-private-volume"
      mount_root              = "/opt/fs2/academic"
      asset_gid               = 65532
      consumer_access         = "supplemental-group"
      world_readable          = false
      embed_licensed_bytes    = false
      general_shared_cache    = false
      deny_egress_on_validate = true
    }
    assets = {
      alphafold3 = {
        model_id      = "alphafold3"
        relative_path = "alphafold3/af3.bin.zst"
        read_only     = true
      }
    }
    readiness_manifest_sha256 = null
  }
}

run "retained_selects_only_the_guarded_claim" {
  command = plan

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained) == 1
    error_message = "A retained lifecycle must plan the destroy-guarded runtime claim."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable) == 0
    error_message = "The two runtime lifecycles must be mutually exclusive."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_legacy_retained) == 1
    error_message = "retain=true must plan the guarded quarantine claim."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_legacy_disposable) == 0
    error_message = "The two quarantine lifecycles must be mutually exclusive."
  }

  assert {
    condition = (
      length(kubernetes_network_policy_v1.academic_default_deny) == 1 &&
      kubernetes_network_policy_v1.academic_default_deny[0].metadata[0].name == "default-deny" &&
      kubernetes_network_policy_v1.academic_default_deny[0].metadata[0].namespace == "fs2-academic-poc" &&
      toset(kubernetes_network_policy_v1.academic_default_deny[0].spec[0].policy_types) == toset(["Ingress", "Egress"])
    )
    error_message = "An enabled academic namespace must be Terraform-owned and default-denied in both directions."
  }

  assert {
    condition = (
      length(kubernetes_network_policy_v1.academic_scientific_workloads) == 1 &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].metadata[0].name == "academic-scientific-workloads" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].pod_selector[0].match_expressions[0].key == "fs2.nebius.ai/workload-id" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].pod_selector[0].match_expressions[0].operator == "Exists" &&
      length(kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress) == 4
    )
    error_message = "Academic Job and JobSet child Pods need a pre-deny allow policy for DNS, the exact internal API, and every configured object-store CIDR."
  }

  assert {
    condition = (
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[1].to[0].namespace_selector[0].match_labels["kubernetes.io/metadata.name"] == "fs2-system" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[1].to[0].pod_selector[0].match_labels["app.kubernetes.io/component"] == "gateway" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[1].ports[0].port == "8080" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[2].to[0].ip_block[0].cidr == "2001:db8::10/128" &&
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[3].to[0].ip_block[0].cidr == "203.0.113.10/32"
    )
    error_message = "Academic egress destinations must stay exact and deterministically ordered."
  }
}

run "retained_outputs_coalesce_the_selected_identity" {
  command = plan

  assert {
    condition     = output.academic_assets.runtime_claim.name == "academic-assets-runtime-rwx"
    error_message = "Outputs must report the selected claim without exposing which lifecycle produced it."
  }

  assert {
    condition     = output.academic_assets.runtime_claim.retained == true
    error_message = "A retained claim must be reported as retained."
  }

  assert {
    condition     = output.managed_addresses.runtime_claim == "kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained[0]"
    error_message = "The adoption helper needs the address of the selected resource."
  }

  assert {
    condition     = output.academic_assets.legacy_quarantine_claim.retained == true
    error_message = "A retained quarantine claim must be reported as retained."
  }
}

run "disposable_acceptance_cluster_applies_and_destroys_cleanly" {
  # Terraform destroys everything this run created during teardown. If a destroy
  # guard were ever reintroduced on the disposable path, teardown would fail here.
  command = apply

  variables {
    academic_assets = merge(var.academic_assets, {
      namespace = "fs2-academic-acceptance"
      runtime_claim = merge(var.academic_assets.runtime_claim, {
        lifecycle = "disposable"
      })
      legacy_quarantine_claim = merge(var.academic_assets.legacy_quarantine_claim, {
        retain = false
      })
    })
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable) == 1
    error_message = "A disposable lifecycle must create the unguarded runtime claim."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained) == 0
    error_message = "A disposable acceptance cluster must not create a destroy-guarded claim."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_legacy_disposable) == 1
    error_message = "retain=false must create the unguarded quarantine claim."
  }

  assert {
    condition     = output.managed_addresses.runtime_claim == "kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable[0]"
    error_message = "Adoption must address the disposable resource when that lifecycle is selected."
  }

  assert {
    condition     = output.academic_assets.runtime_claim.retained == false
    error_message = "A disposable claim must not be reported as retained."
  }
}

run "omitted_lifecycle_defaults_to_disposable" {
  command = plan

  variables {
    academic_assets = merge(var.academic_assets, {
      runtime_claim = {
        name          = "academic-assets-runtime-default"
        storage_gib   = 128
        storage_class = "csi-mounted-fs-path-sc"
        access_mode   = "ReadWriteMany"
      }
      legacy_quarantine_claim = merge(var.academic_assets.legacy_quarantine_claim, {
        enabled = false
        retain  = false
      })
      delivery = merge(var.academic_assets.delivery, {
        deny_egress_on_validate = false
      })
    })
  }

  assert {
    condition = (
      length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable) == 1 &&
      length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained) == 0
    )
    error_message = "Omitting lifecycle must keep the portable default fully disposable."
  }

  assert {
    condition     = length(kubernetes_network_policy_v1.academic_offline_validation) == 0
    error_message = "Offline-validation isolation is opt-in, not a default policy."
  }
}

run "disabled_creates_nothing" {
  command = plan

  variables {
    academic_assets = merge(var.academic_assets, { enabled = false })
  }

  assert {
    condition = (
      length(kubernetes_namespace_v1.academic_assets) == 0 &&
      length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_retained) == 0 &&
      length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable) == 0 &&
      length(kubernetes_network_policy_v1.academic_default_deny) == 0 &&
      length(kubernetes_network_policy_v1.academic_scientific_workloads) == 0 &&
      length(kubernetes_network_policy_v1.academic_offline_validation) == 0
    )
    error_message = "Disabling the feature must create no academic resources at all."
  }

  assert {
    condition     = output.managed_addresses.runtime_claim == null
    error_message = "There is no managed claim to adopt when the feature is disabled."
  }
}

run "scientific_workload_policy_rejects_internet_wide_object_store_routes" {
  command = plan

  variables {
    academic_network_policy = merge(var.academic_network_policy, {
      object_store_cidrs = ["0.0.0.0/0", "::/0"]
    })
  }

  expect_failures = [var.academic_network_policy]
}

run "scientific_workload_policy_rejects_ipv6_32_routes" {
  command = plan

  variables {
    academic_network_policy = merge(var.academic_network_policy, {
      object_store_cidrs = ["2001:db8::/32"]
    })
  }

  expect_failures = [var.academic_network_policy]
}

run "scientific_workload_policy_rejects_ipv6_64_routes" {
  command = plan

  variables {
    academic_network_policy = merge(var.academic_network_policy, {
      object_store_cidrs = ["2001:db8:1::/64"]
    })
  }

  expect_failures = [var.academic_network_policy]
}

run "scientific_workload_policy_accepts_exact_ipv6_hosts" {
  command = plan

  variables {
    academic_network_policy = merge(var.academic_network_policy, {
      object_store_cidrs = ["2001:db8::10/128"]
    })
  }

  assert {
    condition = (
      kubernetes_network_policy_v1.academic_scientific_workloads[0].spec[0].egress[2].to[0].ip_block[0].cidr == "2001:db8::10/128"
    )
    error_message = "An exact IPv6 /128 object-store host must remain admissible."
  }
}

run "delivery_invariants_are_reported_to_consumers" {
  command = plan

  assert {
    condition = (
      output.academic_assets.embeds_licensed_bytes == false &&
      output.academic_assets.consumer_pod_contract.world_readable == false &&
      output.academic_assets.consumer_pod_contract.read_only == true &&
      output.academic_assets.consumer_pod_contract.supplemental_groups == [65532]
    )
    error_message = "Consumers must be told to mount read-only via the asset group, never to embed or world-read."
  }

  assert {
    condition     = output.academic_assets.legacy_quarantine_claim.mountable == false
    error_message = "The quarantine claim is never runtime mountable."
  }

  assert {
    condition = (
      output.academic_assets.default_deny_enforced == true &&
      output.managed_addresses.default_deny_policy == "kubernetes_network_policy_v1.academic_default_deny[0]"
    )
    error_message = "Consumers and adoption tooling must see the Terraform-owned namespace boundary."
  }
}

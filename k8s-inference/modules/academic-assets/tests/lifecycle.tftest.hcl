# Provider-mocked contract tests for claim lifecycle selection.
#
# The point of these is that a long-lived cluster cannot discard verified
# licensed bytes, while a throwaway acceptance cluster still tears down cleanly.
# Terraform's own teardown runs a destroy after each apply run, so the disposable
# cases below fail if a destroy guard is ever reintroduced on that path.

mock_provider "kubernetes" {}

variables {
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
      length(kubernetes_network_policy_v1.academic_offline_validation) == 0
    )
    error_message = "Disabling the feature must create no academic resources at all."
  }

  assert {
    condition     = output.managed_addresses.runtime_claim == null
    error_message = "There is no managed claim to adopt when the feature is disabled."
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
}

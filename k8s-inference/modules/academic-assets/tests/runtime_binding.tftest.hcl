# The advertised mount must be the contracted one.
#
# Deriving a path from the asset key was wrong twice over: it pointed PyRosetta at
# the wheel directory instead of the installed tree, and it could not express the
# canonical AlphaFold 3 location that model onboarding asks for.

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
      lifecycle     = "disposable"
    }
    legacy_quarantine_claim = {
      enabled     = false
      namespace   = "fs2-models"
      name        = "cancer-immunotherapy-academic-assets-rwx-v1"
      storage_gib = 128
      retain      = false
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
        runtime_binding = {
          artifact_id           = "alphafold3-parameters"
          source_sub_path       = "alphafold3/af3.bin.zst"
          consumer_path         = "/models/af3.bin.zst"
          mechanism             = "subpath-file-mount"
          content_digest_sha256 = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
          size_bytes            = 1020545840
        }
      }
      pyrosetta-bindcraft = {
        model_id              = "bindcraft"
        relative_path         = "pyrosetta-bindcraft/pyrosetta-2026.29+releasequarterly.80a0635615-cp310-cp310-linux_x86_64.whl"
        install_relative_path = "pyrosetta-bindcraft/site-packages"
        read_only             = true
        runtime_binding = {
          artifact_id           = "bindcraft-pyrosetta"
          source_sub_path       = "pyrosetta-bindcraft/site-packages"
          consumer_path         = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
          mechanism             = "subpath-directory-mount"
          content_digest_sha256 = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
          size_bytes            = 1667097173
        }
      }
    }
    readiness_manifest_sha256 = null
  }
}

run "alphafold3_binding_is_the_canonical_onboarding_location" {
  command = plan

  assert {
    condition     = output.academic_assets.runtime_bindings["alphafold3"].artifact_id == "alphafold3-parameters"
    error_message = "AlphaFold 3 must be advertised under the artifact ID onboarding uses."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["alphafold3"].consumer_path == "/models/af3.bin.zst"
    error_message = "AlphaFold 3 must be advertised at the canonical /models/af3.bin.zst."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["alphafold3"].source_sub_path == "alphafold3/af3.bin.zst"
    error_message = "The parameters must be localized from their exact subPath on the claim."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["alphafold3"].mechanism == "subpath-file-mount"
    error_message = "A single parameter file is localized by subPath file mount."
  }

  assert {
    condition = (
      output.academic_assets.runtime_bindings["alphafold3"].content_digest_sha256 == "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff" &&
      output.academic_assets.runtime_bindings["alphafold3"].size_bytes == 1020545840
    )
    error_message = "The advertised mount must carry the immutable identity of the object it exposes."
  }
}

run "pyrosetta_binding_points_at_the_installed_tree_not_the_wheel" {
  command = plan

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].source_sub_path == "pyrosetta-bindcraft/site-packages"
    error_message = "BindCraft consumes the installed tree; advertising the wheel directory would be wrong."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].consumer_path == "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
    error_message = "The advertised consumer path must be the installed site-packages tree."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].mechanism == "subpath-directory-mount"
    error_message = "An installed tree is localized by subPath directory mount."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].artifact_id == "bindcraft-pyrosetta"
    error_message = "BindCraft's prerequisite must be advertised under the artifact ID onboarding uses."
  }
}

run "every_binding_is_read_only_and_bound_to_the_selected_claim" {
  command = plan

  assert {
    condition = alltrue([
      for key, binding in output.academic_assets.runtime_bindings :
      binding.read_only && binding.claim == "academic-assets-runtime-rwx"
    ])
    error_message = "Bindings mount the selected claim read-only."
  }

  assert {
    condition     = length(output.academic_assets.assets_without_a_declared_binding) == 0
    error_message = "Both assets declare a binding in this configuration."
  }
}

run "an_asset_without_a_binding_is_reported_not_invented" {
  command = plan

  variables {
    academic_assets = merge(var.academic_assets, {
      assets = {
        alphafold3 = {
          model_id        = "alphafold3"
          relative_path   = "alphafold3/af3.bin.zst"
          read_only       = true
          runtime_binding = null
        }
      }
    })
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["alphafold3"] == null
    error_message = "Without a declared binding the module must not derive a mount path."
  }

  assert {
    condition     = join(",", output.academic_assets.assets_without_a_declared_binding) == "alphafold3"
    error_message = "An asset with no binding must be named, not silently mounted somewhere."
  }
}

run "an_unsafe_binding_is_refused" {
  command = plan

  variables {
    academic_assets = merge(var.academic_assets, {
      assets = {
        alphafold3 = {
          model_id      = "alphafold3"
          relative_path = "alphafold3/af3.bin.zst"
          read_only     = true
          runtime_binding = {
            artifact_id           = "alphafold3-parameters"
            source_sub_path       = "../escape/af3.bin.zst"
            consumer_path         = "/models/af3.bin.zst"
            mechanism             = "subpath-file-mount"
            content_digest_sha256 = null
            size_bytes            = null
          }
        }
      }
    })
  }

  expect_failures = [var.academic_assets]
}

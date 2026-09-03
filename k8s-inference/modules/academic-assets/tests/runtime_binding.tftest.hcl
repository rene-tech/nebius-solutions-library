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
          artifact_id                = "alphafold3-parameters"
          source_sub_path            = "alphafold3/af3.bin.zst"
          consumer_path              = "/models/af3.bin.zst"
          mechanism                  = "subpath-file-mount"
          content_identity_kind      = "file-digest"
          content_manifest_algorithm = null
          content_digest_sha256      = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
          size_bytes                 = 1020545840
          source_artifact = {
            filename   = "af3.bin.zst"
            sha256     = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"
            size_bytes = 1020545840
          }
        }
      }
      pyrosetta-bindcraft = {
        model_id              = "bindcraft"
        relative_path         = "pyrosetta-bindcraft/pyrosetta-2026.29+releasequarterly.80a0635615-cp310-cp310-linux_x86_64.whl"
        install_relative_path = "pyrosetta-bindcraft/site-packages"
        read_only             = true
        runtime_binding = {
          artifact_id                = "bindcraft-pyrosetta-installed-tree"
          source_sub_path            = "pyrosetta-bindcraft/site-packages"
          consumer_path              = "/opt/fs2/academic/pyrosetta-bindcraft/site-packages"
          mechanism                  = "subpath-directory-mount"
          content_identity_kind      = "tree-manifest"
          content_manifest_algorithm = "fs2-tree-manifest/v1"
          # Observed at install time; the wheel identity is the source artifact.
          content_digest_sha256 = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
          size_bytes            = 3287122494
          source_artifact = {
            filename   = "pyrosetta-2026.29+releasequarterly.80a0635615-cp310-cp310-linux_x86_64.whl"
            sha256     = "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
            size_bytes = 1667097173
          }
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
      output.academic_assets.runtime_bindings["alphafold3"].content_identity_kind == "file-digest" &&
      output.academic_assets.runtime_bindings["alphafold3"].content_digest_sha256 == "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff" &&
      output.academic_assets.runtime_bindings["alphafold3"].size_bytes == 1020545840
    )
    error_message = "A single file is identified by its own digest."
  }
}

run "pyrosetta_tree_is_identified_by_its_manifest_not_by_the_wheel" {
  command = plan

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].content_identity_kind == "tree-manifest"
    error_message = "An installed directory is identified by a tree manifest, not a file digest."
  }

  assert {
    condition = (
      output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].content_digest_sha256 !=
      output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].source_artifact.sha256
    )
    error_message = "The installed tree must not be labelled with the wheel digest."
  }

  assert {
    condition = (
      output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].size_bytes !=
      output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].source_artifact.size_bytes
    )
    error_message = "The installed tree must not be labelled with the wheel size."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].source_artifact.sha256 == "4383d8d1a14fd3aff52983de936908791cc77bc6ac418e3bc53bb963a42c5242"
    error_message = "The wheel stays recorded as the separate source artifact."
  }

  assert {
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].content_manifest_algorithm == "fs2-tree-manifest/v1"
    error_message = "A tree identity must name the manifest algorithm that produced it."
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
    condition     = output.academic_assets.runtime_bindings["pyrosetta-bindcraft"].artifact_id == "bindcraft-pyrosetta-installed-tree"
    error_message = "BindCraft's mounted installed tree must use its own artifact ID, not the source wheel's ID."
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

run "execution_is_bound_to_the_namespace_that_holds_the_claim" {
  command = plan

  assert {
    condition = (
      output.academic_assets.execution.namespace ==
      output.academic_assets.execution.claim_namespace
    )
    error_message = "A claim is mountable only from its own namespace, so execution must run there."
  }

  assert {
    condition     = output.academic_assets.execution.cross_namespace_mount == false
    error_message = "A cross-namespace claim mount must never be advertised as working."
  }

  assert {
    condition = (
      output.academic_assets.execution.local_queue_manifest.metadata.namespace ==
      output.academic_assets.execution.claim_namespace
    )
    error_message = "The queue that admits academic work must live in the claim's namespace."
  }

  assert {
    condition     = output.academic_assets.execution.local_queue_manifest.kind == "LocalQueue"
    error_message = "Academic work is admitted through a Kueue LocalQueue."
  }

  assert {
    condition = (
      length(kubernetes_service_account_v1.academic_runner) == 1 &&
      length(kubernetes_role_v1.academic_execution) == 1 &&
      length(kubernetes_role_binding_v1.academic_execution) == 1 &&
      length(kubernetes_manifest.academic_local_queue) == 1
    )
    error_message = "Execution needs a runner identity and permissions in the claim's namespace."
  }

  assert {
    condition     = kubernetes_role_v1.academic_execution[0].metadata[0].namespace == var.academic_assets.namespace
    error_message = "The execution Role must be namespaced to the claim's namespace."
  }

  assert {
    condition = one([
      for subject in kubernetes_role_binding_v1.academic_execution[0].subject : subject.name
      if subject.namespace == "fs2-system"
    ]) == "fs2-serve-control-plane-runtime"
    error_message = "The default cross-namespace binding must name the controller's actual runtime service account."
  }

  assert {
    condition = (
      kubernetes_manifest.academic_local_queue["academic-scientific"].manifest.metadata.namespace ==
      var.academic_assets.namespace
    )
    error_message = "Enabling academic execution must create its LocalQueue in the claim namespace."
  }

  assert {
    condition = (
      length(terraform_data.academic_local_queue_binding) == 1 &&
      terraform_data.academic_local_queue_binding["academic-scientific"].input.cluster_queue ==
      var.academic_assets.execution.cluster_queue &&
      output.managed_addresses.local_queue_binding ==
      "terraform_data.academic_local_queue_binding[\"academic-scientific\"]"
    )
    error_message = "LocalQueue.spec.clusterQueue is immutable, so the binding identity must live in state and force replacement rather than an in-place update."
  }

  assert {
    condition = (
      contains(flatten(kubernetes_role_v1.academic_execution[0].rule[*].resources), "jobsets") &&
      contains(flatten(kubernetes_role_v1.academic_execution[0].rule[*].resources), "workloads")
    )
    error_message = "The controller Role must cover JobSet execution and Kueue admission observation."
  }
}

run "execution_can_be_switched_off_without_removing_the_assets" {
  command = plan

  variables {
    academic_assets = merge(var.academic_assets, {
      execution = {
        enabled                    = false
        local_queue                = "academic-scientific"
        cluster_queue              = "inference-accelerators"
        service_account            = "fs2-academic-runner"
        controller_namespace       = "fs2-system"
        controller_service_account = "fs2-serve-control-plane-runtime"
      }
    })
  }

  assert {
    condition = (
      length(kubernetes_service_account_v1.academic_runner) == 0 &&
      length(kubernetes_role_v1.academic_execution) == 0 &&
      length(kubernetes_manifest.academic_local_queue) == 0 &&
      output.academic_assets.execution.enabled == false
    )
    error_message = "Disabling execution must remove only the execution objects."
  }

  assert {
    condition     = length(kubernetes_persistent_volume_claim_v1.academic_assets_runtime_disposable) == 1
    error_message = "The claim itself must survive disabling execution."
  }
}

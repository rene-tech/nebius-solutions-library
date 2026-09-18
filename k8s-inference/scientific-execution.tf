locals {
  # Match the runtime renderer: optional snapshot and historical-proof metadata
  # do not change the normal-load recipe. The complete Helm configuration keeps
  # its separate raw digest in scientific_execution_map_sha256.
  scientific_execution_recipe_sha256 = sha256(jsonencode({
    for key, value in local.scientific_execution_map : key => value
    if !contains(["snapshot_bundles", "qualification_baselines"], key)
  }))
  scientific_qualification_baselines = try(local.scientific_execution_map.qualification_baselines, {})

  # Adding a model must not invalidate unchanged models. A historical receipt
  # is accepted only when reconstructing its ordered model set from CURRENT
  # rows gives the exact measured digest. Changed or missing rows fail here.
  scientific_qualification_baselines_valid = {
    for digest, model_ids in local.scientific_qualification_baselines : digest => try(
      can(regex("^[a-f0-9]{64}$", digest)) &&
      length(model_ids) > 0 && length(model_ids) <= 256 &&
      length(distinct(model_ids)) == length(model_ids) &&
      sha256(jsonencode({
        schema = local.scientific_execution_map.schema
        models = [for model_id in model_ids : one([
          for model in local.scientific_execution_map.models : model
          if model.model_id == model_id
        ])]
      })) == digest,
      false,
    )
  }

  scientific_execution_identities_valid = try(
    length(local.scientific_qualification_baselines) <= 32 &&
    alltrue(values(local.scientific_qualification_baselines_valid)) &&
    alltrue([
      for model in local.scientific_execution_map.models :
      local.scientific_workload_profiles_by_model_id[model.model_id].execution_identity.execution_identity_sha256 == model.execution_identity_sha256 &&
      (
        local.scientific_workload_profiles_by_model_id[model.model_id].qualification.execution_map_sha256 == local.scientific_execution_recipe_sha256 ||
        (
          try(local.scientific_qualification_baselines_valid[
            local.scientific_workload_profiles_by_model_id[model.model_id].qualification.execution_map_sha256
          ], false) &&
          try(contains(local.scientific_qualification_baselines[
            local.scientific_workload_profiles_by_model_id[model.model_id].qualification.execution_map_sha256
          ], model.model_id), false)
        )
      )
    ]),
    false,
  )
}

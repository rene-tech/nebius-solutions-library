# Terraform is the upgrade owner for the ModelDeployment API. Helm's `crds/`
# directory supports a fresh standalone chart install but deliberately does not
# upgrade CRDs. Applying the same source here gives normal Terraform plans an
# explicit schema diff and establishes the API before any controller release.
locals {
  model_deployment_crd = yamldecode(file(
    "${local.fs2_root}/charts/control-plane/fs2-serve-control-plane/crds/modeldeployments.inference.fs2.nebius.ai.yaml"
  ))
}

resource "kubernetes_manifest" "model_deployment_crd" {
  manifest = local.model_deployment_crd

  field_manager {
    force_conflicts = false
    name            = "fs2-${var.run_id}-model-api"
  }

  wait {
    condition {
      type   = "Established"
      status = "True"
    }
  }

  timeouts {
    create = "10m"
    update = "10m"
  }

  depends_on = [terraform_data.cluster_contract]
}

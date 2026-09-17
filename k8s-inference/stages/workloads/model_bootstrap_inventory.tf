# Recovery authority comes from the cluster objects themselves, not from an
# operator copying a Terraform output into a later input. These generic reads
# inventory the complete name prefix so a missing/mislabeled half-generation
# cannot be silently excluded by a label selector.
data "kubernetes_resources" "model_controller_bootstrap_configmaps" {
  api_version = "v1"
  kind        = "ConfigMap"
  namespace   = "fs2-system"
}

data "kubernetes_resources" "model_controller_bootstrap_jobs" {
  api_version = "batch/v1"
  kind        = "Job"
  namespace   = "fs2-system"
}

# A durable receipt is public, payload-free proof signed by the same external
# release authority which minted the spent models.bootstrap assertion.  The
# trust document contains public keys only.  Signature verification happens
# before any discovered object becomes an import target.
data "external" "model_controller_bootstrap_receipt" {
  for_each = local.model_controller_bootstrap_receipt_queries
  program  = ["python3", "${path.module}/scripts/verify_model_bootstrap_receipt.py"]
  query    = each.value
}

# Declarative imports make a retained Kubernetes generation sufficient to
# recover lost Terraform state. The inventory-derived spec is validated before
# either imported object can be planned as managed state.
import {
  for_each = local.model_controller_bootstrap_verified_specs
  to       = kubernetes_config_map_v1.model_controller_bootstrap[each.key]
  id       = "fs2-system/fs2-model-bootstrap-${each.key}"
}

import {
  for_each = local.model_controller_bootstrap_verified_job_specs
  to       = kubernetes_job_v1.model_controller_bootstrap[each.key]
  id       = "fs2-system/fs2-model-bootstrap-${each.key}"
}

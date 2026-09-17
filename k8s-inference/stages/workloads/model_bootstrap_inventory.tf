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

# Declarative imports make a retained Kubernetes generation sufficient to
# recover lost Terraform state. The inventory-derived spec is validated before
# either imported object can be planned as managed state.
import {
  for_each = local.model_controller_bootstrap_discovered_specs
  to       = kubernetes_config_map_v1.model_controller_bootstrap[each.key]
  id       = "fs2-system/fs2-model-bootstrap-${each.key}"
}

import {
  for_each = local.model_controller_bootstrap_discovered_job_specs
  to       = kubernetes_job_v1.model_controller_bootstrap[each.key]
  id       = "fs2-system/fs2-model-bootstrap-${each.key}"
}

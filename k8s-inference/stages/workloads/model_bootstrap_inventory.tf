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

# Recovery is deliberately two-stage. These policy objects are installed and
# bound first, then their immutable UIDs are integration-pinned before any
# history inventory may become a verification or import candidate.
data "kubernetes_resources" "model_controller_bootstrap_admission_policies" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicy"
}

data "kubernetes_resources" "model_controller_bootstrap_admission_bindings" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingAdmissionPolicyBinding"
}

# This boundary is provisioned and rotated by Platform Security, not by this
# Terraform state. It performs the live current-epoch/JTI decision which a
# static CEL router cannot safely rotate. Source consumes only its externally
# pinned UID/full-object/manifest identity and refuses to create or adopt it.
data "kubernetes_resources" "release_identity_security_webhooks" {
  api_version = "admissionregistration.k8s.io/v1"
  kind        = "ValidatingWebhookConfiguration"
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

import {
  for_each = local.model_controller_bootstrap_verified_receipts
  to       = kubernetes_job_v1.model_controller_bootstrap_receipt_verification[each.key]
  id = "fs2-system/${local.model_controller_bootstrap_verification_queries[
    each.key
  ].verification_name}"
}

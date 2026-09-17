provider "kubernetes" {
  config_path    = pathexpand(var.security_owner_kubeconfig_path)
  config_context = var.security_owner_kube_context
}

resource "kubernetes_config_map_v1" "activation_trust" {
  for_each = var.fence_generations
  metadata {
    name      = each.value.activation_trust_config_map_name
    namespace = "fs2-system"
    labels = {
      "app.kubernetes.io/managed-by"             = "fs2-external-security-owner"
      "security.fs2.nebius.ai/fence-generation" = each.key
    }
  }
  immutable = true
  data      = { "public-key.pem" = each.value.activation_public_key_pem }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "kubernetes_config_map_v1" "activation_ca" {
  for_each = var.fence_generations
  metadata {
    name      = each.value.activation_ca_config_map_name
    namespace = "fs2-system"
    labels = {
      "app.kubernetes.io/managed-by"             = "fs2-external-security-owner"
      "security.fs2.nebius.ai/fence-generation" = each.key
    }
  }
  immutable = true
  data      = { "ca.crt" = base64decode(each.value.ca_bundle) }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

# Exact nondelegatable authority for the CAS executor. The external identity
# can observe Pods and patch only the retained generation-named Deployments;
# it cannot create/delete workloads, read Secrets, mint tokens, or mutate
# RBAC/admission objects through this grant.
resource "kubernetes_role_v1" "cutover_executor" {
  for_each = var.fence_generations
  metadata {
    name      = "fs2-storage-cutover-${each.key}"
    namespace = "fs2-system"
    labels = {
      "app.kubernetes.io/managed-by"             = "fs2-external-security-owner"
      "security.fs2.nebius.ai/fence-generation" = each.key
    }
  }
  rule {
    api_groups     = ["apps"]
    resources      = ["deployments"]
    resource_names = sort(tolist(each.value.cutover_deployment_names))
    verbs           = ["get", "patch", "update"]
  }
  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "kubernetes_role_binding_v1" "cutover_executor" {
  for_each = var.fence_generations
  metadata {
    name      = "fs2-storage-cutover-${each.key}"
    namespace = "fs2-system"
    labels = {
      "app.kubernetes.io/managed-by"             = "fs2-external-security-owner"
      "security.fs2.nebius.ai/fence-generation" = each.key
    }
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "User"
    name      = each.value.cutover_executor_username
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.cutover_executor[each.key].metadata[0].name
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

# The admission service is outside the protected cluster and consumes the
# root-owned runtime registry directly. This root installs only the versioned
# fail-closed webhook under the separate security-owner identity. Ordinary
# workload and release identities have no authority over these objects.
resource "kubernetes_manifest" "daemonset_fence" {
  for_each = var.fence_generations
  manifest = {
    apiVersion = "admissionregistration.k8s.io/v1"
    kind       = "ValidatingWebhookConfiguration"
    metadata = {
      name = each.value.name
      labels = {
        "app.kubernetes.io/managed-by"          = "fs2-external-security-owner"
        "security.fs2.nebius.ai/fence-generation" = each.key
      }
      annotations = {
        "security.fs2.nebius.ai/enforcer-bundle-sha256"    = each.value.enforcer_bundle_sha256
        "security.fs2.nebius.ai/enforcer-image-digest"     = each.value.enforcer_image_digest
        "security.fs2.nebius.ai/runtime-state-head-sha256" = each.value.runtime_state_head_sha256
        "security.fs2.nebius.ai/owner-receipt-sha256"      = each.value.security_owner_receipt_sha256
        "security.fs2.nebius.ai/cutover-source-bundle-sha256" = each.value.cutover_source_bundle_sha256
        "security.fs2.nebius.ai/cutover-receipt-sha256"       = each.value.cutover_receipt_sha256
      }
    }
    webhooks = [{
      name                    = "daemonset-fence.${each.key}.security.fs2.nebius.ai"
      admissionReviewVersions = ["v1"]
      sideEffects             = "NoneOnDryRun"
      failurePolicy           = "Fail"
      matchPolicy             = "Equivalent"
      timeoutSeconds          = 5
      clientConfig = {
        url      = each.value.external_url
        caBundle = each.value.ca_bundle
      }
      rules = [
        {
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["daemonsets"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE"]
          resources   = ["pods"]
          scope       = "Namespaced"
        },
      ]
    }, {
      name                    = "storage-reconciler-cutover.${each.key}.security.fs2.nebius.ai"
      admissionReviewVersions = ["v1"]
      sideEffects             = "NoneOnDryRun"
      failurePolicy           = "Fail"
      matchPolicy             = "Equivalent"
      timeoutSeconds          = 5
      clientConfig = {
        url      = each.value.cutover_url
        caBundle = each.value.ca_bundle
      }
      rules = [
        {
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["UPDATE", "DELETE"]
          resources   = ["deployments"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = ["apps"]
          apiVersions = ["v1"]
          operations  = ["CREATE", "UPDATE", "DELETE"]
          resources   = ["replicasets"]
          scope       = "Namespaced"
        },
        {
          apiGroups   = [""]
          apiVersions = ["v1"]
          operations  = ["CREATE"]
          resources   = ["pods"]
          scope       = "Namespaced"
        },
      ]
      matchConditions = [{
        name = "customer-storage-reconciler-only"
        expression = "request.namespace == 'fs2-system' && ((has(object.metadata.labels) && object.metadata.labels.exists(key, value, key == 'app.kubernetes.io/component' && value == 'storage-reconciler-v3')) || (request.operation in ['UPDATE','DELETE'] && has(oldObject.metadata.labels) && oldObject.metadata.labels.exists(key, value, key == 'app.kubernetes.io/component' && value == 'storage-reconciler-v3')))"
      }]
    }]
  }
  field_manager {
    force_conflicts = false
    name            = "fs2-external-daemonset-fence"
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

resource "terraform_data" "retained_fence_custody" {
  input = {
    retained_generation_receipt_sha256 = var.retained_generation_receipt_sha256
    generations_sha256                 = sha256(jsonencode(var.fence_generations))
    installed_names = {
      for generation, fence in kubernetes_manifest.daemonset_fence :
      generation => fence.object.metadata.name
    }
    installed_cutover_roles = {
      for generation, role in kubernetes_role_v1.cutover_executor :
      generation => {
        name = role.metadata[0].name
        uid  = role.metadata[0].uid
      }
    }
    installed_cutover_bindings = {
      for generation, binding in kubernetes_role_binding_v1.cutover_executor :
      generation => {
        name = binding.metadata[0].name
        uid  = binding.metadata[0].uid
      }
    }
  }
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

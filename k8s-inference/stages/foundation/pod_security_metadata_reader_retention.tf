# Tokenless, metadata-only Secret inventory identity required by the signed
# SAI-07 baseline/legacy-quarantine evidence path. These are platform-state
# resources with prevent_destroy; the external executor cannot create or alter
# RBAC. The only credential is an exact-name, ten-minute, anchor-bound
# TokenRequest issued by the separately audited receipt-custodian group.

resource "kubernetes_service_account_v1" "pod_security_metadata_reader" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-metadata-reader"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  automount_service_account_token = false

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_namespace_v1.platform]
}

resource "kubernetes_role_v1" "pod_security_secret_metadata_reader" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-secret-metadata-reader"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["list"]
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [kubernetes_namespace_v1.platform]
}

resource "kubernetes_role_binding_v1" "pod_security_secret_metadata_reader" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-secret-metadata-reader"
    namespace = "fs2-models"
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.pod_security_secret_metadata_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.pod_security_metadata_reader.metadata[0].name
    namespace = "fs2-system"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_role_v1" "pod_security_token_anchor_metadata_reader" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-token-anchor-metadata-reader"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  rule {
    api_groups = [""]
    resources  = ["secrets"]
    verbs      = ["list"]
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_role_binding_v1" "pod_security_token_anchor_metadata_reader" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-token-anchor-metadata-reader"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.pod_security_token_anchor_metadata_reader.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.pod_security_metadata_reader.metadata[0].name
    namespace = "fs2-system"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_role_v1" "pod_security_metadata_reader_token_request" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-metadata-reader-token-request"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  rule {
    api_groups     = [""]
    resources      = ["serviceaccounts/token"]
    resource_names = ["fs2-pod-security-metadata-reader"]
    verbs          = ["create"]
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "kubernetes_role_binding_v1" "pod_security_metadata_reader_token_request" {
  provider = kubernetes.pod_security_custody

  metadata {
    name      = "fs2-pod-security-metadata-reader-token-request"
    namespace = "fs2-system"
    labels    = local.common_labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.pod_security_metadata_reader_token_request.metadata[0].name
  }
  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = "fs2-pod-security-receipt-custodians"
  }

  lifecycle {
    prevent_destroy = true
  }
}

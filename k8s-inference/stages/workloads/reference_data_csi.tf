# The general cache CSI driver is rooted on /mnt/fs2cache and cannot safely
# back the retained reference-data claim.  This separately named driver uses a
# distinct kubelet socket and the dedicated filesystem already attached at
# /mnt/fs2-reference-data.  Its StorageClass is post-rendered to Retain because
# chart 0.1.7 does not expose reclaimPolicy as a value.
resource "helm_release" "reference_data_csi" {
  count = var.reference_data.enabled ? 1 : 0

  name             = "fs2-reference-data-retained"
  namespace        = "kube-system"
  repository       = "oci://cr.eu-north1.nebius.cloud/mk8s/helm"
  chart            = "csi-mounted-fs-path"
  version          = "0.1.7"
  create_namespace = false
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 900

  values = [yamlencode({
    fullnameOverride = "fs2-reference-data-retained"
    driverName       = "reference-data.mounted-fs-path.csi.nebius.ai"
    dataDir          = "/mnt/fs2-reference-data/csi-mounted-fs-path-data/"
    affinity = {
      nodeAffinity = {
        requiredDuringSchedulingIgnoredDuringExecution = {
          nodeSelectorTerms = [{
            matchExpressions = [{
              key      = "storage.fs2.nebius/reference-data"
              operator = "In"
              values   = ["true"]
            }]
          }]
        }
      }
    }
    tolerations = [{ operator = "Exists" }]
  })]

  postrender = {
    binary_path = abspath("${path.module}/scripts/harden-reference-data-csi.py")
  }

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = (
        var.reference_data.storage_contract.lifecycle.retention_mode == "retain" &&
        var.reference_data.storage_contract.filesystem.forbid_deletion &&
        var.reference_data.storage_contract.filesystem.size_gib >= 1611 &&
        var.reference_data.storage_contract.filesystem.node_mount_path == "/mnt/fs2-reference-data" &&
        var.reference_data.storage_contract.filesystem.host_path == "/mnt/fs2-reference-data/data"
      )
      error_message = "The reference-data CSI driver requires the dedicated retained, deletion-forbidden filesystem mounted at /mnt/fs2-reference-data with at least 1611 GiB."
    }
  }

  depends_on = [terraform_data.cluster_contract]
}

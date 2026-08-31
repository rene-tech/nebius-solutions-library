resource "terraform_data" "kueue_deployment_admission_ready" {
  input = {
    schema              = "fs2-serve.nebius.ai/kueue-deployment-admission-ready/v1"
    cluster_id          = var.cluster_id
    kube_system_uid     = var.kube_system_uid
    kueue_release_name  = helm_release.kueue.name
    kueue_chart_version = helm_release.kueue.version
    probe = {
      api_version = "apps/v1"
      kind        = "Deployment"
      namespace   = kubernetes_namespace_v1.platform["kserve"].metadata[0].name
      operation   = "CREATE"
      persistence = "server-side-dry-run"
    }
  }

  # Re-run the proof whenever either the disposable cluster identity, the
  # Kueue release contract, or the reviewed probe implementation changes.
  triggers_replace = {
    cluster_id               = var.cluster_id
    kube_system_uid          = var.kube_system_uid
    source_commit            = var.accelerator_pool_contract.source_commit
    accelerator_contract_sha = local.accelerator_pool_contract_sha256
    release_name             = helm_release.kueue.name
    release_repository       = helm_release.kueue.repository
    release_chart            = helm_release.kueue.chart
    release_version          = helm_release.kueue.version
    release_atomic           = tostring(helm_release.kueue.atomic)
    release_wait             = tostring(helm_release.kueue.wait)
    release_timeout          = tostring(helm_release.kueue.timeout)
    release_values_sha       = sha256(join("\n", coalesce(helm_release.kueue.values, [])))
    probe_script_sha         = filesha256("${path.module}/scripts/wait-for-kueue-deployment-admission.sh")
  }

  # This is creation-only. Destroy removes the receipt from state without
  # contacting Kubernetes, then continues through the reverse Helm graph.
  provisioner "local-exec" {
    command = "\"${path.module}/scripts/wait-for-kueue-deployment-admission.sh\""
    quiet   = true

    environment = {
      FS2_GATE_KUBECONFIG      = abspath(var.kubeconfig_path)
      FS2_GATE_RUN_ROOT        = abspath(var.run_root)
      FS2_GATE_KUBE_CONTEXT    = var.kube_context
      FS2_GATE_CLUSTER_ID      = var.cluster_id
      FS2_GATE_CLUSTER_NAME    = var.cluster_name
      FS2_GATE_KUBE_SYSTEM_UID = var.kube_system_uid
      FS2_GATE_RUN_ID          = var.run_id
      FS2_GATE_KUEUE_RELEASE   = helm_release.kueue.name
      FS2_GATE_TIMEOUT_SECONDS = "180"
      FS2_GATE_RETRY_SECONDS   = "2"
    }
  }

  depends_on = [
    helm_release.kueue,
    kubernetes_namespace_v1.platform["kserve"],
  ]
}

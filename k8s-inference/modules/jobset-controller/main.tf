locals {
  api_version       = "jobset.x-k8s.io/v1alpha2"
  chart_repository  = "oci://registry.k8s.io/jobset/charts"
  chart_name        = "jobset"
  chart_version     = "0.12.0"
  chart_digest      = "sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
  chart_archive_sha = "bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a"
  chart_ref_base    = "${local.chart_repository}/${local.chart_name}"
  chart_ref         = "${local.chart_repository}/${local.chart_name}@${local.chart_digest}"
  # The exact verified bytes, addressed by their own archive SHA-256.
  chart_archive     = var.enabled ? data.external.chart[0].result.path : null
  image_repository  = "registry.k8s.io/jobset/jobset"
  image_tag         = "v0.12.0"
  image_digest      = "sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
  release_name      = "fs2-${var.run_id}-jobset"
  controller_name   = "${local.release_name}-controller"
  kubernetes_parts  = split(".", trimprefix(var.kubernetes_version, "v"))
  kubernetes_minor  = "v${local.kubernetes_parts[0]}.${local.kubernetes_parts[1]}"
  compatibility_url = "https://github.com/kubernetes-sigs/jobset/releases/tag/v0.12.0"
  install_url       = "https://jobset.sigs.k8s.io/docs/installation/"
}

data "external" "chart" {
  count = var.enabled ? 1 : 0

  program = ["${path.module}/scripts/materialize-chart.sh"]

  query = {
    chart_ref      = local.chart_ref_base
    chart_digest   = local.chart_digest
    archive_sha256 = local.chart_archive_sha
    chart_name     = local.chart_name
    run_root       = abspath(var.run_root)
  }
}

resource "terraform_data" "contract" {
  input = {
    schema                        = "fs2-serve.nebius.ai/jobset-foundation/v1"
    enabled                       = var.enabled
    api_version                   = local.api_version
    configured_kubernetes_version = var.kubernetes_version
    kubernetes_minor              = local.kubernetes_minor
    # Exactly the minors JobSet v0.12.0 itself tests end to end.
    upstream_tested_kubernetes_minors = ["v1.32", "v1.33", "v1.34"]
    namespace                         = var.namespace
    release_name                      = local.release_name
    controller_name                   = local.controller_name
    chart = {
      repository     = local.chart_repository
      name           = local.chart_name
      version        = local.chart_version
      digest         = local.chart_digest
      archive_sha256 = local.chart_archive_sha
    }
    image = {
      repository = local.image_repository
      tag        = local.image_tag
      digest     = local.image_digest
    }
    sources = {
      installation = local.install_url
      release      = local.compatibility_url
    }
  }

  lifecycle {
    precondition {
      condition = (
        can(regex("^[a-z][a-z0-9]{5,11}$", var.run_id)) &&
        var.namespace == "jobset-system" &&
        tonumber(local.kubernetes_parts[0]) == 1 &&
        contains([32, 33, 34], tonumber(local.kubernetes_parts[1])) &&
        local.api_version == "jobset.x-k8s.io/v1alpha2" &&
        can(regex("^sha256:[a-f0-9]{64}$", local.chart_digest)) &&
        can(regex("^[a-f0-9]{64}$", local.chart_archive_sha)) &&
        can(regex("^sha256:[a-f0-9]{64}$", local.image_digest))
      )
      error_message = "JobSet must retain the pinned v0.12.0 chart/image identities, v1alpha2 API, jobset-system namespace, and a Kubernetes 1.32-1.34 deployment minor."
    }
  }
}

# The upstream JobSet Helm chart packages its CRD under chart crds/. Helm does
# not upgrade or delete those CRDs. Keep the upgrade as its own Terraform state
# object, keyed by the reviewed chart digest, and apply the chart's exact CRD
# server-side before Helm. There is deliberately no destroy provisioner: CRD
# deletion could cascade all JobSet objects.
resource "terraform_data" "crd_upgrade" {
  count = var.enabled ? 1 : 0

  input = {
    chart_digest     = local.chart_digest
    api_version      = local.api_version
    kubernetes_minor = local.kubernetes_minor
  }

  triggers_replace = {
    cluster_id       = var.cluster_id
    chart_digest     = local.chart_digest
    chart_archive    = local.chart_archive
    kubernetes_minor = local.kubernetes_minor
    installer_sha256 = filesha256("${path.module}/scripts/apply-jobset-crd.sh")
  }

  provisioner "local-exec" {
    command = "\"${path.module}/scripts/apply-jobset-crd.sh\""
    quiet   = true

    environment = {
      FS2_JOBSET_CHART_ARCHIVE        = local.chart_archive
      FS2_JOBSET_CHART_ARCHIVE_SHA256 = local.chart_archive_sha
      FS2_JOBSET_KUBECONFIG           = abspath(var.kubeconfig_path)
      FS2_JOBSET_CONTEXT              = var.kube_context
      FS2_JOBSET_RUN_ROOT             = abspath(var.run_root)
      FS2_JOBSET_KUBERNETES_MINOR     = local.kubernetes_minor
    }
  }

  depends_on = [terraform_data.artifacts_verified]
}

resource "terraform_data" "artifacts_verified" {
  count = var.enabled ? 1 : 0

  input = {
    chart_digest         = local.chart_digest
    chart_archive_sha256 = local.chart_archive_sha
    image_digest         = local.image_digest
  }

  triggers_replace = {
    chart_digest         = local.chart_digest
    chart_archive_sha256 = local.chart_archive_sha
    image_digest         = local.image_digest
    verifier             = filesha256("${path.module}/scripts/verify-jobset-release.sh")
  }

  provisioner "local-exec" {
    command = "\"${path.module}/scripts/verify-jobset-release.sh\""
    quiet   = true

    environment = {
      FS2_JOBSET_CHART_REF            = local.chart_ref
      FS2_JOBSET_CHART_VERSION        = local.chart_version
      FS2_JOBSET_CHART_DIGEST         = local.chart_digest
      FS2_JOBSET_CHART_ARCHIVE_SHA256 = local.chart_archive_sha
      FS2_JOBSET_IMAGE                = "${local.image_repository}:${local.image_tag}"
      FS2_JOBSET_IMAGE_DIGEST         = local.image_digest
      FS2_JOBSET_RENDERED_IMAGE       = "${local.image_repository}:${local.image_tag}@${local.image_digest}"
      FS2_JOBSET_VERIFY_RUN_ROOT      = abspath(var.run_root)
      FS2_JOBSET_CHART_ARCHIVE        = local.chart_archive
    }
  }

  depends_on = [terraform_data.contract]
}

resource "helm_release" "jobset" {
  count = var.enabled ? 1 : 0

  name             = local.release_name
  namespace        = var.namespace
  chart            = local.chart_archive
  create_namespace = false
  # CRDs are upgraded explicitly by terraform_data.crd_upgrade. Helm cannot
  # upgrade resources in chart crds/ and must never silently retain an older
  # schema while the controller advances.
  skip_crds       = true
  atomic          = true
  cleanup_on_fail = true
  wait            = true
  timeout         = 900

  values = [yamlencode({
    commonLabels = var.labels
    image = {
      repository = local.image_repository
      tag        = "${local.image_tag}@${local.image_digest}"
      pullPolicy = "IfNotPresent"
    }
    controller = {
      replicas = 1
      nodeSelector = {
        "workload.fs2.nebius/system" = "true"
      }
      podSecurityContext = {
        runAsNonRoot = true
        seccompProfile = {
          type = "RuntimeDefault"
        }
      }
      securityContext = {
        allowPrivilegeEscalation = false
        readOnlyRootFilesystem   = true
        capabilities = {
          drop = ["ALL"]
        }
      }
    }
  })]

  depends_on = [terraform_data.crd_upgrade]
}

resource "terraform_data" "api_ready" {
  count = var.enabled ? 1 : 0

  input = merge(terraform_data.contract.output, {
    cluster_id = var.cluster_id
  })

  triggers_replace = {
    cluster_id       = var.cluster_id
    kubernetes_minor = local.kubernetes_minor
    chart_digest     = local.chart_digest
    image_digest     = local.image_digest
    probe_sha256     = filesha256("${path.module}/scripts/wait-for-jobset-api.sh")
  }

  provisioner "local-exec" {
    command = "\"${path.module}/scripts/wait-for-jobset-api.sh\""
    quiet   = true

    environment = {
      FS2_JOBSET_KUBECONFIG       = abspath(var.kubeconfig_path)
      FS2_JOBSET_CONTEXT          = var.kube_context
      FS2_JOBSET_RUN_ROOT         = abspath(var.run_root)
      FS2_JOBSET_CLUSTER_ID       = var.cluster_id
      FS2_JOBSET_NAMESPACE        = var.namespace
      FS2_JOBSET_CONTROLLER       = local.controller_name
      FS2_JOBSET_KUBERNETES_MINOR = local.kubernetes_minor
      FS2_JOBSET_TIMEOUT_SECONDS  = "180"
    }
  }

  depends_on = [helm_release.jobset]
}

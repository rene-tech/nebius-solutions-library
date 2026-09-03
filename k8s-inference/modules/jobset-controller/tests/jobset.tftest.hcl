mock_provider "helm" {}

# The chart archive is materialized during plan by an external program. This
# unit test mocks that program's result so it exercises the wiring without a
# network pull; the materializer itself is proved separately.
mock_provider "external" {}

override_data {
  target = data.external.chart[0]
  values = {
    result = {
      path           = "/tmp/fs2-jobset-test/charts/jobset-bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a.tgz"
      archive_sha256 = "bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a"
      chart_digest   = "sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
    }
  }
}

variables {
  enabled            = true
  run_id             = "jobset01"
  namespace          = "jobset-system"
  kubeconfig_path    = "/tmp/fs2-jobset-test/kubeconfig"
  kube_context       = "fs2-jobset-test"
  run_root           = "/tmp/fs2-jobset-test"
  cluster_id         = "mk8scluster-jobsettest"
  kubernetes_version = "1.35.6"
  labels             = { "app.kubernetes.io/part-of" = "fs2-serve" }
}

run "reject_unqualified_kubernetes_minor" {
  command = plan

  variables {
    kubernetes_version = "1.36.0"
  }

  expect_failures = [var.kubernetes_version]
}

run "pinned_jobset_plan_and_state_allowlist" {
  command = plan

  assert {
    condition = (
      # Helm installs the exact verified archive, addressed by its own SHA-256,
      # not an independent pull of the same reference.
      helm_release.jobset[0].chart == data.external.chart[0].result.path &&
      endswith(
        helm_release.jobset[0].chart,
        "/charts/jobset-bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a.tgz",
      ) &&
      helm_release.jobset[0].skip_crds == true &&
      yamldecode(helm_release.jobset[0].values[0]).controller.nodeSelector["workload.fs2.nebius/system"] == "true" &&
      yamldecode(helm_release.jobset[0].values[0]).image.tag == "v0.12.0@sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d" &&
      output.contract.api_version == "jobset.x-k8s.io/v1alpha2" &&
      output.contract.configured_kubernetes_version == "1.35.6" &&
      output.contract.kubernetes_minor == "v1.35" &&
      output.contract.upstream_tested_kubernetes_minors == ["v1.32", "v1.33", "v1.34"] &&
      output.contract.fs2_qualified_kubernetes_minors == ["v1.35"] &&
      output.contract.supported_kubernetes_minors == ["v1.32", "v1.33", "v1.34", "v1.35"] &&
      output.contract.sources.qualification == "modules/jobset-controller/QUALIFICATION.md" &&
      output.contract.sources.live_evidence == "modules/jobset-controller/evidence/kubernetes-1.35-h100.json" &&
      output.contract.chart.digest == "sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24" &&
      output.contract.chart.archive_sha256 == "bd3503757561d93aa14f35fccab76ca417d17e14984aed9f69c9ab068d40980a" &&
      output.contract.image.digest == "sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
    )
    error_message = "JobSet chart, CRDs, API, server compatibility, or image identity drifted."
  }

  assert {
    condition = join(",", output.managed_resource_addresses) == join(",", [
      "helm_release.jobset[0]",
      "terraform_data.api_ready[0]",
      "terraform_data.artifacts_verified[0]",
      "terraform_data.crd_upgrade[0]",
      "terraform_data.contract",
    ])
    error_message = "JobSet module state address allowlist drifted."
  }
}

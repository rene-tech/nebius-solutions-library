from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "modules/jobset-controller"
EVIDENCE = MODULE / "evidence/kubernetes-1.35-h100.json"

JOBSET_CHART_DIGEST = (
    "sha256:02808a890a0b0e03a1d3bf5959e2f562b3b47c15e446bbba358c1d24e1f81b24"
)
JOBSET_IMAGE_DIGEST = (
    "sha256:e75536f1135b7bb2f19f8c3b620782fbdd9091d73398e3a272f9a5fed322980d"
)
KUEUE_IMAGE_DIGEST = (
    "sha256:cecba825d0b0feab9bed2835efe2eb8d825512f1616c8762ab80c53f2ea6afe6"
)
KIND_IMAGE_DIGEST = (
    "sha256:4613778f3cfcd10e615029370f5786704559103cf27bef934597ba562b269661"
)


def test_135_contract_keeps_upstream_and_fs2_evidence_separate() -> None:
    module = (MODULE / "main.tf").read_text(encoding="utf-8")
    root = (ROOT / "variables.tf").read_text(encoding="utf-8")
    foundation = (ROOT / "stages/foundation/variables.tf").read_text(
        encoding="utf-8"
    )
    workloads = (ROOT / "stages/workloads/cluster_contract.tf").read_text(
        encoding="utf-8"
    )

    assert (
        'upstream_tested_kubernetes_minors = ["v1.32", "v1.33", "v1.34"]'
        in module
    )
    assert 'fs2_qualified_kubernetes_minors   = ["v1.35"]' in module
    assert (
        'supported_kubernetes_minors       = ["v1.32", "v1.33", "v1.34", "v1.35"]'
        in module
    )
    assert JOBSET_CHART_DIGEST in module
    assert JOBSET_IMAGE_DIGEST in module
    for source in (root, foundation, workloads):
        assert "[33, 34, 35]" in source
    assert "fs2_qualified_kubernetes_minors" in workloads


def test_local_and_live_canaries_are_bounded_and_syntax_valid() -> None:
    kind_script = MODULE / "scripts/kind-jobset-kueue-integration.sh"
    live_script = MODULE / "scripts/live-kubernetes-1.35-canary.sh"
    for script in (kind_script, live_script):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    kind_source = kind_script.read_text(encoding="utf-8")
    assert f"kindest/node:v1.35.0@{KIND_IMAGE_DIGEST}" in kind_source
    assert "--for=condition=Completed" in kind_source
    assert "child_jobs=2" in kind_source

    live_source = live_script.read_text(encoding="utf-8")
    assert '--kubeconfig "$FS2_JOBSET_KUBECONFIG"' in live_source
    assert '--context "$FS2_JOBSET_CONTEXT"' in live_source
    assert "kubectl config" not in live_source
    assert "kind: ClusterQueue" not in live_source
    assert "kind: ResourceFlavor" not in live_source
    assert "create namespace" not in live_source
    assert "nvidia.com/gpu:" not in live_source
    assert "--cascade=foreground" in live_source

    verifier = (MODULE / "scripts/verify-jobset-release.sh").read_text(
        encoding="utf-8"
    )
    crd_installer = (MODULE / "scripts/apply-jobset-crd.sh").read_text(
        encoding="utf-8"
    )
    for source in (verifier, crd_installer):
        assert 'tar -xOzf "${' in source
        assert "helm pull" not in source


def test_live_135_evidence_is_complete_and_cleanup_is_proved() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert (
        evidence["schema"]
        == "fs2-serve.nebius.ai/jobset-kubernetes-1.35-live-qualification/v1"
    )
    assert evidence["outcome"] == "PASS"
    assert evidence["target"]["project_id"] == "project-e00rene"
    assert evidence["target"]["region"] == "eu-north1"
    assert evidence["target"]["context"] == "k8s-inference-h100"
    assert evidence["target"]["kubernetes_server"] == "v1.35.6"
    assert evidence["target"]["b300_touched"] is False
    assert evidence["releases"]["jobset"]["version"] == "0.12.0"
    assert evidence["releases"]["jobset"]["chart_digest"] == JOBSET_CHART_DIGEST
    assert evidence["releases"]["jobset"]["image"].endswith(JOBSET_IMAGE_DIGEST)
    assert evidence["releases"]["kueue"]["version"] == "0.17.8"
    assert evidence["releases"]["kueue"]["image_digest"] == KUEUE_IMAGE_DIGEST
    assert evidence["releases"]["crd"]["established"] is True

    completion = evidence["canaries"]["completion"]
    assert completion["workload"]["pod_set_count"] == 2
    assert len(completion["child_jobs"]) == 2
    assert all(job["succeeded"] == 1 for job in completion["child_jobs"])
    assert len(completion["pods"]) == 2
    assert all(pod["phase"] == "Succeeded" for pod in completion["pods"])
    assert all(pod["gpu_request"] is None for pod in completion["pods"])

    cleanup = evidence["canaries"]["cleanup"]
    assert cleanup["workload"]["pod_set_count"] == 2
    assert len(cleanup["child_jobs_before_delete"]) == 2
    assert len(cleanup["pods_before_delete"]) == 2
    assert all(pod["phase"] == "Running" for pod in cleanup["pods_before_delete"])
    assert all(pod["gpu_request"] is None for pod in cleanup["pods_before_delete"])

    for canary in (completion, cleanup):
        for key in (
            "residual_jobsets",
            "residual_jobs",
            "residual_pods",
            "residual_workloads",
        ):
            assert canary[key] == 0

    preservation = evidence["preservation"]
    assert (
        preservation["cluster_queue_specs_sha256_before"]
        == preservation["cluster_queue_specs_sha256_after"]
    )
    assert (
        preservation["resource_flavor_specs_sha256_before"]
        == preservation["resource_flavor_specs_sha256_after"]
    )
    assert preservation["quota_or_flavor_mutation"] is False
    assert preservation["nodegroup_mutation"] is False
    assert preservation["gpu_requested"] is False
    assert evidence["terraform"]["previous"]["managed_instances"] == 30
    assert evidence["terraform"]["post"]["managed_instances"] == 36
    assert evidence["terraform"]["post"]["jobset_targeted_plan"] == "no changes"

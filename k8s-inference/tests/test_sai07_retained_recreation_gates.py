from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_bioir_launcher_renders_a_baseline_csi_successor() -> None:
    launcher = ROOT / "acceptance/bioir-20260915/coverage/batch_control.py"
    result = subprocess.run(
        [
            sys.executable,
            launcher,
            "rfdiffusion",
            "--node",
            "gpu-node",
            "--reference-pvc",
            "fs2-reference-data-rwx",
            "--render-only",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    pod = json.loads(result.stdout)["pod"]
    assert all("hostPath" not in volume for volume in pod["spec"]["volumes"])
    reference = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "reference-data")
    assert reference["persistentVolumeClaim"] == {
        "claimName": "fs2-reference-data-rwx",
        "readOnly": True,
    }
    runtime = pod["spec"]["containers"][0]
    assert runtime["securityContext"]["allowPrivilegeEscalation"] is False
    assert runtime["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_reference_data_csi_probe_reads_the_exact_terminal_tree(tmp_path: Path) -> None:
    bundle = "alphafold3-public-databases-v3.0"
    revision = "v3.0-paper-snapshot-2022-09-28"
    tree = "c" * 64
    manifest = "b" * 64
    receipt_relative = Path("receipts") / bundle / f"{revision}.json"
    dataset_relative = Path("datasets") / bundle / revision / "sha256" / tree
    (tmp_path / receipt_relative).parent.mkdir(parents=True)
    (tmp_path / dataset_relative).mkdir(parents=True)
    (tmp_path / dataset_relative / ".fs2-manifest-sha256").write_text(manifest + "\n")
    (tmp_path / receipt_relative).write_text(json.dumps({
        "schema": "fs2-serve.nebius.ai/reference-data-terminal-receipt/v1",
        "bundle_id": bundle,
        "revision": revision,
        "created_at": "2026-09-16T00:00:00Z",
        "storage": {
            "host_root": "/mnt/fs2-reference-data/data",
            "mount_path": "/reference-data",
            "dataset_sub_path": dataset_relative.as_posix(),
            "read_only": True,
        },
        "content": {
            "tree_sha256": tree,
            "manifest_sha256": manifest,
            "inventory_sha256": "d" * 64,
            "inventory_marker": ".fs2-manifest-sha256",
            "file_count": 1,
            "expanded_bytes": 64,
            "inline_inventory": True,
        },
        "placement": {},
    }))
    command = [
        sys.executable,
        ROOT / "reference-data/verify_csi_readiness.py",
        "--root", tmp_path,
        "--receipt", receipt_relative,
        "--bundle", bundle,
        "--revision", revision,
        "--tree-sha256", tree,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["read_probe_passed"] is True

    (tmp_path / dataset_relative / ".fs2-manifest-sha256").write_text("e" * 64)
    refused = subprocess.run(command, check=False, capture_output=True, text=True)
    assert refused.returncode != 0
    assert "marker differs" in refused.stderr


def test_reference_data_csi_probe_is_an_apply_time_gate() -> None:
    source = (ROOT / "reference-data/terraform/main.tf").read_text(encoding="utf-8")
    variables = (ROOT / "reference-data/terraform/variables.tf").read_text(encoding="utf-8")
    assert 'resource "kubernetes_job_v1" "csi_read_probe"' in source
    assert "wait_for_completion = true" in source
    assert 'read_only  = true' in source
    assert 'claim_name = kubernetes_persistent_volume_claim_v1.reference_data.metadata[0].name' in source
    assert "verify_csi_readiness.py" in source
    assert 'variable "expected_tree_sha256"' in variables


def test_no_live_scientific_launcher_or_manifest_retains_hostpath() -> None:
    roots = (
        ROOT / "acceptance/bioir-20260915",
        ROOT / "acceptance/scientific-startup",
    )
    executable_suffixes = {".py", ".sh", ".yaml", ".yml", ".tf"}
    retained: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in executable_suffixes:
                source = path.read_text(encoding="utf-8")
                if "hostPath" in source or "host_path" in source:
                    retained.append(str(path.relative_to(ROOT)))
    assert retained == []


def test_protenix_manifest_self_enforces_pinned_baseline_and_uses_csi() -> None:
    path = ROOT / "acceptance/bioir-20260915/protenix/baseline.yaml"
    namespace, pod = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/enforce-version"] == "v1.35"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert labels["pod-security.kubernetes.io/audit-version"] == "v1.35"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn-version"] == "v1.35"
    assert pod["metadata"]["labels"]["fs2-serve.nebius.ai/network-profile"] == "mounted-content"
    assert all("hostPath" not in volume for volume in pod["spec"]["volumes"])
    model = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "model")
    assert model["persistentVolumeClaim"] == {
        "claimName": "fs2-reference-data-rwx",
        "readOnly": True,
    }


def test_privileged_snapshot_renderer_uses_the_exact_admission_profile() -> None:
    renderer = ROOT / "acceptance/scientific-startup/render_persistent_probe.py"
    common = [
        "--name", "fs2-snapshot-test",
        "--namespace", "fs2-snapshot-operations",
        "--node", "gpu-node",
        "--runtime-image", "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cancer-immunotherapy/esmfold2@sha256:b372dd7e34e464680a82456ca31b403b0ac0d0851511930d471b67041adbbde3",
        "--tools-image", "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4",
        "--source-in-image",
        "--checkpoint-pvc", "fs2-snapshot-checkpoints",
        "--checkpoint-subdir", "test",
    ]
    for mode in ("donor", "restore"):
        result = subprocess.run(
            [sys.executable, renderer, mode, *common], check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        pod = json.loads(result.stdout)
        assert pod["metadata"]["labels"]["security.fs2.nebius.ai/snapshot-profile"] == "esmfold2-h100-v1"
        assert pod["spec"]["serviceAccountName"] == "fs2-snapshot-runtime"
        assert all("hostPath" not in volume for volume in pod["spec"]["volumes"])
        assert pod["spec"]["securityContext"] == {
            "fsGroup": 65532,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        assert pod["spec"]["containers"][0]["securityContext"] == {
            "privileged": True,
            "runAsUser": 0,
            "runAsGroup": 0,
        }


def test_snapshot_renderer_rejects_profile_image_substitution() -> None:
    renderer = ROOT / "acceptance/scientific-startup/render_persistent_probe.py"
    result = subprocess.run(
        [
            sys.executable,
            renderer,
            "donor",
            "--name", "fs2-snapshot-test",
            "--namespace", "fs2-snapshot-operations",
            "--node", "gpu-node",
            "--runtime-image", "example.invalid/runtime@sha256:" + "a" * 64,
            "--tools-image", "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-snapshot/scientific-tools@sha256:17cc3536dd847355b8457b2e92bd7d0fdf292bdd8e6acc457e25f14e28284ba4",
            "--source-in-image",
            "--checkpoint-pvc", "fs2-snapshot-checkpoints",
            "--checkpoint-subdir", "test",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "images differ from the exact admitted profile" in result.stderr


def test_nonprivileged_storage_holder_remains_renderable() -> None:
    renderer = ROOT / "acceptance/scientific-startup/render_persistent_probe.py"
    result = subprocess.run(
        [
            sys.executable,
            renderer,
            "storage-holder",
            "--name", "test",
            "--node", "node",
            "--runtime-image", "example.invalid/runtime@sha256:" + "a" * 64,
            "--tools-image", "example.invalid/tools@sha256:" + "b" * 64,
            "--source-in-image",
            "--checkpoint-pvc", "checkpoint",
            "--checkpoint-subdir", "test",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '"persistentVolumeClaim"' in result.stdout
    assert '"privileged"' not in result.stdout
    assert '"hostPath"' not in result.stdout

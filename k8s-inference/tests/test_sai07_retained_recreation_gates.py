from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_historical_bioir_hostpath_launcher_is_fail_closed() -> None:
    launcher = ROOT / "acceptance/bioir-20260915/coverage/batch_control.py"
    result = subprocess.run(
        [sys.executable, launcher, "rfdiffusion", "--node", "unused"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "historical hostPath launcher is non-recreatable" in result.stderr


def test_historical_protenix_manifest_self_enforces_pinned_baseline() -> None:
    path = ROOT / "acceptance/bioir-20260915/protenix/baseline.yaml"
    namespace, pod = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    labels = namespace["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "baseline"
    assert labels["pod-security.kubernetes.io/enforce-version"] == "v1.35"
    assert labels["pod-security.kubernetes.io/audit"] == "restricted"
    assert labels["pod-security.kubernetes.io/audit-version"] == "v1.35"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn-version"] == "v1.35"
    assert pod["metadata"]["labels"]["security.fs2.nebius.ai/historical-non-recreatable"] == "true"
    assert any("hostPath" in volume for volume in pod["spec"]["volumes"])


def test_privileged_snapshot_renderer_refuses_donor_and_restore() -> None:
    renderer = ROOT / "acceptance/scientific-startup/render_persistent_probe.py"
    common = [
        "--name", "test", "--node", "node", "--runtime-image", "example.invalid/runtime@sha256:" + "a" * 64,
        "--tools-image", "example.invalid/tools@sha256:" + "b" * 64,
        "--source-in-image", "--checkpoint-pvc", "checkpoint", "--checkpoint-subdir", "test",
    ]
    for mode in ("donor", "restore"):
        result = subprocess.run(
            [sys.executable, renderer, mode, *common], check=False, capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "retired privileged snapshot Pod recreation" in result.stderr


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

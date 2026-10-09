"""Terraform selects captured helper content without mutating historical files."""
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_quiet_capture_helper_has_only_the_measured_dump_verbosity_change():
    sources = ROOT / "models/scientific-snapshot"
    original = (sources / "process_checkpoint.py").read_bytes()
    quiet = (sources / "process_checkpoint_quiet.py").read_bytes()
    assert hashlib.sha256(original).hexdigest() == "8a65528b0abfdfd4308f438b572b78b5183fde41a5f0a3d5980aaa9329a761ea"
    assert hashlib.sha256(quiet).hexdigest() == "4378066b4aa7e73c4bb605e320f4ce8aecfd9eafe306b30e07b801bbc4194124"
    assert quiet == original.replace(
        b'"-v4", "--file-locks", "--tcp-established", "--link-remap",',
        b'"-v2", "--file-locks", "--tcp-established", "--link-remap",',
        1,
    )


@pytest.mark.skipif(shutil.which("terraform") is None, reason="Terraform CLI unavailable")
def test_real_terraform_selects_old_and_quiet_source_maps_by_exact_digest(tmp_path):
    original = json.loads((ROOT / "acceptance/h100-fleet/snapshots/cosmos3-nano-bundle.json").read_text())
    quiet = {**original, "bundle_id": "quiet-test", "source_configmap": "quiet-test-sources",
             "source_sha256": {**original["source_sha256"], "process_checkpoint.py":
                               "4378066b4aa7e73c4bb605e320f4ce8aecfd9eafe306b30e07b801bbc4194124"}}
    config = {"enabled": True, "gpu_snapshots": {
        "bundles": {row["bundle_id"]: row for row in (original, quiet)},
        "cache": {}, "adopt_existing": False,
    }}
    declarations = 'variable "model_controller" { type = any }\n'
    declarations += "locals { fs2_root = " + json.dumps(str(ROOT)) + " }\n"
    locals_only = (ROOT / "stages/workloads/serving_snapshots.tf").read_text().split('\nresource "', 1)[0]
    (tmp_path / "main.tf").write_text(declarations + locals_only)
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({"model_controller": config}))
    result = subprocess.run(  # noqa: S603 - fixed local Terraform command and generated test directory
        [shutil.which("terraform"), f"-chdir={tmp_path}", "console"],
        input="jsonencode(local.serving_snapshot_configmaps)\n", text=True,
        capture_output=True, check=True, timeout=30,
    )
    maps = json.loads(json.loads(result.stdout))
    for bundle in (original, quiet):
        contents = maps[bundle["source_configmap"]]
        actual = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in contents.items()}
        assert actual == bundle["source_sha256"]
    original_helper = maps[original["source_configmap"]]["process_checkpoint.py"]
    quiet_helper = maps[quiet["source_configmap"]]["process_checkpoint.py"]
    assert original_helper != quiet_helper

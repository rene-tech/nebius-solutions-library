"""Real Terraform locals preserve existing and shared scientific source CMs."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("terraform") is None, reason="Terraform CLI unavailable")
def test_protenix_bytes_and_shared_esm_cli_union(tmp_path):
    protenix = json.loads((ROOT / "acceptance/h100-fleet/snapshots/protenix-v2-bundle.json").read_text())
    source = ROOT / "models/scientific-snapshot"
    wrapper = ROOT / "models/cancer-immunotherapy/images/structure-secondary/run_esmfold2.py"
    expected = {name: (source / name).read_text() for name in (
        "supervisor.py", "process_checkpoint.py", "esmfold2_server.py",
    )}
    expected["run_esmfold2.py"] = wrapper.read_text()
    esm = {
        **protenix, "bundle_id": "esm-test", "model_id": "esmfold2", "stage_id": "fold",
        "source_configmap": "esm-shared", "cli_configmap": "esm-shared", "cli_key": "run_esmfold2.py",
        "source_sha256": {name: hashlib.sha256(data.encode()).hexdigest() for name, data in expected.items()},
        "cli_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "qualified": False, "qualification_receipt_sha256": None,
    }
    fast = {**esm, "bundle_id": "esm-fast-test", "model_id": "esmfold2-fast"}
    rf_sources = {
        "rfdiffusion_runtime_entrypoint.py": (
            ROOT / "models/cancer-immunotherapy/runtime-images/rfdiffusion/runtime_entrypoint.py"
        ).read_text(),
        "sitecustomize.py": (source / "python310_sitecustomize.py").read_text(),
    }
    rf = {
        **protenix, "bundle_id": "rf-test", "source_configmap": "rf-sources", "cli_configmap": "rf-request",
        "cli_key": "rfdiffusion_observed_cli.py", "source_sha256": {
            name: hashlib.sha256(data.encode()).hexdigest() for name, data in rf_sources.items()
        },
        "entrypoint": {"configmap": "rf-request", "key": "scientific_request_entrypoint.py",
                       "sha256": hashlib.sha256((source / "scientific_request_entrypoint.py").read_bytes()).hexdigest()},
    }
    config = {"enabled": True, "gpu_snapshots": {
        "bundles": {item["bundle_id"]: item for item in (protenix, esm, fast, rf)}, "adopt_existing": False,
    }}
    declarations = 'variable "scientific_batch" { type = any }\n'
    declarations += "locals { fs2_root = " + json.dumps(str(ROOT)) + " }\n"
    locals_only = (ROOT / "stages/workloads/scientific_snapshots.tf").read_text().split('\nresource "', 1)[0]
    (tmp_path / "main.tf").write_text(declarations + locals_only)
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({"scientific_batch": config}))
    result = subprocess.run(
        ["terraform", f"-chdir={tmp_path}", "console"],
        input="jsonencode(local.scientific_snapshot_configmaps)\n", text=True,
        capture_output=True, check=True, timeout=30,
    )
    maps = json.loads(json.loads(result.stdout))
    assert maps["esm-shared"] == expected
    assert len(maps["esm-shared"]) == 4  # CLI must not replace the server's source map.
    assert maps[protenix["source_configmap"]] == {
        name: (source / name).read_text() for name in protenix["source_sha256"]
    }
    assert maps[protenix["cli_configmap"]] == {
        protenix["cli_key"]: (source / "protenix_cli_proxy.py").read_text()
    }
    assert maps["rf-sources"] == rf_sources
    assert maps["rf-request"] == {
        "scientific_request_entrypoint.py": (source / "scientific_request_entrypoint.py").read_text(),
        "rfdiffusion_observed_cli.py": (source / "rfdiffusion_observed_cli.py").read_text(),
    }

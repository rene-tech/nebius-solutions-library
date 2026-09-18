"""Exercise the real facade expressions without providers, state, or cloud calls."""

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (None, True),
        ("image", False),
        ("remove", False),
        ("baseline_order", False),
        ("identity", False),
    ],
)
def test_additive_execution_contract(tmp_path, mutation, expected):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform is required")
    old_rows = [
        {
            "model_id": name,
            "image": "image@sha256:" + name * 64,
            "execution_identity_sha256": name * 64,
        }
        for name in ("a", "b")
    ]
    base = {"schema": "example/v3", "models": old_rows}
    old_digest = digest(base)
    current = copy.deepcopy(base)
    current["models"].append({"model_id": "new", "execution_identity_sha256": "c" * 64})
    new_digest = digest(current)
    current["qualification_baselines"] = {old_digest: ["a", "b"]}
    current["snapshot_bundles"] = {"optional": {"separately_bound": True}}
    profiles = {
        row["model_id"]: {
            "execution_identity": {
                "execution_identity_sha256": row["execution_identity_sha256"]
            },
            "qualification": {
                "execution_map_sha256": new_digest
                if row["model_id"] == "new"
                else old_digest
            },
        }
        for row in current["models"]
    }
    if mutation == "image":
        current["models"][0]["image"] = "changed"
    elif mutation == "remove":
        current["models"].pop(0)
    elif mutation == "baseline_order":
        current["qualification_baselines"][old_digest].reverse()
    elif mutation == "identity":
        profiles["new"]["execution_identity"]["execution_identity_sha256"] = "d" * 64
    (tmp_path / "contract.tf").write_text(
        (ROOT / "scientific-execution.tf").read_text()
    )
    (tmp_path / "main.tf").write_text(
        "locals {\n scientific_execution_map = jsondecode("
        + json.dumps(json.dumps(current))
        + ")\n"
        + "scientific_workload_profiles_by_model_id = jsondecode("
        + json.dumps(json.dumps(profiles))
        + ")\n}\n"
        + 'output "valid" { value = local.scientific_execution_identities_valid }\n'
    )
    subprocess.run(
        [terraform, "init", "-backend=false", "-input=false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [terraform, "validate"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        [terraform, "plan", "-input=false", "-refresh=false", "-out=plan"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    planned = subprocess.run(
        [terraform, "show", "-json", "plan"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert (
        json.loads(planned.stdout)["planned_values"]["outputs"]["valid"]["value"]
        is expected
    )

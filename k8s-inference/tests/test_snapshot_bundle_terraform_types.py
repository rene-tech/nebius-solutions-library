"""Real Terraform conversion must retain heterogeneous qualified bundle JSON."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("variables", ["variables.tf", "stages/workloads/variables.tf"])
@pytest.mark.parametrize("declaration_index", [0, 1])
def test_mixed_bundle_shapes_survive_terraform_conversion(tmp_path, variables, declaration_index):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform is required for the real type-conversion regression")
    declarations = re.findall(
        r"^\s*(bundles\s*=\s*optional\([^\n]+)$",
        (ROOT / variables).read_text(),
        re.MULTILINE,
    )
    assert len(declarations) == 2
    # Exercise the actual declared attribute type in an isolated provider-free
    # module. No cluster, cloud resource, remote backend or existing state.
    (tmp_path / "main.tf").write_text(
        'variable "snapshots" {\n type = object({\n'
        + declarations[declaration_index]
        + '\n })\n}\noutput "bundles" { value = var.snapshots.bundles }\n'
    )
    bundles = {}
    for model in ("qwen3-8b", "cosmos3-nano", "genmol", "protenix-v2", "esmfold2", "esmfold2-fast"):
        value = json.loads((ROOT / f"acceptance/h100-fleet/snapshots/{model}-bundle.json").read_bytes())
        bundles[value["bundle_id"]] = value
    inputs = tmp_path / "inputs.tfvars.json"
    inputs.write_text(json.dumps({"snapshots": {"bundles": bundles}}))
    subprocess.run(  # noqa: S603 - fixed Terraform command, isolated generated module
        [terraform, "init", "-backend=false", "-input=false"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [terraform, "plan", "-input=false", "-refresh=false", f"-var-file={inputs}", "-out=plan"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    result = subprocess.run(  # noqa: S603
        [terraform, "show", "-json", "plan"], cwd=tmp_path, check=True, capture_output=True,
    )
    actual = json.loads(result.stdout)["planned_values"]["outputs"]["bundles"]["value"]
    assert actual == bundles  # No dropped fields, coerced tuples, or model aliasing.

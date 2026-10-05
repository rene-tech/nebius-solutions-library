import copy
import runpy
from pathlib import Path

import pytest

MODULE = runpy.run_path(str(Path(__file__).with_name("activate_compatible_readers.py")))


def deployment():
    return {"metadata": {"uid": "fixed"}, "spec": {
        "replicas": 3, "strategy": {"type": "RollingUpdate"},
        "template": {"metadata": {"annotations": {"binding": "preserved"}}, "spec": {
            "containers": [{"name": "control-plane", "image": MODULE["PREVIOUS"],
                            "env": [{"name": "TOOLS_IMAGE", "value": "unchanged"}]}],
            "volumes": [{"name": "fresh-agent-binding", "configMap": {"name": "current"}}],
        }},
    }}


def test_only_api_image_changes_and_whole_template_is_tested():
    before = deployment()
    saved = copy.deepcopy(before)
    patch = MODULE["prepare"](before)
    expected = copy.deepcopy(before["spec"]["template"])
    expected["spec"]["containers"][0]["image"] = MODULE["COMPATIBLE"]
    assert patch[1] == {"op": "test", "path": "/spec/template", "value": before["spec"]["template"]}
    assert patch[-1]["value"] == expected
    assert before == saved


@pytest.mark.parametrize("change", ["image", "replicas", "strategy"])
def test_changed_starting_contract_refused(change):
    before = deployment()
    if change == "image":
        before["spec"]["template"]["spec"]["containers"][0]["image"] = "unexpected"
    elif change == "replicas":
        before["spec"]["replicas"] = 1
    else:
        before["spec"]["strategy"]["type"] = "Recreate"
    with pytest.raises(ValueError):
        MODULE["prepare"](before)

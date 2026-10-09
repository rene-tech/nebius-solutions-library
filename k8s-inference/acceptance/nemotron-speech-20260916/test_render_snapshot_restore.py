import copy
import json
from pathlib import Path

import pytest

from render_snapshot_restore import render_restore


@pytest.mark.parametrize("model", ["en", "multi"])
def test_cross_node_restore_preserves_capture_and_uses_private_scratch(model):
    original = json.loads((Path(__file__).parent / (model + "-donor-r2-live.json")).read_text())
    retained = copy.deepcopy(original)
    pod = render_restore(original, name="fs2-speech-cross-test", node="other-existing-h100")
    assert original == retained
    assert "nodeName" not in pod["spec"]
    assert pod["spec"]["nodeSelector"]["kubernetes.io/hostname"] == "other-existing-h100"
    assert pod["spec"]["activeDeadlineSeconds"] == 900
    runtime = pod["spec"]["containers"][0]
    assert "restore" in runtime["command"]
    assert "--allow-device-remap" in runtime["command"]
    assert runtime["command"][runtime["command"].index("--fallback")+1] == "fail"
    assert runtime["image"] == original["spec"]["containers"][0]["image"]
    bundle = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "snapshot-bundle")
    assert bundle["persistentVolumeClaim"]["readOnly"]
    scratch = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "snapshot-checkpoints")
    assert scratch["emptyDir"] == {}

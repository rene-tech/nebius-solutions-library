import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "reliability_release", Path(__file__).with_name("release.py")
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_image_and_tools_change_without_resetting_other_live_configuration():
    before = {
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": release.BEFORE_API,
                    "env": [
                        {
                            "name": "FS2_WORKBENCH_RELEASES",
                            "value": "preserve-current-customers",
                        },
                        {
                            "name": "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE",
                            "value": release.BEFORE_TOOLS,
                        },
                    ],
                }
            ],
            "volumes": [{"configMap": {"name": "keep-scvi-and-serving-map"}}],
        }
    }
    target = release.REPO + "@sha256:" + "1" * 64
    after = release.extend(before, target)
    assert before["spec"]["containers"][0]["image"] == release.BEFORE_API
    assert after["spec"]["containers"][0]["image"] == target
    assert after["spec"]["containers"][0]["env"][1]["value"] == target
    assert (
        after["spec"]["containers"][0]["env"][0]
        == before["spec"]["containers"][0]["env"][0]
    )
    assert after["spec"]["volumes"] == before["spec"]["volumes"]
    with pytest.raises(ValueError, match="Concurrent API"):
        release.extend(after, release.REPO + "@sha256:" + "2" * 64)
    assert release.extend(after, target) == after


def test_api_only_metrics_change_keeps_qualified_collector():
    before = {
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": release.BEFORE_API,
                    "env": [
                        {
                            "name": "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE",
                            "value": release.BEFORE_TOOLS,
                        }
                    ],
                }
            ]
        }
    }
    after = release.extend(
        before, release.REPO + "@sha256:" + "3" * 64, tools_image=release.BEFORE_TOOLS
    )
    assert (
        after["spec"]["containers"][0]["env"] == before["spec"]["containers"][0]["env"]
    )

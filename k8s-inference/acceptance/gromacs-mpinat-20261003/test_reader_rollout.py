import copy

import pytest

from reader_rollout import API_IMAGE, REPO, prepare


def deployment():
    return {"spec": {"template": {
        "metadata": {"annotations": {"unrelated": "preserve", "fs2.nebius.ai/image-digest": API_IMAGE.split("@")[1]}},
        "spec": {"containers": [{"name": "control-plane", "image": API_IMAGE, "env": [{"name": "TOOLS", "value": "old"}]}],
                 "volumes": [{"name": "catalog", "configMap": {"name": "old"}}]},
    }}}


def test_changes_only_image_and_annotation():
    original = deployment()
    saved = copy.deepcopy(original)
    result = prepare(original, REPO + "@sha256:" + "a" * 64)
    new = result["patch"][-1]["value"]
    new["spec"]["containers"][0]["image"] = API_IMAGE
    new["metadata"]["annotations"]["fs2.nebius.ai/image-digest"] = API_IMAGE.split("@")[1]
    assert new == saved["spec"]["template"]
    assert original == saved
    assert result["patch"][0] == {"op": "test", "path": "/spec/template", "value": saved["spec"]["template"]}
    assert result["inverse_before_shapes"][-1]["value"] == saved["spec"]["template"]


def test_refuses_changed_baseline():
    current = deployment()
    current["spec"]["template"]["spec"]["containers"][0]["image"] = "other"
    with pytest.raises(ValueError, match="baseline"):
        prepare(current, REPO + "@sha256:" + "a" * 64)


@pytest.mark.parametrize("image", [API_IMAGE, REPO + ":latest", "elsewhere@sha256:" + "a" * 64])
def test_refuses_ambiguous_target(image):
    with pytest.raises(ValueError, match="immutable"):
        prepare(deployment(), image)

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator, ValidationError

from fs2_serve.model_input_contracts import _examples, _sam2, _wan2_i2v, _wan2_t2v


def test_wan_contracts_split_deployment_modes_and_use_artifact_transport():
    t2v = _wan2_t2v()
    i2v = _wan2_i2v()
    assert "input_reference" not in t2v["properties"]
    assert i2v["properties"]["input_reference"]["x-fs2-artifact-materialization"] == "data-url"
    assert i2v["properties"]["input_reference"]["x-fs2-artifact-max-bytes"] == 16 * 1024 * 1024
    Draft202012Validator(t2v).validate(_examples("wan2-2-t2v-nim")[0])
    Draft202012Validator(i2v).validate(_examples("wan2-2-i2v-nim")[0])


def test_sam_contract_supports_prompted_image_automatic_image_and_video():
    schema = _sam2()
    validator = Draft202012Validator(schema)
    validator.validate(_examples("sam2-1-hiera-large")[0])
    validator.validate(
        {
            "mode": "automatic-image",
            "media_base64": "aGVsbG8=",
            "media_type": "image/jpeg",
            "max_masks": 16,
        }
    )
    validator.validate(
        {
            "mode": "prompted-video",
            "media_base64": "aGVsbG8=",
            "media_type": "video/mp4",
            "box": [10, 10, 100, 100],
            "prompt_frame": 0,
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "mode": "prompted-image",
                "media_base64": "aGVsbG8=",
                "media_type": "image/png",
                "points": [],
            }
        )
    assert schema["properties"]["media_base64"]["x-fs2-artifact-materialization"] == "base64"

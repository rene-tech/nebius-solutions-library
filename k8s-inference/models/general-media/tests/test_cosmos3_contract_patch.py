from __future__ import annotations

import ast
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "cosmos_contract_patch", Path(__file__).parents[1] / "cosmos3_contract_patch.py"
)
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


def patched_method(monkeypatch):
    source = ("class Pipeline:\n    def _transfer_bucket_size(self, sp, source_hw):\n" + patch.ANCHOR
              + "\ndef preprocess(request, image):\n    def set_transfer_size(request, image):\n"
              + patch.PREPROCESS_ANCHOR + "    return set_transfer_size(request, image)\n").encode()
    monkeypatch.setattr(patch, "UPSTREAM_SHA256", hashlib.sha256(source).hexdigest())
    result = patch.patch_source(source)
    namespace = {
        "COSMOS3_T2V_DEFAULT_HEIGHT": 480,
        "COSMOS3_T2V_DEFAULT_WIDTH": 640,
        "find_closest_target_size": lambda h, w, resolution: (736, 544),
        "_extra_args": lambda request: request.sampling_params.extra,
    }
    exec(compile(ast.parse(result), "patched_cosmos_test", "exec"), namespace)  # noqa: S102 - fixed fixture
    pipeline = namespace["Pipeline"]()
    pipeline._get_sp_param = lambda sp, key, default: getattr(sp, "extra", {}).get(key, default)
    return pipeline._transfer_bucket_size, namespace["preprocess"]


@pytest.mark.parametrize("height,width", [(480, 640), (256, 448), (720, 1280), (512, 512)])
def test_explicit_transfer_shape_is_not_replaced_by_bucket(monkeypatch, height, width):
    method, preprocess = patched_method(monkeypatch)
    request = SimpleNamespace(height=height, width=width, extra={"use_resolution_template": False, "resolution": 480})
    assert preprocess(SimpleNamespace(sampling_params=request), SimpleNamespace(height=480, width=640)) == (height, width)
    assert (request.height, request.width) == (height, width)
    assert method(request, (480, 640)) == (height, width)


@pytest.mark.parametrize("extra", [{}, {"use_resolution_template": True}])
def test_default_and_explicit_bucket_policy_are_unchanged(monkeypatch, extra):
    method, preprocess = patched_method(monkeypatch)
    request = SimpleNamespace(height=480, width=640, extra=extra)
    assert preprocess(SimpleNamespace(sampling_params=request), SimpleNamespace(height=480, width=640)) == (544, 736)
    assert (request.height, request.width) == (544, 736)
    assert method(request, (480, 640)) == (544, 736)


@pytest.mark.parametrize("height,width", [(0, 640), (480, 0), (481, 640)])
def test_invalid_explicit_shapes_are_actionable(monkeypatch, height, width):
    method, preprocess = patched_method(monkeypatch)
    with pytest.raises(ValueError, match="positive multiples of 16"):
        method(SimpleNamespace(height=height, width=width, extra={"use_resolution_template": False}), (480, 640))
    with pytest.raises(ValueError, match="positive multiples of 16"):
        preprocess(SimpleNamespace(sampling_params=SimpleNamespace(height=height, width=width, extra={"use_resolution_template": False})), SimpleNamespace(height=480, width=640))


def test_unreviewed_upstream_bytes_are_not_patched():
    with pytest.raises(ValueError, match="reviewed pinned image"):
        patch.patch_source(b"another version")

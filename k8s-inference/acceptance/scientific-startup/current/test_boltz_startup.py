"""Validate benchmark hook ordering without loading a scientific model."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


def load_hook():
    spec = importlib.util.spec_from_file_location("boltz_test_hook", Path(__file__).with_name("boltz_sitecustomize.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict("os.environ", {"FS2_STARTUP_BENCHMARK_MODEL": ""}):
        spec.loader.exec_module(module)
    return module


class BoltzStartupHookTests(unittest.TestCase):
    def test_native_hook_then_sync_then_single_marker(self):
        calls = []

        class Loop:
            def _on_predict_start(self):
                calls.append("native_prediction_start")
                return "unchanged_return"

        cuda = types.SimpleNamespace(
            is_available=lambda: True,
            synchronize=lambda: calls.append("cuda_synchronize"),
            get_device_name=lambda: "test GPU",
            get_device_capability=lambda: (9, 0),
        )
        torch = types.SimpleNamespace(cuda=cuda, __version__="test", version=types.SimpleNamespace(cuda="test"))
        fake = types.SimpleNamespace(_PredictionLoop=Loop)
        hook = load_hook()
        with patch.dict(sys.modules, {"torch": torch, "pytorch_lightning.loops.prediction_loop": fake}):
            hook.patch_loaded()
            hook.patch_loaded()
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                self.assertEqual(Loop()._on_predict_start(), "unchanged_return")
                Loop()._on_predict_start()
        self.assertEqual(calls, ["native_prediction_start", "cuda_synchronize", "native_prediction_start"])
        lines = capture.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0].split("FS2_STARTUP ", 1)[1])
        self.assertEqual(record["phase"], "model_ready")
        self.assertIn("not-warmed", record["compilation"])
        self.assertEqual(len(record["native_hook_source_sha256"]), 64)

    def test_no_cuda_cannot_pass(self):
        class Loop:
            def _on_predict_start(self):
                return None

        hook = load_hook()
        with patch.dict(sys.modules, {
            "torch": types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False)),
            "pytorch_lightning.loops.prediction_loop": types.SimpleNamespace(_PredictionLoop=Loop),
        }):
            hook.patch_loaded()
            with self.assertRaisesRegex(RuntimeError, "no CUDA device"):
                Loop()._on_predict_start()
        self.assertFalse(hook.EMITTED)


if __name__ == "__main__":
    unittest.main()

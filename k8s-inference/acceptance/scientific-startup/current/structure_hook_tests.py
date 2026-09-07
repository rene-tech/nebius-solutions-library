"""CPU-only regression checks for benchmark hooks (no model or GPU imports)."""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from structure_isolated_runs import parse_markers
from structure_report import distribution


class ReceiptTests(unittest.TestCase):
    def test_native_progress_bar_prefix_keeps_complete_json_event(self):
        event = {
            "phase": "model_ready",
            "model_id": "openfold3-openbind",
            "utc": "2026-09-07T06:06:54Z",
            "monotonic_seconds": 1.2,
        }
        self.assertEqual(
            parse_markers("Predicting: 100%|###| FS2_STARTUP " + json.dumps(event)),
            [event],
        )

    def test_nested_inventory_stdout_is_not_an_event(self):
        nested = json.dumps({"stdout": 'FS2_STARTUP {"phase": "model_ready"}'})
        self.assertEqual(parse_markers("FS2_ENVIRONMENT " + nested), [])

    def test_distribution_uses_independent_repetition_values(self):
        self.assertEqual(
            distribution([3, 1, 2]), {"n": 3, "median": 2, "min": 1, "max": 3}
        )


class HookTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "structure_hooks_under_test",
            Path(__file__).with_name("structure_sitecustomize.py"),
        )
        self.hooks = importlib.util.module_from_spec(spec)
        with patch.dict("os.environ", {"FS2_STARTUP_BENCHMARK_MODEL": ""}):
            spec.loader.exec_module(self.hooks)
        self.events = []
        self.hooks.emit = lambda phase, **values: self.events.append((phase, values))
        self.torch = SimpleNamespace(
            cuda=SimpleNamespace(
                synchronize=lambda: self.events.append(("synchronize", {})),
                get_device_name=lambda _: "H100",
                get_device_capability=lambda _: (9, 0),
            ),
            version=SimpleNamespace(cuda="test"),
            __version__="test",
        )

    def test_esm_preserves_arguments_return_and_exactly_one_call(self):
        calls = []

        class Builder:
            def fold(self, *args, **kwargs):
                calls.append((args, kwargs))
                return "original-result"

        self.hooks.MODEL = "esmfold2"
        with patch.dict(
            sys.modules,
            {
                "esm.models.esmfold2": SimpleNamespace(ESMFold2InputBuilder=Builder),
                "torch": self.torch,
            },
        ):
            self.hooks.patch_loaded_modules()
            self.hooks.patch_loaded_modules()
            result = Builder().fold("model", steps=200, loops=20)
        self.assertEqual(result, "original-result")
        self.assertEqual(calls, [(("model",), {"steps": 200, "loops": 20})])
        self.assertEqual(
            [phase for phase, _ in self.events],
            ["synchronize", "model_ready", "synchronize", "first_compute_complete"],
        )

    def test_protenix_ready_is_after_original_loader(self):
        events = self.events

        class Runner:
            def __init__(self, value):
                events.append(("original-init", {"value": value}))

            def predict(self, value):
                return value

        self.hooks.MODEL = "protenix-v2"
        with patch.dict(
            sys.modules,
            {
                "runner.inference": SimpleNamespace(InferenceRunner=Runner),
                "torch": self.torch,
            },
        ):
            self.hooks.patch_loaded_modules()
            Runner("unchanged")
        self.assertEqual(
            [phase for phase, _ in events],
            [
                "model_initialization_start",
                "original-init",
                "synchronize",
                "model_ready",
            ],
        )

    def test_af3_never_claims_host_parameters_are_cuda_ready(self):
        self.hooks.MODEL = "alphafold3"
        module = SimpleNamespace(
            get_model_haiku_params=lambda *args, **kwargs: {"original": 1}
        )
        with patch.dict(sys.modules, {"alphafold3.model.params": module}):
            self.hooks.patch_loaded_modules()
            self.assertEqual(
                module.get_model_haiku_params("same-directory"), {"original": 1}
            )
        ready = next(values for phase, values in self.events if phase == "model_ready")
        self.assertEqual(ready["placement"], "host-parameters")
        self.assertIn("device placement remain", ready["compilation"])

    def test_missing_target_does_not_manufacture_ready(self):
        self.hooks.MODEL = "unrecognized"
        self.hooks.patch_loaded_modules()
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()

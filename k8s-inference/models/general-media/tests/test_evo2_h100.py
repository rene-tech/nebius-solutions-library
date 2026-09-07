import importlib.util
from pathlib import Path
import sys
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch


class H100TopologyTests(unittest.TestCase):
    def setUp(self):
        runtime = types.ModuleType("evo2_deep.runtime")
        runtime.Evo2Backend = object
        runtime.RuntimeFailure = ValueError
        runtime._driver_version = lambda: "stub"
        runtime._tool_identity = lambda _: {}
        spec = importlib.util.spec_from_file_location("evo2_h100", Path(__file__).parents[1] / "evo2_h100.py")
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"evo2_deep": types.ModuleType("evo2_deep"), "evo2_deep.runtime": runtime}):
            spec.loader.exec_module(self.module)

    def test_accepts_exact_two_h100(self):
        self.module.validate_devices([{"name": "NVIDIA H100 80GB HBM3", "capability": [9, 0]}] * 2)

    def test_rejects_one_h100(self):
        with self.assertRaises(ValueError):
            self.module.validate_devices([{"name": "NVIDIA H100 80GB HBM3", "capability": [9, 0]}])

    def test_rejects_wrong_family(self):
        with self.assertRaises(ValueError):
            self.module.validate_devices([{"name": "NVIDIA B300", "capability": [10, 3]}] * 2)

    def test_rejects_mixed_family(self):
        with self.assertRaises(ValueError):
            self.module.validate_devices([{"name": "NVIDIA H100", "capability": [9, 0]}, {"name": "NVIDIA H200", "capability": [9, 0]}])

    def test_complete_block_enters_and_restores_its_cuda_device(self):
        events = []

        @contextmanager
        def device(value):
            events.append(("enter", value))
            try:
                yield
            finally:
                events.append(("exit", value))

        def forward(value, *, factor):
            events.append(("forward", value))
            return value * factor

        block = types.SimpleNamespace(forward=forward)
        torch = types.SimpleNamespace(cuda=types.SimpleNamespace(device=device))
        self.module.guard_block_device(torch, block, "cuda:1")
        self.assertEqual(block.forward(3, factor=4), 12)
        self.assertEqual(events, [("enter", "cuda:1"), ("forward", 3), ("exit", "cuda:1")])


if __name__ == "__main__":
    unittest.main()

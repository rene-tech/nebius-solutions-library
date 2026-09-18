"""CPU regression tests for the specific OOM path, not generic 500 handling."""
import importlib.util
import io
from pathlib import Path
import sys
import threading
import types
import unittest
from contextlib import nullcontext, redirect_stdout
from unittest.mock import Mock, patch


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from evo2_prefill import ModelMemoryExhausted


def load(name, modules):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class MemoryErrorTests(unittest.TestCase):
    def setUp(self):
        self.generate = Mock()
        generate = self.generate

        class BaseBackend:
            def generate(self, request):
                return generate(request)

        self.runtime = types.ModuleType("evo2_deep.runtime")
        self.runtime.Evo2Backend = BaseBackend
        self.runtime.RuntimeFailure = ValueError
        self.runtime.GenerationRequest = object
        self.runtime.model_path_from_environment = Mock()
        self.runtime._driver_version = lambda: "stub"
        self.runtime._tool_identity = lambda _: {}
        modules = {"evo2_deep": types.ModuleType("evo2_deep"), "evo2_deep.runtime": self.runtime}
        self.module = load("evo2_h100", modules)
        self.backend = object.__new__(self.module.H100Backend)
        self.backend._torch = types.SimpleNamespace(
            OutOfMemoryError=MemoryError,
            cuda=types.SimpleNamespace(synchronize=Mock(), empty_cache=Mock(), device=lambda _: nullcontext()),
        )
        self.request = types.SimpleNamespace(sequence="ACGT" * 2048, num_tokens=512)

    def test_oom_is_typed_without_sequence_and_followup_recovers(self):
        self.generate.side_effect = [MemoryError("CUDA allocator internal detail"), {"sequence": "A"}]
        with patch.object(self.module.gc, "collect") as collect:
            with self.assertRaises(ModelMemoryExhausted) as raised:
                self.backend.generate(self.request)
            collect.assert_called_once()
        self.assertEqual(raised.exception.input_length, 8192)
        self.assertEqual(raised.exception.num_tokens, 512)
        self.assertNotIn("ACGT", str(raised.exception))
        self.assertNotIn("allocator", str(raised.exception))
        self.assertEqual(self.backend._torch.cuda.empty_cache.call_count, 2)
        self.assertEqual(self.backend.generate(self.request), {"sequence": "A"})

    def test_non_memory_failures_are_not_reclassified(self):
        self.generate.side_effect = ValueError("unrelated model error")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.backend.generate(self.request)
        self.backend._torch.cuda.empty_cache.assert_not_called()

    def test_success_does_not_empty_cache(self):
        self.generate.return_value = {"sequence": "AC"}
        self.assertEqual(self.backend.generate(self.request), {"sequence": "AC"})
        self.backend._torch.cuda.empty_cache.assert_not_called()
        self.assertEqual(self.backend._torch.cuda.synchronize.call_count, 4)

    def handler(self, exception):
        class BaseHandler:
            def do_POST(self):
                if exception is not None:
                    raise exception
                self._send(200, {"sequence": "AC"})

        server = types.ModuleType("evo2_deep.server")
        server.Evo2Handler = BaseHandler
        server.Evo2HTTPServer = object
        server.LoadingBackend = object
        module = load("evo2_serve", {
            "evo2_deep": types.ModuleType("evo2_deep"),
            "evo2_deep.runtime": self.runtime, "evo2_deep.server": server,
        })
        handler = object.__new__(module.MemoryAwareHandler)
        handler.evo_server = types.SimpleNamespace(generation_lock=threading.Lock())
        handler._send = Mock()
        return handler

    def test_handler_returns_actionable_500_without_disconnect(self):
        handler = self.handler(ModelMemoryExhausted(8192, 512))
        with redirect_stdout(io.StringIO()) as logged:
            handler.do_POST()
        status, response = handler._send.call_args.args
        self.assertEqual(status, 500)
        self.assertEqual(response["detail"]["code"], "MODEL_MEMORY_EXHAUSTED")
        self.assertIs(response["detail"]["retryable"], False)
        self.assertEqual(response["detail"]["input_length"], 8192)
        self.assertNotIn("ACGT", logged.getvalue())

    def test_handler_preserves_success(self):
        handler = self.handler(None)
        handler.do_POST()
        handler._send.assert_called_once_with(200, {"sequence": "AC"})

    def test_handler_does_not_swallow_unrecognized_failures(self):
        handler = self.handler(ConnectionError("connection lost"))
        with self.assertRaises(ConnectionError):
            handler.do_POST()
        handler._send.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Real CPU HTTP concurrency: health must not wait on serialized generation."""
import concurrent.futures
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


ROOT = Path(__file__).parents[1]


class OriginalHandler(BaseHTTPRequestHandler):
    @property
    def evo_server(self):
        return self.server

    def log_message(self, *_):
        pass

    def _send(self, status, value):
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send(200, {"status": "ready"})

    def do_POST(self):
        request_id = self.headers["X-Request-ID"]
        self.rfile.read(int(self.headers["Content-Length"]))
        if request_id in self.evo_server.request_ids:
            self._send(409, {"error": "request ID replay rejected"})
            return
        self.evo_server.request_ids.add(request_id)
        self._send(200, self.evo_server.backend.generate())


class OriginalServer(HTTPServer):
    def __init__(self, address, backend):
        self.backend, self.request_ids = backend, set()
        super().__init__(address, OriginalHandler)


class ConcurrencyTests(unittest.TestCase):
    def test_health_bypasses_gpu_lock_while_post_and_replay_check_remain_serial(self):
        runtime = types.ModuleType("evo2_deep.runtime")
        for name in ("Evo2Backend", "GenerationRequest", "RuntimeFailure", "model_path_from_environment"):
            setattr(runtime, name, object)
        server_module = types.ModuleType("evo2_deep.server")
        server_module.Evo2HTTPServer, server_module.Evo2Handler = OriginalServer, OriginalHandler
        server_module.LoadingBackend = object
        spec = importlib.util.spec_from_file_location("evo2_serve_concurrency", ROOT / "evo2_serve.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ROOT), *sys.path]), patch.dict(sys.modules, {
            "evo2_deep": types.ModuleType("evo2_deep"), "evo2_deep.runtime": runtime,
            "evo2_deep.server": server_module,
        }):
            spec.loader.exec_module(module)
        entered, release = threading.Event(), threading.Event()
        state = {"active": 0, "maximum": 0, "calls": 0}
        accounting_lock = threading.Lock()

        def generate():
            with accounting_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                state["calls"] += 1
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test failed to release the fake GPU")
            with accounting_lock:
                state["active"] -= 1
            return {"sequence": "AC"}

        server = module.MemoryAwareHTTPServer(("127.0.0.1", 0), types.SimpleNamespace(generate=generate))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def post(request_id):
            request = urllib.request.Request(base + "/biology/arc/evo2/generate", data=b"{}",
                headers={"Content-Type": "application/json", "X-Request-ID": request_id})
            try:
                response = urllib.request.urlopen(request, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read())

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as clients:
                first = clients.submit(post, "same-request-id")
                self.assertTrue(entered.wait(2))
                duplicate = clients.submit(post, "same-request-id")
                another = clients.submit(post, "different-request-id")
                started = time.monotonic()
                with urllib.request.urlopen(base + "/v1/health/ready", timeout=0.5) as response:
                    self.assertEqual(response.status, 200)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(state["active"], 1)
                release.set()
                self.assertEqual(first.result()[0], 200)
                self.assertEqual(duplicate.result()[0], 409)
                self.assertEqual(another.result()[0], 200)
            self.assertEqual(state, {"active": 0, "maximum": 1, "calls": 2})
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

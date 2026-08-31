from __future__ import annotations

import http.client
import importlib.util
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "stages"
    / "workloads"
    / "scripts"
    / "internal_edge_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location(
    "internal_edge_acceptance_under_test", SCRIPT_PATH
)
assert SPEC and SPEC.loader
ACCEPTANCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ACCEPTANCE)


class InternalEdgePortTests(unittest.TestCase):
    def tearDown(self) -> None:
        ACCEPTANCE.configure_local_ports(18080, 18081, 18082)

    def test_offset_tuple_configures_all_loopback_routes(self) -> None:
        ACCEPTANCE.configure_local_ports(28080, 28081, 28082)

        self.assertEqual(ACCEPTANCE.CONTROL_PORT, 28080)
        self.assertEqual(ACCEPTANCE.ADMIN_PORT, 28081)
        self.assertEqual(ACCEPTANCE.PROXY_PORT, 28082)
        self.assertEqual(ACCEPTANCE.APPLICATION_ORIGIN, "http://localhost:28082")
        self.assertEqual(ACCEPTANCE.upstream_port("/mcp"), 28080)
        self.assertEqual(ACCEPTANCE.upstream_port("/admin/"), 28081)
        self.assertEqual(ACCEPTANCE.upstream_port("/admin/?section=models"), 28081)
        self.assertEqual(ACCEPTANCE.upstream_port("/admin/api?section=models"), 28080)
        self.assertEqual(ACCEPTANCE.upstream_port("/admin/v1/tokens"), 28080)

    def test_tuple_rejects_privileged_or_colliding_ports(self) -> None:
        for ports in ((443, 28081, 28082), (28080, 28080, 28082)):
            with self.subTest(ports=ports), self.assertRaises(ValueError):
                ACCEPTANCE.configure_local_ports(*ports)

    def test_port_forward_command_accepts_wrapper_selected_kubectl(self) -> None:
        command = ACCEPTANCE.port_forward_command(
            Path("/private/kubeconfig"),
            "k8s-inference-test",
            ACCEPTANCE.CONTROL_SERVICE,
            28080,
            kubectl="kubectl-test",
        )
        self.assertEqual(command[0], "kubectl-test")
        self.assertIn("k8s-inference-test", command)

    def test_same_origin_proxy_streams_sse_before_upstream_completion(self) -> None:
        first_event = b'data: {"sequence":1}\n\n'
        second_event = b'data: {"sequence":2}\n\n'
        first_sent = threading.Event()
        release_upstream = threading.Event()

        class EventStreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(first_event)
                self.wfile.flush()
                first_sent.set()
                if not release_upstream.wait(timeout=5):
                    return
                self.wfile.write(second_event)
                self.wfile.flush()

            def log_message(self, _format: str, *args: object) -> None:
                del args

        upstream = ThreadingHTTPServer((ACCEPTANCE.BIND_ADDRESS, 0), EventStreamHandler)
        proxy = ThreadingHTTPServer(
            (ACCEPTANCE.BIND_ADDRESS, 0), ACCEPTANCE.SameOriginProxy
        )
        with socket.socket() as reserved:
            reserved.bind((ACCEPTANCE.BIND_ADDRESS, 0))
            admin_port = reserved.getsockname()[1]
        ACCEPTANCE.configure_local_ports(
            upstream.server_address[1],
            admin_port,
            proxy.server_address[1],
        )
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        upstream_thread.start()
        proxy_thread.start()
        connection = http.client.HTTPConnection(
            ACCEPTANCE.BIND_ADDRESS, proxy.server_address[1], timeout=3
        )
        try:
            connection.request("GET", "/v1/chat/completions")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(first_sent.wait(timeout=1))
            self.assertEqual(response.read(len(first_event)), first_event)
            self.assertFalse(release_upstream.is_set())
            release_upstream.set()
            self.assertEqual(response.read(), second_event)
        finally:
            release_upstream.set()
            connection.close()
            proxy.shutdown()
            upstream.shutdown()
            proxy.server_close()
            upstream.server_close()
            proxy_thread.join(timeout=2)
            upstream_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import importlib.util
import json
import queue
import threading
import time
import unittest
from pathlib import Path
from typing import Any


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location(
    "fs2_structure_common_server", SERVER_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError("cannot load common structure runtime server")
server: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class BlockingAdapter:
    paths = {"/infer"}
    identity = {"model_id": "test-model", "revision": "test-revision"}

    def __init__(self) -> None:
        self.release = threading.Event()
        self.first_entered = threading.Event()
        self.second_entered = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if call == 1:
            self.first_entered.set()
        elif call == 2:
            self.second_entered.set()
        try:
            if not self.release.wait(timeout=10):
                raise RuntimeError("test inference release timed out")
            return {"call": call, "value": payload["value"]}
        finally:
            with self._lock:
                self.active -= 1


class QuietHandler(server.Handler):
    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args


class LiveServer:
    def __init__(self) -> None:
        self.previous_state = server.STATE
        self.previous_max_request_bytes = server.MAX_REQUEST_BYTES
        self.adapter = BlockingAdapter()
        state = server.RuntimeState("test-model")
        state.adapter = self.adapter
        state.load_state = "ready"
        state.load_seconds = 1.0
        state.backend_id = "test-backend"
        server.STATE = state
        self.httpd = server.BoundedHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            name="test-http-server",
            daemon=True,
        )
        self.thread.start()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address
        return str(host), int(port)

    def close(self) -> None:
        self.adapter.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.STATE = self.previous_state
        server.MAX_REQUEST_BYTES = self.previous_max_request_bytes
        if self.thread.is_alive():
            raise RuntimeError("test HTTP server did not stop")

    def get(self, path: str, *, timeout: float = 2) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(*self.address, timeout=timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def post(
        self,
        value: str,
        *,
        request_id: str,
        timeout: float = 5,
    ) -> tuple[int, str | None, dict[str, Any]]:
        connection = http.client.HTTPConnection(*self.address, timeout=timeout)
        body = json.dumps({"value": value}).encode()
        try:
            connection.request(
                "POST",
                "/infer",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Request-Id": request_id,
                },
            )
            response = connection.getresponse()
            response_request_id = response.getheader("X-Request-Id")
            return response.status, response_request_id, json.loads(response.read())
        finally:
            connection.close()


class ThreadedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.live = LiveServer()

    def tearDown(self) -> None:
        self.live.close()

    def test_long_inference_does_not_block_health_or_readiness(self) -> None:
        responses: queue.Queue[tuple[int, str | None, dict[str, Any]]] = queue.Queue()
        inference = threading.Thread(
            target=lambda: responses.put(
                self.live.post("slow", request_id="long-request")
            ),
            name="long-inference-client",
        )
        inference.start()
        self.assertTrue(self.live.adapter.first_entered.wait(timeout=2))

        health = self.live.get("/healthz")
        readiness = self.live.get("/readyz")
        self.assertEqual(health, (200, {"status": "alive"}))
        self.assertEqual(readiness, (200, {"status": "ready"}))
        self.assertTrue(
            inference.is_alive(), "inference unexpectedly completed before release"
        )

        self.live.adapter.release.set()
        inference.join(timeout=2)
        self.assertFalse(inference.is_alive())
        status, request_id, response = responses.get_nowait()
        self.assertEqual(status, 200)
        self.assertEqual(request_id, "long-request")
        self.assertEqual(response["request_id"], "long-request")

    def test_duplicate_request_ids_are_preserved_but_inference_is_single_flight(
        self,
    ) -> None:
        responses: queue.Queue[tuple[int, str | None, dict[str, Any]]] = queue.Queue()

        def invoke(value: str) -> None:
            responses.put(self.live.post(value, request_id="duplicate-id"))

        first = threading.Thread(target=invoke, args=("first",), name="first-client")
        second = threading.Thread(target=invoke, args=("second",), name="second-client")
        first.start()
        self.assertTrue(self.live.adapter.first_entered.wait(timeout=2))
        second.start()

        self.assertFalse(
            self.live.adapter.second_entered.wait(timeout=0.25),
            "second adapter call overlapped the first",
        )
        self.assertEqual(self.live.adapter.calls, 1)
        self.assertEqual(self.live.adapter.max_active, 1)

        self.live.adapter.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(self.live.adapter.second_entered.is_set())
        self.assertEqual(self.live.adapter.calls, 2)
        self.assertEqual(self.live.adapter.max_active, 1)

        values = []
        for _ in range(2):
            status, request_id, response = responses.get_nowait()
            self.assertEqual(status, 200)
            self.assertEqual(request_id, "duplicate-id")
            self.assertEqual(response["request_id"], "duplicate-id")
            values.append(response["output"]["value"])
        self.assertEqual(sorted(values), ["first", "second"])
        self.assertEqual(server.STATE.metrics["accepted"], 2)
        self.assertEqual(server.STATE.metrics["completed"], 2)
        self.assertEqual(server.STATE.active, 0)

    def test_saturated_post_capacity_reserves_a_health_handler(self) -> None:
        responses: queue.Queue[
            tuple[int, str | None, dict[str, Any]] | BaseException
        ] = queue.Queue()

        def invoke(index: int) -> None:
            try:
                responses.put(
                    self.live.post(
                        f"queued-{index}",
                        request_id=f"queued-request-{index}",
                        timeout=10,
                    )
                )
            except BaseException as exc:
                responses.put(exc)

        clients = [
            threading.Thread(
                target=invoke, args=(index,), name=f"queued-client-{index}"
            )
            for index in range(self.live.httpd.max_post_threads)
        ]
        for client in clients:
            client.start()
        self.assertTrue(self.live.adapter.first_entered.wait(timeout=2))

        deadline = time.monotonic() + 3
        post_slots_exhausted = False
        while time.monotonic() < deadline:
            if not self.live.httpd.acquire_post_slot():
                post_slots_exhausted = True
                break
            self.live.httpd.release_post_slot()
            time.sleep(0.01)
        self.assertTrue(post_slots_exhausted, "POST handler capacity did not saturate")
        self.assertEqual(self.live.adapter.calls, 1)
        self.assertEqual(
            self.live.httpd.max_post_threads
            + self.live.httpd.reserved_non_post_threads,
            self.live.httpd.max_request_threads,
        )

        status, request_id, response = self.live.post(
            "overload", request_id="overload-request", timeout=2
        )
        self.assertEqual(status, 503)
        self.assertEqual(request_id, "overload-request")
        self.assertEqual(response["request_id"], "overload-request")
        self.assertEqual(response["error"], {"code": "runtime_busy", "retryable": True})
        self.assertEqual(self.live.get("/healthz"), (200, {"status": "alive"}))

        self.live.adapter.release.set()
        for client in clients:
            client.join(timeout=5)
            self.assertFalse(client.is_alive())
        for _ in clients:
            queued_response = responses.get_nowait()
            if isinstance(queued_response, BaseException):
                raise queued_response
            queued_status, queued_request_id, queued_body = queued_response
            self.assertEqual(queued_status, 200)
            self.assertEqual(queued_request_id, queued_body["request_id"])
        self.assertEqual(self.live.adapter.max_active, 1)

    def test_payload_bound_and_request_id_error_contract_are_unchanged(self) -> None:
        server.MAX_REQUEST_BYTES = 8
        status, request_id, response = self.live.post(
            "larger-than-eight-bytes", request_id="bounded-request"
        )
        self.assertEqual(status, 413)
        self.assertEqual(request_id, "bounded-request")
        self.assertEqual(response["request_id"], "bounded-request")
        self.assertEqual(
            response["error"], {"code": "request_size", "retryable": False}
        )
        self.assertEqual(self.live.adapter.calls, 0)

    def test_server_close_waits_for_the_inflight_inference(self) -> None:
        responses: queue.Queue[tuple[int, str | None, dict[str, Any]]] = queue.Queue()
        inference = threading.Thread(
            target=lambda: responses.put(
                self.live.post("slow", request_id="shutdown-request")
            ),
            name="shutdown-inference-client",
        )
        inference.start()
        self.assertTrue(self.live.adapter.first_entered.wait(timeout=2))

        self.live.httpd.shutdown()
        close_finished = threading.Event()

        def close_server() -> None:
            self.live.httpd.server_close()
            close_finished.set()

        closer = threading.Thread(target=close_server, name="server-close")
        closer.start()
        self.assertFalse(
            close_finished.wait(timeout=0.25),
            "server_close returned before the in-flight inference completed",
        )

        self.live.adapter.release.set()
        inference.join(timeout=2)
        closer.join(timeout=2)
        self.assertFalse(inference.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(close_finished.is_set())
        status, request_id, response = responses.get_nowait()
        self.assertEqual(status, 200)
        self.assertEqual(request_id, "shutdown-request")
        self.assertEqual(response["request_id"], "shutdown-request")


if __name__ == "__main__":
    unittest.main()

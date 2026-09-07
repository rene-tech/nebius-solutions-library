"""Serial localhost transport for immutable scientific snapshot workers.

This is a pod-local loader bridge, not a new public API or tenant boundary.
The original scientific adapter remains responsible for its input/provenance
contract and its normal result envelope. No input is present at capture.
"""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
import os


def serve(backend):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, document):
            payload = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404,
                         backend.ready if self.path == "/health" else {})

        def do_POST(self):
            if self.path == "/prepare-snapshot":
                self.respond(200, backend.prepare_snapshot())
                return
            if self.path != "/execute":
                self.respond(404, {})
                return
            output, errors = StringIO(), StringIO()
            exit_code = 0
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 1 <= size <= 1024 * 1024:
                    raise ValueError("invalid pod-local request length")
                request = json.loads(self.rfile.read(size))
                arguments = request["argv"]
                if (not isinstance(arguments, list) or not arguments
                    or arguments[0] != backend.command
                    or not all(isinstance(argument, str) for argument in arguments)):
                    raise ValueError("request does not match the scientific stage CLI")
                allowed_environment = getattr(backend, "environment_keys", ())
                environment = request.get("environment", {})
                if not isinstance(environment, dict) or any(
                    key not in allowed_environment or not isinstance(value, str)
                    for key, value in environment.items()
                ):
                    raise ValueError("request environment differs from the scientific stage contract")
                previous_environment = {key: os.environ.get(key) for key in allowed_environment}
                original_uid, original_gid = os.geteuid(), os.getegid()
                try:
                    for key in allowed_environment:
                        os.environ.pop(key, None)
                    os.environ.update(environment)
                    os.setegid(int(os.environ.get("FS2_SNAPSHOT_REQUEST_GID", "10001")))
                    os.seteuid(int(os.environ.get("FS2_SNAPSHOT_REQUEST_UID", "10001")))
                    with redirect_stdout(output), redirect_stderr(errors):
                        backend.execute(arguments)
                finally:
                    os.seteuid(original_uid)
                    os.setegid(original_gid)
                    for key, value in previous_environment.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            except SystemExit as error:
                exit_code = error.code if isinstance(error.code, int) else 1
                if not isinstance(error.code, int):
                    errors.write(str(error.code) + "\n")
            except Exception as error:
                exit_code = 1
                errors.write(str(error) + "\n")
            self.respond(200, {"exit_code": exit_code, "stdout": output.getvalue(),
                               "stderr": errors.getvalue()})

    print(json.dumps({"event": "scientific_worker_ready", **backend.ready}), flush=True)
    HTTPServer(("127.0.0.1", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()

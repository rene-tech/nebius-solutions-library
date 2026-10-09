import contextlib
import hashlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import httpx

spec = importlib.util.spec_from_file_location("registry_probe", Path(__file__).with_name("registry_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class RegistryProbeTests(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_registry_service_is_taken_from_verified_challenge(self):
        requests = []
        manifest = {"config": {"digest": "sha256:" + "a" * 64}, "layers": [{"size": 123}]}

        def handler(request):
            requests.append(request)
            if request.url.path == "/proxy_auth":
                self.assertEqual(request.url.params["service"], "registry")
                self.assertEqual(request.url.params["scope"], f"repository:{probe.REPOSITORY}:pull")
                self.assertTrue(request.headers["authorization"].startswith("Basic "))
                return httpx.Response(200, json={"token": "synthetic-registry-token"})
            if request.headers.get("authorization"):
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-registry-token")
                return httpx.Response(200, json=manifest)
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Bearer realm="https://nvcr.io/proxy_auth",service="registry"'}
            )

        result = probe.probe(self.client(handler), "synthetic-key")
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["compressed_layer_bytes"], 123)
        self.assertTrue(result["authenticated_manifest_access"])
        self.assertNotIn("synthetic", json.dumps(result))

    def test_rejects_external_realm_without_sending_credentials(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer realm="https://example.invalid/proxy_auth",service="registry"'},
            )

        with self.assertRaises(probe.ProbeError) as error:
            probe.probe(self.client(handler), "synthetic-key")
        self.assertEqual(error.exception.phase, "untrusted-authentication-realm")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("authorization", requests[0].headers)

    def test_local_403_does_not_send_a_key(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(403)

        with self.assertRaises(probe.ProbeError) as error:
            probe.probe(self.client(handler), "synthetic-key")
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(error.exception.phase, "image-manifest")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("authorization", requests[0].headers)

    def test_digest_mismatch_is_rejected(self):
        with self.assertRaises(probe.ProbeError):
            probe.digest_bytes(httpx.Response(200, content=b"other"), "sha256:" + "0" * 64)

    def test_launch_configuration_strips_auth_on_cdn_and_omits_environment(self):
        config = json.dumps(
            {"config": {"Entrypoint": ["/entrypoint"], "Cmd": ["start_server"], "Env": ["PRIVATE=synthetic-sensitive"]}}
        ).encode()
        digest = "sha256:" + hashlib.sha256(config).hexdigest()
        manifest = {"config": {"digest": digest}, "layers": []}

        def handler(request):
            if request.url.host == "images.cloudfront.net":
                self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, content=config)
            if request.url.path == "/proxy_auth":
                return httpx.Response(200, json={"token": "synthetic-registry-token"})
            if request.url.path.endswith(digest):
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-registry-token")
                return httpx.Response(
                    307, headers={"location": "https://images.cloudfront.net/config?signed=synthetic"}
                )
            if request.headers.get("authorization"):
                return httpx.Response(200, json=manifest)
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Bearer realm="https://nvcr.io/proxy_auth",service="registry"'}
            )

        result = probe.probe(self.client(handler), "synthetic-key", True)
        self.assertEqual(result["launch_configuration"]["Entrypoint"], ["/entrypoint"])
        self.assertNotIn("synthetic", json.dumps(result))
        self.assertNotIn("Env", result["launch_configuration"])

    def test_exception_details_are_not_printed(self):
        output = io.StringIO()
        with (
            mock.patch.dict(probe.os.environ, {"NGC_API_KEY": "synthetic-key"}),
            mock.patch.object(probe, "probe", side_effect=ValueError("synthetic-key private-response")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(probe.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "error_type": "ValueError"})


if __name__ == "__main__":
    unittest.main()

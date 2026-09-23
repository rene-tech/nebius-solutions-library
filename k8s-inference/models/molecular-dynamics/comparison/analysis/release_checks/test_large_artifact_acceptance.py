"""Synthetic local transport fixtures only; no external request is made."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from large_artifact_acceptance import ARTIFACTS, CHUNK, debug_pass, download, memory_bytes, ready_exact, restart_deltas, sanitize_debug


class GeneratedStream(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(32):
            yield b"x" * CHUNK


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.artifact = {"id": ARTIFACTS[0]["id"], "size": 32 * CHUNK, "sha256": hashlib.sha256(b"x" * (32 * CHUNK)).hexdigest()}

    def transport(self, wrong_digest=False):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.headers["authorization"], "Bearer synthetic-secret")
            headers = {"x-fs2-artifact-id": self.artifact["id"], "x-fs2-artifact-size-bytes": str(self.artifact["size"]), "content-length": str(self.artifact["size"]), "x-fs2-artifact-sha256": "0" * 64 if wrong_digest else self.artifact["sha256"]}
            return httpx.Response(200, headers=headers, stream=GeneratedStream())
        return httpx.MockTransport(handler)

    def test_full_stream_digest_discard_and_no_secret_output(self):
        row = download("https://synthetic.invalid", "synthetic-secret", self.artifact, transport=self.transport())
        self.assertTrue(row["verified"])
        self.assertEqual(row["received_bytes"], self.artifact["size"])
        self.assertFalse(row["scientific_bytes_retained"])
        self.assertNotIn("synthetic-secret", json.dumps(row))

    def test_partial_close_is_not_successful_full_transfer(self):
        row = download("https://synthetic.invalid", "synthetic-secret", self.artifact, partial=True, transport=self.transport())
        self.assertTrue(row["intentional_close"])
        self.assertEqual(row["received_bytes"], 1024 * 1024)
        self.assertFalse(row["verified"])

    def test_response_identity_failure_cannot_pass(self):
        row = download("https://synthetic.invalid", "synthetic-secret", self.artifact, transport=self.transport(True))
        self.assertFalse(row["verified"])
        self.assertEqual(row["received_bytes"], 0)
        self.assertEqual(row["error_code"], "artifact_response_identity_mismatch")

    def test_digest_mismatch_after_complete_bytes_cannot_pass(self):
        wrong = {**self.artifact, "sha256": "0" * 64}
        row = download("https://synthetic.invalid", "synthetic-secret", wrong, transport=self.transport(True))
        self.assertEqual(row["received_bytes"], wrong["size"])
        self.assertFalse(row["verified"])
        self.assertEqual(row["error_code"], "artifact_size_or_digest_mismatch")

    def test_restart_identity_and_delta(self):
        before = {"pods": [{"uid": "uid1", "statuses": [{"name": "cp", "restartCount": 2}]}]}
        after = deepcopy(before)
        self.assertEqual(restart_deltas(before, after)["deltas"][0]["restart_delta"], 0)
        after["pods"][0]["statuses"][0]["restartCount"] += 1
        self.assertEqual(restart_deltas(before, after)["deltas"][0]["restart_delta"], 1)
        after["pods"][0]["uid"] = "newuid"
        self.assertFalse(restart_deltas(before, after)["same_pod_container_identities"])

    def test_three_ready_exact_image_and_unchanged_limit(self):
        pod = {"deleting": None, "containers": [{"image": "exact", "resources": {"limits": {"memory": "2Gi"}}}], "statuses": [{"ready": True}]}
        snapshot = {"pods": [deepcopy(pod) for _ in range(3)]}
        self.assertTrue(ready_exact(snapshot, "exact"))
        snapshot["pods"][0]["containers"][0]["resources"]["limits"]["memory"] = "4Gi"
        self.assertFalse(ready_exact(snapshot, "exact"))

    def test_memory_units(self):
        self.assertEqual(memory_bytes("512Mi"), 512 * 1024**2)
        self.assertEqual(memory_bytes("200M"), 200_000_000)
        with self.assertRaises(ValueError):
            memory_bytes("unknown")

    def test_debug_only_own_metadata_and_exact_reference(self):
        artifact = ARTIFACTS[0]
        detail = {"id": "exchange", "request_id": "our-request", "tenant_id": "our-tenant", "principal_id": "our-principal", "endpoint": f"/v1/artifacts/{artifact['id']}/content", "response_body": {"data": "reference only", "capture_mode": "artifact_reference", "observed_bytes": artifact["size"], "complete": True, "artifact_reference": {"artifact_id": artifact["id"], "sha256": artifact["sha256"], "size_bytes": artifact["size"], "delivered_bytes": artifact["size"], "observed_sha256": artifact["sha256"], "verified": True}}, "request_headers": [["authorization", "do not export"]]}
        row = sanitize_debug(detail, {"our-request"}, "our-tenant", "our-principal")
        self.assertNotIn("do not export", json.dumps(row))
        transfer = {"request_id": "our-request", "mode": "full-stream-retry", "artifact": artifact}
        self.assertTrue(debug_pass({"rows": [row]}, [transfer]))
        row["response_body_metadata"]["artifact_reference"]["verified"] = False
        self.assertFalse(debug_pass({"rows": [row]}, [transfer]))
        with self.assertRaises(ValueError):
            sanitize_debug(detail, {"our-request"}, "different-tenant", "our-principal")


if __name__ == "__main__":
    unittest.main()

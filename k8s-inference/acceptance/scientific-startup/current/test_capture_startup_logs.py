import unittest

from capture_startup_logs import consume_json, pod_projection


class WatchCaptureTests(unittest.TestCase):
    def test_concatenated_and_partial_json(self):
        records, tail = consume_json(' {"type":"ADDED"}\n{"object":')
        self.assertEqual(records, [{"type": "ADDED"}])
        self.assertEqual(tail, '{"object":')
        records, tail = consume_json(tail + '{}}')
        self.assertEqual(records, [{"object": {}}])
        self.assertEqual(tail, "")

    def test_projection_omits_credentials_and_does_not_claim_model_ready(self):
        pod = {
            "metadata": {"uid": "pod", "annotations": {"token": "private"}},
            "spec": {"containers": [{"name": "stage", "image": "pinned", "env": [{"value": "private"}], "args": ["private"]}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        view = pod_projection(pod)
        self.assertNotIn("private", repr(view))
        self.assertNotIn("model_ready", view)
        self.assertEqual(view["conditions"], pod["status"]["conditions"])


if __name__ == "__main__":
    unittest.main()

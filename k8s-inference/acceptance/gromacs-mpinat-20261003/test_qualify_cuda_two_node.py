import json
from pathlib import Path
import tempfile
import unittest

from qualify_cuda_two_node import PROBE_CODE, fixture, manifest, passed


class TwoNodeGateTests(unittest.TestCase):
    def test_exact_two_node_pool_and_no_scheduler_bypass(self):
        value = manifest("fs2-gmx-cuda-tcp-test", "repo@sha256:abc", ["node-a", "node-b"], "private-seed")
        job = value["spec"]["replicatedJobs"][0]
        self.assertEqual(job["replicas"], 2)
        pod = job["template"]["spec"]["template"]["spec"]
        self.assertNotIn("nodeName", pod)
        self.assertEqual(pod["nodeSelector"], {"accelerator.fs2.nebius/pool-id": "h100-ondemand-1x"})
        self.assertEqual(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"], ["node-a", "node-b"])
        self.assertEqual(pod["containers"][0]["resources"]["requests"]["nvidia.com/gpu"], "1")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(job["template"]["spec"]["activeDeadlineSeconds"], 1200)

    def test_probe_reuses_native_launcher_and_is_bounded(self):
        self.assertIn("prepare_keys(w)", PROBE_CODE)
        self.assertIn("configure_launcher(w,d,request['threads'],1)", PROBE_CODE)
        self.assertIn("launch_command(request", PROBE_CODE)
        self.assertIn("timeout=90", PROBE_CODE)
        self.assertNotIn("GMX_FORCE", PROBE_CODE)
        self.assertNotIn("UCX_TLS=", PROBE_CODE)

    def test_fixture_only_changes_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source", Path(tmp) / "output"
            source.mkdir()
            request = {"schema": "fs2-serve.nebius.ai/gromacs-mpi-workflow-request/v1", "nodes": 1, "gpus_per_node": 2,
                       "threads": 8, "jobs": [{"id": "gang", "steps": [{"id": "run", "command": "mdrun",
                       "args": ["-s", "benchmark.tpr", "-pme", "gpu", "-npme", "1", "-notunepme"]}]}]}
            (source / "request.json").write_text(json.dumps(request))
            (source / "input.tar.gz").write_bytes(b"exact-input")
            fixture(source, output)
            result = json.loads((output / "request.json").read_text())
            self.assertEqual((result["nodes"], result.get("gpus_per_node", 1)), (2, 1))
            self.assertEqual(result["jobs"][0]["steps"][0]["args"], request["jobs"][0]["steps"][0]["args"])
            self.assertEqual((output / "input.tar.gz").read_bytes(), b"exact-input")

    def test_device_probe_is_an_independent_required_gate(self):
        record = {"validation": {"status": "passed"}, "cleanup": "absence observed"}
        self.assertTrue(passed(record, False))
        self.assertFalse(passed(record, True))
        record["device_buffer_validation"] = {"status": "failed"}
        self.assertFalse(passed(record, True))
        record["device_buffer_validation"] = {"status": "passed"}
        self.assertTrue(passed(record, True))
        record["cleanup"] = "not confirmed"
        self.assertFalse(passed(record, True))


if __name__ == "__main__":
    unittest.main()

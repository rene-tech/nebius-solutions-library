"""Offline subprocess and append-only checks; no kubectl or GPU work is launched."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import observe


class ObservationReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_output_retains_both_streams_and_replaces_invalid_utf8(self):
        result = await observe.read_observation([
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(b'out\\xff'); sys.stderr.buffer.write(b'err\\xff')",
        ])
        self.assertEqual(result, {"returncode": 0, "stdout": "out\ufffd", "stderr": "err\ufffd"})

    async def test_nonzero_exit_remains_an_observation_not_a_raised_campaign_error(self):
        result = await observe.read_observation([
            sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
        ])
        self.assertEqual(result, {"returncode": 7, "stdout": "out\n", "stderr": "err\n"})

    async def test_large_stdout_and_stderr_are_both_drained(self):
        result = await observe.read_observation([
            sys.executable, "-c",
            "import sys; sys.stdout.write('o'*262144); sys.stderr.write('e'*262144)",
        ])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "o" * 262144)
        self.assertEqual(result["stderr"], "e" * 262144)

    async def test_timeout_returns_existing_unknown_error_shape_without_wait_deadlock(self):
        started = time.monotonic()
        result = await observe.read_observation([
            sys.executable, "-c",
            "import sys,time; sys.stdout.write('o'*262144); sys.stderr.write('e'*262144); "
            "sys.stdout.flush(); sys.stderr.flush(); time.sleep(30)",
        ], timeout=0.2)
        self.assertEqual(result, {"error": "observation_timeout"})
        self.assertLess(time.monotonic() - started, 3)

    async def test_timeout_kills_the_isolated_descendant_group_even_after_parent_exit(self):
        # The direct parent exits successfully while a child still owns both
        # inherited pipes. Waiting for the parent alone cannot drain the reader.
        source = (
            "import subprocess,sys; sys.stdout.write('o'*262144); sys.stderr.write('e'*262144); "
            "sys.stdout.flush(); sys.stderr.flush(); "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"
        )
        started = time.monotonic()
        result = await observe.read_observation([sys.executable, "-c", source], timeout=0.2)
        self.assertEqual(result, {"error": "observation_timeout"})
        self.assertLess(time.monotonic() - started, 3)

    async def test_cancellation_propagates_after_draining_and_reaping_the_local_reader(self):
        original_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event()
        processes = []

        async def spawn(*args, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            created.set()
            return process

        with patch("acceptance_subprocess.asyncio.create_subprocess_exec", new=spawn):
            task = asyncio.create_task(observe.read_observation([
                sys.executable, "-c",
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "sys.stdout.write('o'*262144); sys.stderr.write('e'*262144); "
                "sys.stdout.flush(); sys.stderr.flush(); time.sleep(30)",
            ]))
            await asyncio.wait_for(created.wait(), timeout=3)
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)


class ObserverAppendTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_timeout_is_appended_as_error_not_empty_success(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            old = '{"observed_at":"2026-10-03T00:00:00Z","error":"retained"}\n'
            (output / "errors.jsonl").write_text(old)
            pod = {"metadata": {"uid": "owned-pod", "name": "owned",
                                "labels": {"fs2.nebius.ai/model-id": "gromacs"}},
                   "spec": {"nodeName": "node", "containers": []}, "status": {}}
            calls = []

            async def command(arguments, **kwargs):
                calls.append(arguments)
                if arguments[4:6] == ["get", "pods"]:
                    return {"returncode": 0, "stdout": json.dumps({"items": [pod]}), "stderr": ""}
                return {"error": "observation_timeout"}

            clock = SimpleNamespace(monotonic=iter([0, 0, 0, 0, 2]).__next__)
            with patch("observe.read_observation", new=command), \
                    patch("observe.time", new=clock), patch("observe.asyncio.sleep", new=AsyncMock()):
                await observe.main(SimpleNamespace(output=output, context="test-only", seconds=1, interval=0))
            content = (output / "errors.jsonl").read_text()
            self.assertTrue(content.startswith(old))
            rows = [json.loads(line) for line in content.splitlines()]
            self.assertEqual({row["kind"] for row in rows[1:]}, {"metrics", "nodes", "events"})
            self.assertTrue(all(row["error"] == "observation_timeout" for row in rows[1:]))
            self.assertTrue(all(not (output / (name + ".jsonl")).exists() for name in ("metrics", "nodes", "events")))
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(call[:5] == ["kubectl", "--context", "test-only", "--request-timeout=15s", "get"]
                                for call in calls))

    async def test_stop_marker_preserves_existing_samples_and_does_not_launch_a_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            old = '{"observed_at":"2026-10-03T00:00:00Z","pod":{"metadata":{"uid":"old"}}}\n'
            (output / "pods.jsonl").write_text(old)
            (output / "STOP").touch()
            with patch("observe.read_observation", new=AsyncMock()) as read:
                await observe.main(SimpleNamespace(output=output, context="test-only", seconds=1, interval=0))
            read.assert_not_awaited()
            self.assertEqual((output / "pods.jsonl").read_text(), old)
            closeout = json.loads((output / "observer-closeout.jsonl").read_text())
            self.assertEqual(closeout["state"], "stopped")


if __name__ == "__main__":
    unittest.main()

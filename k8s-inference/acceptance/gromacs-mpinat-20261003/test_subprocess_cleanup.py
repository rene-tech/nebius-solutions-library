import asyncio
import sys
import time
import unittest

from run_rest import Campaign  # noqa: F401 -- adds the established runner path
from verify_concurrency import read_command


class SubprocessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_output_completes_without_pipe_deadlock(self):
        output = await read_command([sys.executable, "-c", "import sys; sys.stdout.write('x' * 262144)"])
        self.assertEqual(len(output), 262144)

    async def test_timeout_drains_buffered_output_and_child_group(self):
        # A child inherits the pipes after the direct parent exits. Merely
        # waiting for/killing that parent cannot finish communicate().
        source = ("import subprocess,sys; sys.stdout.write('x' * 262144); sys.stdout.flush(); "
                  "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await read_command([sys.executable, "-c", source], timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)

    async def test_cancellation_drains_reader(self):
        task = asyncio.create_task(read_command([
            sys.executable, "-c", "import sys,time; sys.stdout.write('x'*262144); sys.stdout.flush(); time.sleep(30)"]))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)

    async def test_nonzero_exit_is_reported_without_command_output(self):
        with self.assertRaisesRegex(RuntimeError, "Local read/observation command failed"):
            await read_command([sys.executable, "-c", "import sys; print('private diagnostic'); sys.exit(2)"])

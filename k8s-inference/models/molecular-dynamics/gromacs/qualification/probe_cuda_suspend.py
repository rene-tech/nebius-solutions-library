"""Measure GPU-only suspend/resume of our own finite GROMACS test process.

This is not CRIU persistence, Pod recovery, or a deployment snapshot feature.
No driver, GPU compute mode, host process, or cluster configuration is changed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tpr", type=Path, required=True)
    parser.add_argument("--checkpoint-tool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.tpr, args.output / "run.tpr")
    events = []

    def checkpoint(*options):
        started = time.monotonic()
        result = subprocess.run(
            [str(args.checkpoint_tool), *options, "--pid", str(child.pid)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        events.append(
            {
                "options": options,
                "seconds": time.monotonic() - started,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return result

    started = time.monotonic()
    with (args.output / "mdrun.stdout.log").open("wb") as output:
        child = subprocess.Popen(
            [
                "/usr/local/gromacs/avx2_256/bin/gmx",
                "mdrun",
                "-s",
                "run.tpr",
                "-deffnm",
                "md",
                "-ntmpi",
                "1",
                "-ntomp",
                "8",
                "-pin",
                "off",
            ],
            cwd=args.output,
            env={**os.environ, "OMP_NUM_THREADS": "8"},
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            ready = False
            while child.poll() is None and time.monotonic() - started < 30:
                log = (args.output / "mdrun.stdout.log").read_text(errors="replace")
                if "starting mdrun" in log:
                    ready = True
                    break
                time.sleep(0.05)
            first_step_ready = time.monotonic() - started
            if not ready:
                raise RuntimeError("finite test did not reach native mdrun startup")
            checkpoint("--get-state")
            suspended = False
            locked = checkpoint("--action", "lock", "--timeout", "5000").returncode == 0
            if locked:
                suspended = checkpoint("--action", "checkpoint").returncode == 0
                checkpoint("--get-state")
                if suspended:
                    if checkpoint("--action", "restore").returncode:
                        raise RuntimeError("GPU restore failed on this test process")
                checkpoint("--action", "unlock")
            code = child.wait(timeout=120)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            record = {
                "pid": child.pid,
                "worker_exit_code": child.returncode,
                "wall_seconds": time.monotonic() - started,
                "events": events,
                "tool_sha256": hashlib.sha256(
                    args.checkpoint_tool.read_bytes()
                ).hexdigest(),
                "tpr_sha256": hashlib.sha256(args.tpr.read_bytes()).hexdigest(),
                "durable_process_snapshot_tested": False,
                "customer_path_tested": False,
            }
            if "first_step_ready" in locals():
                record["native_startup_log_seconds"] = first_step_ready
            (args.output / "probe.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))


if __name__ == "__main__":
    main()

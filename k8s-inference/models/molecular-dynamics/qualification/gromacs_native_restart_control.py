"""Image-cached native restart comparator for the isolated snapshot screen.

This is not an exactly state-matched timing comparison: a native checkpoint can
precede the CUDA capture point. Both native steps are retained in the receipt.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captured", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, "/snapshot-source")
    from snapshot_probe import progress
    from supervisor import bind_allocated_gpu, configure_runtime_cache
    from benchmark_sm89 import validate

    captured = json.loads((args.captured / "capture-probe.json").read_text())
    plan = json.loads((args.captured / "plan.json").read_text())
    args.directory.mkdir(exist_ok=False)
    work = args.directory / "work"
    shutil.copytree(args.captured / "native-restart-seed", work)
    shutil.copytree(args.captured / "cache", args.directory / "cache")
    bind_allocated_gpu()
    configure_runtime_cache(args.directory)
    binary = plan["argv"][0]
    checkpoint = subprocess.check_output([binary, "dump", "-cp", str(work / "md.cpt")], text=True, stderr=subprocess.DEVNULL)
    native_step = int(re.search(r"^\s*step\s*=\s*(\d+)", checkpoint, re.MULTILINE)[1])
    receipt = {
        "native_checkpoint_step": native_step, "snapshot_logged_step": captured["captured_logged_step"],
        "exact_state_matched": False, "image_cached_process_timing_only": True,
        "includes_scheduling_or_image_pull": False,
    }
    for path in (args.directory, *args.directory.rglob("*")):
        os.chown(path, 10001, 10001)
    started = time.monotonic()
    with (args.directory / "worker.log").open("wb") as output:
        child = subprocess.Popen(
            [*plan["argv"], "-cpi", "md.cpt", "-append"], cwd=work,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, user=10001, group=10001, extra_groups=[],
            env={**os.environ, **plan.get("environment", {})},
        )
    try:
        while child.poll() is None:
            step = progress(args.directory, plan)
            if step is not None and step > captured["captured_logged_step"] and "first_new_logged_step" not in receipt:
                receipt["first_new_logged_step"] = step
                receipt["native_restart_to_new_logged_step_seconds"] = time.monotonic() - started
            if time.monotonic() - started > 180:
                raise RuntimeError("native restart exceeded bounded completion time")
            time.sleep(0.05)
        receipt["returncode"] = child.returncode
        receipt["native_restart_to_completion_seconds"] = time.monotonic() - started
        receipt["validation"] = validate(binary, work, 200000, 400, ["md.xtc"])
        receipt["status"] = "passed" if child.returncode == 0 and receipt["validation"].get("passed") else "failed"
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=20)
        (args.directory / "native-control.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    if receipt.get("status") != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

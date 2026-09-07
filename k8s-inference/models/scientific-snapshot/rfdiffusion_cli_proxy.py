#!/usr/bin/env python3
"""Optional RF loader bridge; all original FS2 request/result checks remain."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.request


def remote_upstream(argv, *, cwd, log_path, environment, timeout_seconds):
    origin = os.environ["FS2_RFDIFFUSION_WORKER_URL"]
    if origin != "http://127.0.0.1:8000":
        raise ValueError("snapshot worker must be this Pod's localhost runtime")
    if Path(argv[1]) != Path(cwd) / "scripts/run_inference.py":
        raise ValueError("expected the unchanged native RF inference script")
    allowed = ("HOME", "XDG_CACHE_HOME", "MPLCONFIGDIR", "DGL_HOME", "OMP_NUM_THREADS",
               "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    request = urllib.request.Request(origin + "/execute", data=json.dumps({
        "argv": ["run-inference", *argv[2:]],
        "environment": {key: environment[key] for key in allowed if key in environment},
    }).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        result = json.load(response)
    output = result.get("stdout", "") + result.get("stderr", "")
    log_path.write_text(output)
    sys.stdout.write(output)
    markers = [json.loads(line.removeprefix("FS2_RF_SNAPSHOT_EXECUTION ")) for line in output.splitlines()
               if line.startswith("FS2_RF_SNAPSHOT_EXECUTION ")]
    first_design = markers[-1]["first_design_seconds"] if markers else None
    return result["exit_code"], first_design, output.splitlines()[-200:]


def main():
    # Canonical image-owned source is mounted under a separate immutable key;
    # there is no copied/forked implementation of its scientific contract.
    path = Path("/snapshot-source/rfdiffusion_runtime_entrypoint.py")
    spec = importlib.util.spec_from_file_location("fs2_native_rf_runtime", path)
    runtime = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runtime
    spec.loader.exec_module(runtime)
    if os.environ.get("FS2_RFDIFFUSION_WORKER_URL"):
        runtime.run_upstream = remote_upstream
    return runtime.main()


if __name__ == "__main__":
    raise SystemExit(main())

#!/opt/venv/bin/python
"""Keep the original Mosaic adapter CLI; optionally reuse its restored model."""

import json
import os
import runpy
import sys
import urllib.request


def main():
    origin = os.environ.get("FS2_MOSAIC_WORKER_URL", "")
    if not origin or sys.argv[1:2] != ["run-shard"]:
        runpy.run_path("/opt/fs2/mosaic/runtime_entrypoint_original.py", run_name="__main__")
        return
    if origin != "http://127.0.0.1:8000":
        raise ValueError("snapshot worker must be this Pod's localhost runtime")
    environment = {key: os.environ[key] for key in ("FS2_INPUT_ARTIFACT_ROOT",) if key in os.environ}
    request = urllib.request.Request(
        origin + "/execute", data=json.dumps({"argv": sys.argv[1:], "environment": environment}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=86400) as response:
        result = json.load(response)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()

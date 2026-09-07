#!/opt/protenix-venv/bin/python
"""Optional loader bridge; absence of worker URL runs the original CLI."""

import json
import os
import sys
import urllib.request


def main():
    origin = os.environ.get("FS2_PROTENIX_WORKER_URL", "")
    if not origin:
        from runner.batch_inference import protenix_cli

        protenix_cli()
        return
    if origin != "http://127.0.0.1:8000":
        raise ValueError("snapshot worker must be this Pod's localhost runtime")
    request = urllib.request.Request(
        origin + "/execute", data=json.dumps({"argv": sys.argv[1:]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=86400) as response:
        result = json.load(response)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()

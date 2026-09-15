#!/usr/bin/env python3
"""Sample only the GPU allocated to this isolated benchmark pod.

Utilization samples are diagnostic observations, not exact kernel-active time
or billing. Allocation timestamps remain the cost denominator.
"""
import argparse
from datetime import datetime, timezone
import json
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=180)
args = parser.parse_args()
started = time.monotonic()
while time.monotonic() - started < args.seconds:
    query = subprocess.run([
        "nvidia-smi", "--query-gpu=uuid,utilization.gpu,memory.used,power.draw",
        "--format=csv,noheader,nounits",
    ], capture_output=True, text=True, timeout=5, check=False)
    print(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                      "elapsed_seconds": time.monotonic() - started,
                      "return_code": query.returncode,
                      "csv": query.stdout.strip(), "stderr": query.stderr.strip()}), flush=True)
    time.sleep(1)

#!/usr/bin/env python3
"""Run the single canonical lifecycle implementation; no copied provisioner."""

import os
from pathlib import Path
import sys

root = Path(os.environ.get("FS2_LIFECYCLE_ROOT", Path(__file__).resolve().parents[2]))
script = root / "lifecycle.py"
if not script.is_file():
    raise SystemExit(
        "Set FS2_LIFECYCLE_ROOT to k8s-inference/operations/tenant-lifecycle"
    )
os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])

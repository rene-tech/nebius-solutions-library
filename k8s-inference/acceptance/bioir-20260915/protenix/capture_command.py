#!/usr/bin/env python3
"""Retain sanitized evaluation commands and outputs; never pass credentials."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

tag, *command = sys.argv[1:]
if not tag.replace("-", "").replace("_", "").isalnum():
    raise SystemExit("tag must be a simple evidence name")
root = Path(__file__).resolve().parent / "raw"
root.mkdir(exist_ok=True)
started = time.monotonic()
created = datetime.now(timezone.utc).isoformat()
result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
(root / (tag + ".stdout")).write_text(result.stdout)
(root / (tag + ".stderr")).write_text(result.stderr)
(root / (tag + ".command.json")).write_text(json.dumps({
    "command": command, "created_at": created,
    "elapsed_seconds": time.monotonic() - started, "return_code": result.returncode,
}, indent=2) + "\n")
print(result.stdout[-2000:])
print(result.stderr[-1000:], file=sys.stderr)
raise SystemExit(result.returncode)

"""Run an evidence command, retaining its clock bounds and complete output."""
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent / "raw"
root.mkdir(exist_ok=True)
tag, *command = sys.argv[1:]
started = time.time()
result = subprocess.run(command, capture_output=True, text=True, check=False)
(root / f"{tag}.stdout").write_text(result.stdout)
(root / f"{tag}.stderr").write_text(result.stderr)
(root / f"{tag}.command.json").write_text(json.dumps({
    "command": command, "started_unix": started, "ended_unix": time.time(),
    "started_utc": datetime.datetime.fromtimestamp(started, datetime.UTC).isoformat(),
    "returncode": result.returncode}, indent=2) + "\n")
print(result.stdout[-2500:])
print(result.stderr[-1000:], file=sys.stderr)
raise SystemExit(result.returncode)

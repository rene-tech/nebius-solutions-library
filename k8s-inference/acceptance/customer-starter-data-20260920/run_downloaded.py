#!/usr/bin/env python3
"""Exercise the exact customer-downloaded runner without exposing its API key."""

import argparse
import json
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--key-file", type=Path, required=True)
parser.add_argument("--python", required=True)
parser.add_argument("--pack", type=Path, required=True)
parser.add_argument("--case", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
access = json.loads(args.key_file.read_bytes())
assert access["tenant_id"] in {
    "fs2-starter-data-acceptance-20260920",
    "fs2-starter-private-20260920",
}
environment = {
    **os.environ,
    "SCIENTIFIC_MODELS_API_KEY": access["secret"],
    "SCIENTIFIC_MODELS_MCP_URL": access["origin"] + "/mcp",
}
subprocess.run(
    [
        args.python,
        str(args.pack / "run-example.py"),
        "--root",
        str(args.pack),
        args.case,
        "--model",
        args.model,
        "--output",
        str(args.output),
    ],
    env=environment,
    check=True,
)

#!/usr/bin/env python3
"""Verify pinned file digests inside an existing task-owned preview pod."""

import argparse
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--kubeconfig", required=True)
parser.add_argument("--context", required=True)
parser.add_argument("--model", choices=["mindguard-4b", "mindguard-8b"], required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
manifest = json.loads((root / "artifacts" / (args.model + ".json")).read_text())
name = "fs2-mindguard-r20260916-" + args.model.removeprefix("mindguard-")
command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-models",
           "exec", "-i", "deployment/" + name, "-c", "vllm", "--", "python3", "-c",
           (root / "verify_public_cache.py").read_text(), f"/models/{args.model}/{manifest['revision']}"]
response = subprocess.run(command, input=json.dumps(manifest), check=True, capture_output=True, text=True)
result = json.loads(response.stdout)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))

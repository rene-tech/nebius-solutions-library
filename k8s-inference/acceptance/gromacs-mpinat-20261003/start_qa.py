"""One task-owned local baseline/candidate client; no customer credentials.

The existing system/qa API identity and provider credentials are read privately.
The shared client preparation script supplies a synthetic login and empty state.
Stop this container after all chats/execution jobs are terminal; retain evidence.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import re

IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:e96a66501807a2c446c17413f66c042b4a6ae2bdee2c32b9b05adcd1d378fd63"
QA_ID = "56130b22-ae09-42fc-a0f0-48012f22fb71"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--client-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--label", default="mpinat-20261003")
    parser.add_argument("--port", type=int, default=13203)
    parser.add_argument("--input-bindings", type=Path,
                        help="Explicit private dispatch plan for hash-pinned read-only local QA input files")
    args = parser.parse_args()
    if (not re.fullmatch(r"cr\.eu-north1\.nebius\.cloud/e00akg9ndpx77eaexh/lc@sha256:[a-f0-9]{64}", args.image)
            or not re.fullmatch(r"mpinat-[a-z0-9-]{1,60}", args.label)
            or not 13203 <= args.port <= 13209):
        raise ValueError("Use an exact regional client digest and a bounded task-owned local instance")
    os.umask(0o077)
    values = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    if not key.startswith("fs2_pat_" + QA_ID.replace("-", "")[:12]):
        raise ValueError("Only existing system/qa is permitted")
    with tempfile.TemporaryDirectory(prefix="mpinat-qa-key-") as private:
        path = Path(private) / "key.json"
        path.write_text(json.dumps({"secret": key}))
        subprocess.run([sys.executable, str(args.client_root / "scripts/qualification/prepare_default_release.py"),
                        "--root", str(args.output), "--source-env", str(args.qa_env),
                        "--platform-key", str(path), "--image", args.image,
                        "--label", args.label, "--port", str(args.port),
                        *(["--input-bindings", str(args.input_bindings)] if args.input_bindings else [])], check=True)
    print(json.dumps({"owner": "fs2-gromacs-mpinat-api-mcp-mpi-r20261003", "tenant": "system",
                      "principal": "qa", "image": args.image, "customer_state_used": False,
                      "cleanup": f"Stop fs2-default-release-{args.label} after terminal evidence; retain local state"}))


if __name__ == "__main__":
    main()

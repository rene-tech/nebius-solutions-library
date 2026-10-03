"""One task-owned local client on the exact Lynx image; no customer credentials.

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

IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:e96a66501807a2c446c17413f66c042b4a6ae2bdee2c32b9b05adcd1d378fd63"
QA_ID = "56130b22-ae09-42fc-a0f0-48012f22fb71"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--client-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
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
                        "--platform-key", str(path), "--image", IMAGE,
                        "--label", "mpinat-20261003", "--port", "13203"], check=True)
    print(json.dumps({"owner": "fs2-gromacs-mpinat-api-mcp-mpi-r20261003", "tenant": "system",
                      "principal": "qa", "image": IMAGE, "customer_state_used": False,
                      "cleanup": "Stop fs2-default-release-mpinat-20261003 after terminal evidence; retain local state"}))


if __name__ == "__main__":
    main()

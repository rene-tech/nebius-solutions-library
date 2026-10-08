"""Issue a reusable narrow shared-serving QA key through the lifecycle CLI.

No tenant, user or bucket is created. Bootstrap credential remains private and
exists in a temporary file only while the operator session is established.
"""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    assert not args.output.exists()
    secret = json.loads(subprocess.check_output([
        "kubectl", "--context", "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a",
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json",
    ]))
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, prefix="operator-", suffix=".token") as token:
        token.write(base64.b64decode(secret["data"]["token"]).decode())
        token.flush()
        command = [
            "python3", "/home/tux/.codex/skills/scientific-ai-tenant-lifecycle/scripts/fs2-users.py",
            "--base-url", "https://89.169.99.188", "--admin-token-file", token.name,
            "issue-key", "--tenant", "system", "--user", "qa",
            "--spec", str(Path(__file__).with_name("qa-key.json")), "--output", str(args.output),
        ]
        if args.apply:
            command.append("--apply")
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()

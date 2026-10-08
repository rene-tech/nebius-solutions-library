"""Issue a reusable narrow shared-serving QA key through the lifecycle CLI.

No tenant, user or bucket is created. Bootstrap credential remains private and
exists in a temporary file only while the operator session is established.
"""
import argparse
import base64
import json
import importlib.util
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--update-existing", action="store_true")
    args = parser.parse_args()
    assert args.output.exists() == args.update_existing
    secret = json.loads(subprocess.check_output([
        "kubectl", "--context", "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a",
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json",
    ]))
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, prefix="operator-", suffix=".token") as token:
        token.write(base64.b64decode(secret["data"]["token"]).decode())
        token.flush()
        if args.update_existing:
            module_spec = importlib.util.spec_from_file_location("lifecycle", "/home/tux/nebius-solutions-library-inference/k8s-inference/operations/tenant-lifecycle/lifecycle.py")
            lifecycle = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(lifecycle)
            client = lifecycle.AdminClient("https://89.169.99.188", token.name)
            try:
                saved = json.loads(args.output.read_text())
                token_id = saved["key"]["id"]
                keys = client.request("GET", "/admin/api/v1/keys?tenant_id=system")["items"]
                key = next(item for item in keys if item["id"] == token_id)
                assert (key["tenant_id"], key["principal_id"]) == ("system", "qa")
                desired = json.loads(Path(__file__).with_name("qa-key.json").read_text())
                if args.apply:
                    client.request("PATCH", f"/admin/api/v1/keys/{token_id}", {"scopes": desired["scopes"]})
                print(json.dumps({"updated_internal_key_scopes": args.apply}))
            finally:
                client.close()
            return
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

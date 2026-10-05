"""Temporarily extend only the existing internal QA key's named model grants."""

import argparse
import base64
import json
from pathlib import Path

import httpx

from remove_hot_floors import kubectl

TOKEN_ID = "56130b22-ae09-42fc-a0f0-48012f22fb71"
TEST_MODELS = ("scvi-scanvi", "cellpose-cpsam-v2", "sam2-1-hiera-large", "ace-step-1-5",
               "wan2-2-t2v-nim", "wan2-2-i2v-nim", "mindguard-4b", "mindguard-8b", "genmol")


def main(args):
    secret = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=120, trust_env=False) as client:
        client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}).raise_for_status()
        try:
            response = client.get("/admin/api/v1/keys", params={"tenant_id": "system"})
            response.raise_for_status()
            before = next(k for k in response.json()["data"]["items"] if k["id"] == TOKEN_ID)
            assert before["tenant_id"] == "system" and before["principal_id"] == "qa"
            assert before["max_concurrency"] == 2 and before["expires_at"] is None
            backup = args.directory / "qa-key-before.json"
            if args.restore:
                original = json.loads(backup.read_text())
                # Remove exactly task-added grants, preserving independent edits.
                added = set(TEST_MODELS) - set(original["models"])
                models = sorted(set(before["models"]) - added)
            else:
                if not backup.exists():
                    backup.write_text(json.dumps(before, indent=2) + "\n")
                models = sorted(set(before["models"]) | set(TEST_MODELS))
            result = client.patch("/admin/api/v1/keys/" + TOKEN_ID, json={"models": models})
            result.raise_for_status()
            after = result.json()["data"]
            assert after["max_concurrency"] == 2 and after["expires_at"] is None
            assert before["scopes"] == after["scopes"]
            (args.directory / ("qa-key-restored.json" if args.restore else "qa-key-expanded.json")).write_text(
                json.dumps(after, indent=2) + "\n")
            print(json.dumps({"key_id": TOKEN_ID, "restore": args.restore, "max_concurrency": 2,
                              "changed_model_grants": sorted(set(before["models"]) ^ set(after["models"]))}))
        finally:
            client.delete("/admin/api/v1/session")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--restore", action="store_true")
    main(parser.parse_args())

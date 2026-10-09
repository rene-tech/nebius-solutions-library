"""Restore established visual App tool identities after the corrected envelope rolls."""

import argparse
import base64
import copy
import json
from pathlib import Path

import httpx

from remove_hot_floors import kubectl

TOOLS = {"scvi-scanvi": "integrate_single_cell", "cellpose-cpsam-v2": "segment_cells",
         "sam2-1-hiera-large": "segment_track_media"}


def main(args):
    secret = json.loads(kubectl(args.context, "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    receipt = []
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=120, trust_env=False) as client:
        client.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token}).raise_for_status()
        try:
            for model, tool in TOOLS.items():
                current_response = client.get("/admin/api/v1/model-deployments/" + model)
                current_response.raise_for_status()
                current = current_response.json()["data"]
                spec = copy.deepcopy(current["spec"])
                spec["exposure"]["mcpToolName"] = tool
                proposal = {"name": model, "namespace": current["namespace"], "base_etag": current["etag"], "spec": spec}
                response = client.post("/admin/api/v1/model-deployments:plan-preview", json=proposal)
                response.raise_for_status()
                preview = response.json()["data"]
                if preview["decision"]["disposition"] != "accepted":
                    raise RuntimeError(str(preview["decision"]))
                response = client.post("/admin/api/v1/model-deployments:apply", json={
                    "preview_id": preview["preview_id"], "proposed_etag": preview["proposed_etag"],
                    "proposal": proposal, "idempotency_key": "idle-original-tool-20261005-" + model})
                response.raise_for_status()
                receipt.append({"model": model, "before": current, "after": response.json()})
                print(json.dumps({"model": model, "restored_original_tool_base": tool}), flush=True)
        finally:
            client.delete("/admin/api/v1/session")
            (args.directory / "tool-names-restored.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    main(parser.parse_args())

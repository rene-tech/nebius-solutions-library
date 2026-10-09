"""Apply only voice App proposals or adjust their existing min floor (1..2).

Uses the ordinary preview/ETag/apply admin API. No Kubernetes writes, cloud
changes, credentials in output, unrelated Apps, or snapshot flags are allowed.
"""

import argparse
import base64
import copy
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx

IDS = {
    "parakeet-realtime-eou-120m-v1",
    "magpie-tts-multilingual-357m",
    "diar-streaming-sortformer-4spk-v2-1",
}


def main(args):
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    raw = json.loads(
        subprocess.check_output(
            kubectl
            + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
        )
    )
    token = base64.b64decode(raw["data"]["token"]).decode().strip()
    receipt = {"mode": args.mode, "results": []}
    with httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        verify=False,
        timeout=120,
        trust_env=False,
    ) as admin:
        try:
            admin.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            ).raise_for_status()
            if args.mode == "apply":
                proposals = json.loads(Path(args.proposals).read_text())
                if {p["name"] for p in proposals} != IDS or len(proposals) != 3:
                    raise ValueError("only exact three voice proposals are allowed")
                for proposal in proposals:
                    existing = admin.get(
                        "/admin/api/v1/model-deployments/" + proposal["name"]
                    )
                    if existing.status_code != 404:
                        raise ValueError(
                            "App already exists or read failed; inspect rather than overwrite"
                        )
            else:
                if args.model not in IDS or args.min_replicas not in (1, 2):
                    raise ValueError("only a voice App floor of 1 or 2 is allowed")
                response = admin.get("/admin/api/v1/model-deployments/" + args.model)
                response.raise_for_status()
                value = response.json()["data"]
                spec = copy.deepcopy(value["spec"])
                spec["availability"].update(
                    minReplicas=args.min_replicas, maxReplicas=2
                )
                proposals = [
                    {
                        "name": args.model,
                        "namespace": value["namespace"],
                        "base_etag": value["etag"],
                        "spec": spec,
                    }
                ]
                receipt["previous_availability"] = value["spec"]["availability"]
            for proposal in proposals:
                spec = proposal["spec"]
                if (
                    proposal["namespace"] != "fs2-models"
                    or proposal["name"] not in IDS
                    or spec["modelRef"] != proposal["name"]
                    or spec["cache"]["snapshotPreference"] != "Never"
                    or spec["placement"]["poolRefs"] != ["l40s-1x"]
                    or spec["policy"]["allowedPrincipalIds"] != []
                ):
                    raise ValueError("proposal escapes approved voice scope")
                row = {"model": proposal["name"], "proposal": proposal}
                receipt["results"].append(row)
                response = admin.post(
                    "/admin/api/v1/model-deployments:plan-preview", json=proposal
                )
                response.raise_for_status()
                preview = response.json()["data"]
                row["preview"] = preview
                if preview["decision"]["disposition"] != "accepted":
                    raise ValueError("voice App preview rejected")
                response = admin.post(
                    "/admin/api/v1/model-deployments:apply",
                    json={
                        "preview_id": preview["preview_id"],
                        "proposed_etag": preview["proposed_etag"],
                        "proposal": proposal,
                        "idempotency_key": "voice-" + uuid4().hex,
                    },
                )
                row["apply_status"] = response.status_code
                row["apply"] = response.json()
                response.raise_for_status()
                print(
                    json.dumps(
                        {
                            "model": proposal["name"],
                            "status": response.status_code,
                            "min_replicas": spec["availability"]["minReplicas"],
                        }
                    ),
                    flush=True,
                )
            receipt["status"] = "applied"
        finally:
            admin.delete("/admin/api/v1/session")
            Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "origin", "output"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--mode", choices=("apply", "scale"), required=True)
    parser.add_argument("--proposals")
    parser.add_argument("--model", choices=sorted(IDS))
    parser.add_argument("--min-replicas", type=int, choices=(1, 2))
    main(parser.parse_args())

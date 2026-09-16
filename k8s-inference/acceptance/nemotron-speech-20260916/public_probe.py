"""Apply the two test Apps through admin API, or exercise ordinary customer files.

The public test uses a short-lived Rene key, revoked in finally. No credentials
or signed URLs enter argv/evidence. Full medical recordings are losslessly FLAC
encoded only to fit the compatibility upload ceiling; no samples are trimmed.
This is an integration cohort, not complete scaling/option/resilience acceptance.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

IDS = ("nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", choices=("apply-apps", "repair-app-policy", "files"), required=True)
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    raw = json.loads(subprocess.check_output(command + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]))
    token = base64.b64decode(raw["data"]["token"]).decode().strip()
    receipt = {"started_at": datetime.now(UTC).isoformat(), "scope": args.mode,
               "complete_customer_acceptance": False, "measurements": []}
    key_id = None
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False) as admin:
        try:
            response = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + token})
            response.raise_for_status()
            if args.mode in {"apply-apps", "repair-app-policy"}:
                proposals = []
                if args.mode == "repair-app-policy":
                    for name in IDS:
                        current = admin.get("/admin/api/v1/model-deployments/" + name)
                        current.raise_for_status()
                        value = current.json()["data"]
                        if value["spec"]["policy"]["allowedPrincipalIds"] not in ([], ["rene"]):
                            raise ValueError("speech policy changed outside this task")
                        value["spec"]["policy"]["allowedPrincipalIds"] = []
                        proposals.append({"name": name, "namespace": value["namespace"],
                                          "base_etag": value["etag"], "spec": value["spec"]})
                else:
                    proposals = json.loads(args.proposals.read_text())
                for proposal in proposals:
                    if proposal["name"] not in IDS or proposal["spec"]["policy"]["allowedPrincipalIds"] != []:
                        raise ValueError("not a task-scoped speech App proposal")
                    response = admin.post("/admin/api/v1/model-deployments:plan-preview", json=proposal)
                    response.raise_for_status()
                    preview = response.json()["data"]
                    receipt["measurements"].append({"model": proposal["name"], "preview": preview})
                    if preview["decision"]["disposition"] != "accepted":
                        raise RuntimeError("speech_app_preview_rejected")
                    response = admin.post("/admin/api/v1/model-deployments:apply", json={
                        "preview_id": preview["preview_id"], "proposed_etag": preview["proposed_etag"],
                        "proposal": proposal, "idempotency_key": "speech-" + hashlib.sha256(
                            (proposal["name"] + preview["proposed_etag"]).encode()).hexdigest(),
                    })
                    receipt["measurements"][-1]["apply_http_status"] = response.status_code
                    receipt["measurements"][-1]["apply"] = response.json()
                    response.raise_for_status()
                    print(json.dumps({"model": proposal["name"], "app_applied": True}), flush=True)
                return
            response = admin.post("/admin/api/v1/keys", json={
                "name": "speech-acceptance-" + uuid4().hex[:10], "tenant_id": "rene", "principal_id": "rene",
                "models": list(IDS), "scopes": ["catalog.read", "inference.invoke", "mcp.invoke", "operations.read",
                    "operations.result", "artifacts.write", "operations.cancel", "use.nonclinical"],
                "max_concurrency": 2, "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            })
            response.raise_for_status()
            disclosure = response.json()["data"]
            key_id, key = disclosure["key"]["id"], disclosure["secret"]
            receipt["temporary_key_id"] = key_id
            cases = [
                (IDS[0], "en-01", "ready/en/day1_consultation01_conversation.wav", "en"),
                (IDS[0], "en-02", "ready/en/day1_consultation02_conversation.wav", "en"),
                (IDS[1], "de-herzrasen", "ready/de/hhu-herzrasen.wav", "de"),
                (IDS[1], "de-grippaler-infekt", "ready/de/hhu-grippaler-infekt.wav", "de"),
                (IDS[1], "de-polyarthritis", "ready/de/hhu-polyarthritis.wav", "de"),
            ]
            with httpx.Client(base_url=args.origin, headers={"authorization": "Bearer " + key}, timeout=60, trust_env=False) as client:
                for model, case, relative, language in cases:
                    source = args.assets / relative
                    with tempfile.TemporaryDirectory(prefix="fs2-speech-public-") as directory:
                        path = Path(directory) / "recording.flac"
                        subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source),
                                        "-c:a", "flac", str(path)], check=True, timeout=90)
                        if path.stat().st_size > 8 * 1024 * 1024:
                            raise ValueError("fixture_exceeds_compatibility_limit_use_artifact_api")
                        started = time.monotonic()
                        with path.open("rb") as handle:
                            response = client.post("/v1/audio/transcriptions",
                                data={"model": model, "language": language, "response_format": "verbose_json"},
                                files={"file": ("recording.flac", handle, "audio/flac")},
                                headers={"idempotency-key": "speech-public-" + uuid4().hex})
                        row = {"model": model, "case": case, "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                               "transport_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "transport_bytes": path.stat().st_size, "submit_status": response.status_code,
                               "operation_id": response.headers.get("x-fs2-operation-id")}
                        receipt["measurements"].append(row)
                        if response.status_code == 202:
                            operation_id = response.json()["id"] if "id" in response.json() else response.json()["operation_id"]
                            row["operation_id"] = operation_id
                            deadline = time.monotonic() + 900
                            while time.monotonic() < deadline:
                                poll = client.get("/v1/operations/" + operation_id)
                                poll.raise_for_status()
                                operation = poll.json()
                                if operation["status"] == "succeeded":
                                    response = client.get("/v1/operations/" + operation_id + "/result")
                                    break
                                if operation["status"] in {"failed", "cancelled", "expired", "preempted"}:
                                    row["operation"] = operation
                                    raise RuntimeError("public_speech_operation_failed")
                                time.sleep(2)
                            else:
                                raise RuntimeError("public_speech_operation_timeout")
                        row["wall_seconds"] = time.monotonic()-started
                        row["final_status"] = response.status_code
                        if "json" in response.headers.get("content-type", ""):
                            row["result"] = response.json()
                        else:
                            row["result"] = {"error": "non_json_response", "body": response.text[:1000]}
                        response.raise_for_status()
                        if not row["result"].get("text", "").strip():
                            raise RuntimeError("public_speech_empty_transcript")
                        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
                        print(json.dumps({"case": case, "model": model, "status": "completed",
                                          "wall_seconds": row["wall_seconds"], "operation_id": row["operation_id"]}), flush=True)
        finally:
            if key_id:
                revoke = admin.delete("/admin/api/v1/keys/" + key_id)
                receipt["temporary_key_revoked"] = revoke.status_code == 200
            receipt["finished_at"] = datetime.now(UTC).isoformat()
            args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
            admin.delete("/admin/api/v1/session")


if __name__ == "__main__":
    main()

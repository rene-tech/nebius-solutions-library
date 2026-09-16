"""Apply the two test Apps through admin API, or exercise ordinary customer files.

The public test uses a short-lived Rene key, revoked in finally. No credentials
or signed URLs enter argv/evidence. Full medical recordings are losslessly FLAC
encoded only to fit the compatibility upload ceiling; no samples are trimmed.
This is an integration cohort, not complete scaling/option/resilience acceptance.
"""

import argparse
import asyncio
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


def submit_file(client, path, model, language, row):
    """Keep full audio intact; use durable artifacts above compatibility size."""
    key = "speech-public-" + uuid4().hex
    if path.stat().st_size <= 8 * 1024 * 1024:
        row["transport"] = "multipart"
        with path.open("rb") as handle:
            return client.post("/v1/audio/transcriptions",
                data={"model": model, "language": language, "response_format": "verbose_json"},
                files={"file": ("recording.flac", handle, "audio/flac")},
                headers={"idempotency-key": key})
    row["transport"] = "artifact-native-async"
    artifact = upload_artifact(client, path, model, row, key)
    return client.post("/v1/models/" + model + ":invoke", json={
        "operation": "transcribe", "payload": {"audio": artifact,
            "options": {"model": model.replace("0-6b", "0.6b"), "language": language}},
    }, headers={"idempotency-key": key, "x-fs2-wait-seconds": "0"})


def upload_artifact(client, path, model, row, key):
    """Stage a complete FLAC for either HTTP or typed MCP invocation."""
    response = client.post("/v1/scientific-artifacts/uploads", json={
        "model_id": model, "sha256": row["transport_sha256"],
        "size_bytes": row["transport_bytes"], "media_type": "audio/flac", "compression": "none",
    }, headers={"idempotency-key": key + "-upload"})
    response.raise_for_status()
    upload = response.json()
    row["upload_operation_id"] = upload["operation_id"]
    if path.stat().st_size <= upload["max_content_bytes"]:
        with path.open("rb") as handle:
            response = client.put(upload["content_path"], content=handle,
                headers={"content-type": "audio/flac", "content-length": str(path.stat().st_size)})
    else:
        # Separate client: NEVER send the platform bearer token to object storage.
        # Neither the signed URL nor its exception text belongs in receipts.
        try:
            with httpx.Client(timeout=180, trust_env=False) as storage, path.open("rb") as handle:
                response = storage.put(upload["handle"]["url"], content=handle,
                                       headers=upload["handle"]["headers"])
        except httpx.HTTPError:
            raise RuntimeError("direct_audio_upload_transport_failed") from None
        if response.status_code >= 400:
            raise RuntimeError(f"direct_audio_upload_failed_http_{response.status_code}")
    response.raise_for_status()
    response = client.post("/v1/scientific-artifacts/uploads/" + upload["upload_id"] + ":finalize",
                           json={"operation_id": upload["operation_id"]})
    response.raise_for_status()
    artifact = response.json()
    row["artifact_id"] = artifact["artifact_id"]
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "origin"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--mode", choices=("apply-apps", "repair-app-policy", "drain-apps", "update-templates", "files", "live", "mcp"), required=True)
    parser.add_argument("--paced", action="store_true", help="Replay live audio at original recording speed")
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--template-refs", type=Path)
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
            if args.mode == "drain-apps":
                for name in IDS:
                    response = admin.get("/admin/api/v1/model-deployments/" + name)
                    response.raise_for_status()
                    value = response.json()["data"]
                    response = admin.post("/admin/api/v1/model-deployments/" + name + ":drain", json={
                        "base_etag": value["etag"], "idempotency_key": "speech-scratch-drain-" + uuid4().hex})
                    receipt["measurements"].append({"model": name, "before": value, "drain": response.json()})
                    response.raise_for_status()
                    print(json.dumps({"model": name, "drain_requested": True}), flush=True)
                return
            if args.mode in {"apply-apps", "repair-app-policy", "update-templates"}:
                proposals = []
                if args.mode in {"repair-app-policy", "update-templates"}:
                    references = json.loads(args.template_refs.read_text()) if args.mode == "update-templates" else {}
                    for name in IDS:
                        current = admin.get("/admin/api/v1/model-deployments/" + name)
                        current.raise_for_status()
                        value = current.json()["data"]
                        if value["spec"]["policy"]["allowedPrincipalIds"] not in ([], ["rene"]):
                            raise ValueError("speech policy changed outside this task")
                        value["spec"]["policy"]["allowedPrincipalIds"] = []
                        if args.mode == "update-templates":
                            if value["spec"]["lifecycle"]["desiredState"] != "Draining":
                                raise ValueError("drain this task App before changing runtime material")
                            value["spec"]["runtime"]["templateRef"] = references[name]
                            value["spec"]["lifecycle"]["desiredState"] = "Enabled"
                            value["spec"]["availability"]["minReplicas"] = 1
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
            if args.mode == "live":
                from public_live import run_cohort
                asyncio.run(run_cohort(args.origin, key, args.assets, receipt, paced=args.paced))
                return
            if args.mode == "mcp":
                from public_mcp import run_cohort
                asyncio.run(run_cohort(args.origin, key, args.assets, receipt))
                return
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
                        started = time.monotonic()
                        row = {"model": model, "case": case, "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                               "transport_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "transport_bytes": path.stat().st_size}
                        receipt["measurements"].append(row)
                        response = submit_file(client, path, model, language, row)
                        row.update(submit_status=response.status_code,
                                   operation_id=response.headers.get("x-fs2-operation-id"))
                        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
                        if response.status_code == 202:
                            operation_id = response.json()["id"] if "id" in response.json() else response.json()["operation_id"]
                            row["operation_id"] = operation_id
                            deadline = time.monotonic() + 900
                            while time.monotonic() < deadline:
                                poll = client.get("/v1/operations/" + operation_id)
                                poll.raise_for_status()
                                operation = poll.json()
                                history = row.setdefault("poll_history", [])
                                if not history or history[-1]["status"] != operation["status"]:
                                    history.append({"status": operation["status"], "elapsed_seconds": time.monotonic()-started})
                                    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
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

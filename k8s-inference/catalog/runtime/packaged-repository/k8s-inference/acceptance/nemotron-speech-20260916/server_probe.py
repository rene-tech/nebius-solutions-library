"""Exercise actual private HTTP/live workers using full user-supplied recordings.

Task-owned inputs temporarily use Rene's existing bucket. Credentials/presigned
URLs stay in memory/stdin, never argv, output or receipts. No public gateway
qualification is implied; this also tests the restored process serving audio.
"""

import argparse
import base64
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import boto3
import httpx

import native_fixtures

DRIVER = r"""
import asyncio, json, sys, tempfile, time, wave
from pathlib import Path
import httpx
from websockets.asyncio.client import connect

async def main():
    request = json.load(sys.stdin)
    origin = "http://127.0.0.1:8000"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        ready = await client.get(origin + "/readyz")
        if ready.status_code != 200:
            raise RuntimeError("worker_not_ready")
        response = await client.post(origin + "/generate", json=request)
        if response.status_code != 200:
            raise RuntimeError("file_transcription_http_" + str(response.status_code))
        result = response.json()
        print(json.dumps({"mode":"http-file", "wall_seconds":time.monotonic()-started,
                          "ready":ready.json(), "result":result}), flush=True)
        if not result["text"].strip():
            raise RuntimeError("empty_consultation_transcript")
        with tempfile.TemporaryDirectory(prefix="fs2-speech-acceptance-") as directory:
            path = Path(directory) / "input.wav"
            async with client.stream("GET", request["audio"]["url"]) as download:
                if download.status_code != 200:
                    raise RuntimeError("fixture_download_failed")
                with path.open("wb") as handle:
                    async for chunk in download.aiter_bytes(65536):
                        handle.write(chunk)
            async with connect("ws://127.0.0.1:8000/v1/audio/stream", proxy=None,
                               max_size=1024*1024, max_queue=2) as socket:
                await socket.send(json.dumps({"type":"session.start", "options":request["options"]}))
                first = json.loads(await socket.recv())
                if first["type"] != "session.ready":
                    raise RuntimeError("live_session_not_ready")
                started = time.monotonic()
                async def upload():
                    with wave.open(str(path), "rb") as audio:
                        while chunk := audio.readframes(1600):
                            await socket.send(chunk)
                            await asyncio.sleep(0)
                    await socket.send('{"type":"input.finish"}')
                task = asyncio.create_task(upload())
                finals, state = [], {}
                try:
                    async for message in socket:
                        event = json.loads(message)
                        if event["type"] == "transcript.partial" and event.get("text"):
                            state.setdefault("first_partial_seconds", time.monotonic()-started)
                        elif event["type"] == "transcript.final":
                            finals.append(event)
                        elif event["type"] == "session.completed":
                            state["completed"] = event
                            break
                        elif event["type"] == "session.error":
                            raise RuntimeError("live_transcription_failed")
                    await task
                    if "completed" not in state:
                        raise RuntimeError("live_transcription_incomplete")
                    text = "".join(item["text"] for item in finals).strip()
                    print(json.dumps({"mode":"websocket-unpaced", "wall_seconds":time.monotonic()-started,
                                      "first_partial_seconds":state.get("first_partial_seconds"),
                                      "result":{"text":text,"segments":finals,**state["completed"]},
                                      "matches_file_text":text==result["text"]}), flush=True)
                    if text != result["text"] or state["completed"]["audio_seconds"] != result["audio_seconds"]:
                        raise RuntimeError("file_live_result_mismatch")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

asyncio.run(main())
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("kubeconfig", "context", "origin", "pod", "model"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Native catalog fixtures; not medical acceptance",
    )
    args = parser.parse_args()
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    pod = json.loads(
        subprocess.check_output(
            kubectl + ["-n", "fs2-models", "get", "pod", args.pod, "-o", "json"]
        )
    )
    if (
        pod["metadata"]["labels"].get("workload.fs2.nebius/owner")
        != "nemotron-speech-20260916"
    ):
        raise ValueError("not a task-owned speech worker")
    raw = json.loads(
        subprocess.check_output(
            kubectl
            + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
        )
    )
    token = base64.b64decode(raw["data"]["token"]).decode().strip()
    receipt = {
        "scope": "private resident worker HTTP/WebSocket; not public gateway acceptance",
        "started_at": datetime.now(UTC).isoformat(),
        "pod": args.pod,
        "pod_uid": pod["metadata"]["uid"],
        "image": pod["spec"]["containers"][0]["image"],
        "model": args.model,
        "measurements": [],
    }
    objects, s3 = [], None
    with httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        timeout=60,
        trust_env=False,
    ) as admin:
        try:
            if (
                admin.post(
                    "/admin/api/v1/session",
                    headers={"authorization": "Bearer " + token},
                ).status_code
                != 200
            ):
                raise RuntimeError("admin_login_failed")
            users = admin.get("/admin/api/v1/users?tenant_id=rene").json()["data"][
                "items"
            ]
            user = next(user for user in users if user["principal_id"] == "rene")
            response = admin.post(
                f"/admin/api/v1/users/{user['id']}/storage/credentials"
            )
            if response.status_code != 200:
                raise RuntimeError("existing_test_storage_unavailable")
            credentials = response.json()["data"]
            s3 = boto3.client(
                "s3",
                endpoint_url=credentials["endpoint"],
                region_name=credentials["region"],
                aws_access_key_id=credentials["access_key_id"],
                aws_secret_access_key=credentials["secret_access_key"],
            )
            bucket = credentials["bucket_name"]
            second = (
                ("de-herzrasen", "ready/de/hhu-herzrasen.wav", "de-DE")
                if "multilingual" in args.model
                else ("en-02", "ready/en/day1_consultation02_conversation.wav", "en-US")
            )
            cases = [
                ("en-01", "ready/en/day1_consultation01_conversation.wav", "en-US"),
                second,
            ]
            if args.synthetic:
                # Generate with the exact serving image's speech synthesizer,
                # not an unpinned optional binary on the development host.
                script = (
                    Path(native_fixtures.__file__).read_text()
                    + "\n"
                    + (
                        "import base64,json,tempfile\n"
                        "with tempfile.TemporaryDirectory() as d:\n"
                        "    p=Path(d)\n"
                        "    rows=prepare(p)\n"
                        "    print(json.dumps([[i,f,l,base64.b64encode((p/f).read_bytes()).decode()] for i,f,l in rows]))\n"
                    )
                )
                encoded = subprocess.check_output(
                    kubectl
                    + [
                        "-n",
                        "fs2-models",
                        "exec",
                        args.pod,
                        "-c",
                        "speech",
                        "--",
                        "/opt/conda/bin/python",
                        "-c",
                        script,
                    ],
                    timeout=60,
                )
                cases = []
                args.assets.mkdir(parents=True, exist_ok=True)
                for identity, filename, language, content in json.loads(encoded):
                    (args.assets / filename).write_bytes(base64.b64decode(content))
                    cases.append((identity, filename, language))
            for case, relative, locale in cases:
                path = args.assets / relative
                with path.open("rb") as handle:
                    sha = hashlib.file_digest(handle, "sha256").hexdigest()
                key = "acceptance/nemotron-speech-20260916/" + str(uuid4()) + ".wav"
                objects.append(key)
                s3.upload_file(
                    str(path), bucket, key, ExtraArgs={"ContentType": "audio/wav"}
                )
                url = s3.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=1800
                )
                payload = {
                    "audio": {
                        "url": url,
                        "sha256": sha,
                        "size_bytes": path.stat().st_size,
                        "media_type": "audio/wav",
                    },
                    "options": {
                        "model": args.model,
                        "language": locale,
                        "output_granularity": "word",
                    },
                }
                canonical = httpx.Request(
                    "POST", "http://worker/generate", json=payload
                ).content
                payload_sha256 = hashlib.sha256(canonical).hexdigest()
                started = datetime.now(UTC).isoformat()
                process = subprocess.run(
                    kubectl
                    + [
                        "-n",
                        "fs2-models",
                        "exec",
                        "-i",
                        args.pod,
                        "-c",
                        "speech",
                        "--",
                        "/opt/conda/bin/python",
                        "-c",
                        DRIVER,
                    ],
                    input=json.dumps(payload).encode(),
                    capture_output=True,
                    timeout=900,
                )
                # Do not dump stderr/argv or signed input on failure.
                if process.returncode:
                    receipt["error"] = {
                        "case": case,
                        "code": "private_worker_probe_failed",
                        "returncode": process.returncode,
                        "detail": process.stderr.decode(errors="replace").replace(
                            url, "[signed-artifact]"
                        ),
                    }
                    raise RuntimeError(
                        "private_worker_probe_failed; inspect task-owned worker logs"
                    )
                rows = [
                    json.loads(line)
                    for line in process.stdout.decode().splitlines()
                    if line.startswith("{")
                ]
                if len(rows) != 2:
                    raise RuntimeError("incomplete_probe_receipt")
                for row in rows:
                    receipt["measurements"].append(
                        {
                            "case": case,
                            "input_sha256": sha,
                            "request_payload_sha256": payload_sha256,
                            "started_at": started,
                            **row,
                        }
                    )
                args.output.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
                )
                print(
                    json.dumps(
                        {"case": case, "model": args.model, "http_and_live": "passed"}
                    ),
                    flush=True,
                )
            receipt["status"] = "passed"
        finally:
            if s3 is not None:
                for key in objects:
                    s3.delete_object(Bucket=bucket, Key=key)
            admin.delete("/admin/api/v1/session")
            receipt["test_objects_deleted"] = len(objects)
            args.output.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
            )


if __name__ == "__main__":
    main()

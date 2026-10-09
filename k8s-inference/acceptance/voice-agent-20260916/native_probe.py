"""Qualify two native requests/model with synthetic fixtures and temporary storage.

Credentials and signed URLs stay in memory. Only hashes, safe model results and
metrics are retained. Temporary fixture objects are deleted after the probe.
"""

import argparse
import base64
import hashlib
import io
import json
import subprocess
import time
import wave
from pathlib import Path
from uuid import uuid4

import boto3
import httpx

MODELS = {
    "magpie": "magpie-tts-multilingual-357m",
    "parakeet": "parakeet-realtime-eou-120m-v1",
    "sortformer": "diar-streaming-sortformer-4spk-v2-1",
}
FIXTURES = [
    {
        "text": "Please meet us at the observatory. The final word is telescope.",
        "language": "en",
        "voice": "Sofia",
    },
    {
        "text": "This workshop tests a different voice. The final word is microscope.",
        "language": "en",
        "voice": "Jason",
    },
]


def main(args):
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context]
    secret = json.loads(
        subprocess.check_output(
            kubectl
            + ["-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]
        )
    )
    token = base64.b64decode(secret["data"]["token"]).decode().strip()
    receipt = {
        "scope": "private native workers, synthetic fixtures only",
        "models": {},
        "objects_deleted": 0,
    }
    objects, s3 = [], None
    with httpx.Client(
        base_url=args.origin,
        headers={"origin": args.origin},
        verify=False,
        timeout=240,
        trust_env=False,
    ) as admin:
        try:
            login = admin.post(
                "/admin/api/v1/session", headers={"authorization": "Bearer " + token}
            )
            login.raise_for_status()
            users = admin.get("/admin/api/v1/users?tenant_id=rene").json()["data"][
                "items"
            ]
            user = next(row for row in users if row["principal_id"] == "rene")
            result = admin.post(f"/admin/api/v1/users/{user['id']}/storage/credentials")
            result.raise_for_status()
            credentials = result.json()["data"]
            s3 = boto3.client(
                "s3",
                endpoint_url=credentials["endpoint"],
                region_name=credentials["region"],
                aws_access_key_id=credentials["access_key_id"],
                aws_secret_access_key=credentials["secret_access_key"],
            )
            bucket = credentials["bucket_name"]
            with httpx.Client(timeout=240, trust_env=False) as client:
                for index, fixture in enumerate(FIXTURES):
                    payload = {
                        "model": MODELS["magpie"],
                        **fixture,
                        "apply_text_normalization": False,
                    }
                    content = httpx.Request(
                        "POST", "http://worker/generate", json=payload
                    ).content
                    start = time.monotonic()
                    response = client.post(
                        args.magpie + "/generate",
                        content=content,
                        headers={"content-type": "application/json"},
                    )
                    response.raise_for_status()
                    wav = response.content
                    with wave.open(io.BytesIO(wav)) as audio:
                        assert (
                            audio.getnframes() > 100 and audio.getframerate() == 22050
                        )
                        duration = audio.getnframes() / 22050
                    (root / f"native-{index}.wav").write_bytes(wav)
                    receipt["models"].setdefault(MODELS["magpie"], []).append(
                        {
                            "id": f"voice-native-{index}",
                            "request_payload_sha256": hashlib.sha256(
                                content
                            ).hexdigest(),
                            "response_sha256": hashlib.sha256(wav).hexdigest(),
                            "audio_seconds": duration,
                            "elapsed_seconds": time.monotonic() - start,
                        }
                    )
                    key = "acceptance/voice-agent-20260916/" + str(uuid4()) + ".wav"
                    objects.append(key)
                    s3.put_object(
                        Bucket=bucket, Key=key, Body=wav, ContentType="audio/wav"
                    )
                    url = s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket, "Key": key},
                        ExpiresIn=1800,
                    )
                    payload = {
                        "audio": {
                            "url": url,
                            "sha256": hashlib.sha256(wav).hexdigest(),
                            "size_bytes": len(wav),
                            "media_type": "audio/wav",
                        }
                    }
                    content = httpx.Request(
                        "POST", "http://worker/generate", json=payload
                    ).content
                    for short in ("parakeet", "sortformer"):
                        start = time.monotonic()
                        response = client.post(
                            getattr(args, short) + "/generate",
                            content=content,
                            headers={"content-type": "application/json"},
                        )
                        if response.status_code != 200:
                            raise RuntimeError(
                                short + "_native_http_" + str(response.status_code)
                            )
                        result = response.json()
                        assert result["events"]
                        if short == "parakeet":
                            assert result["text"].strip()
                        receipt["models"].setdefault(MODELS[short], []).append(
                            {
                                "id": f"voice-native-{index}",
                                "request_payload_sha256": hashlib.sha256(
                                    content
                                ).hexdigest(),
                                "input_sha256": payload["audio"]["sha256"],
                                "response_sha256": hashlib.sha256(
                                    response.content
                                ).hexdigest(),
                                "result": result,
                                "elapsed_seconds": time.monotonic() - start,
                            }
                        )
                    (root / "native-results.json").write_text(
                        json.dumps(receipt, indent=2) + "\n"
                    )
            receipt["status"] = "passed"
        finally:
            if s3 is not None:
                for key in objects:
                    s3.delete_object(Bucket=bucket, Key=key)
                    receipt["objects_deleted"] += 1
            admin.delete("/admin/api/v1/session")
            (root / "native-results.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
    print(
        json.dumps(
            {
                "status": receipt.get("status"),
                "objects_deleted": receipt["objects_deleted"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "kubeconfig",
        "context",
        "origin",
        "magpie",
        "parakeet",
        "sortformer",
        "output",
    ):
        parser.add_argument("--" + field, required=True)
    main(parser.parse_args())

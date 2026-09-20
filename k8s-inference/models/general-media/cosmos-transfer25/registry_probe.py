"""Read-only, credential-suppressed NGC image preflight run in the target cluster."""

import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import httpx


REPOSITORY = "nim/nvidia/cosmos-transfer2.5-2b"
TAG = "1.1"
ORIGIN = "https://nvcr.io"
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class ProbeError(Exception):
    def __init__(self, phase, status=None):
        self.phase = phase
        self.status = status


def require_response(response, phase):
    if response.status_code != 200:
        raise ProbeError(phase, response.status_code)
    if len(response.content) > 2 * 1024 * 1024:
        raise ProbeError(phase + "-response-too-large")
    return response


def digest_bytes(response, expected=None):
    digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
    if expected is not None and digest != expected:
        raise ProbeError("content-digest-mismatch")
    header = response.headers.get("docker-content-digest")
    if header and header != digest:
        raise ProbeError("registry-digest-mismatch")
    return digest


def probe(client, key):
    path = f"{ORIGIN}/v2/{REPOSITORY}/manifests/{TAG}"
    response = client.get(path, headers={"Accept": ACCEPT})
    auth = {}
    if response.status_code == 401:
        challenge = response.headers.get("www-authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise ProbeError("unexpected-authentication-challenge")
        fields = dict(re.findall(r'(\w+)="([^"]+)"', challenge))
        realm = fields.get("realm", "")
        parsed = urlsplit(realm)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "nvcr.io"
            or parsed.path not in ("/proxy_auth", "/token")
            or parsed.query
            or parsed.fragment
            or fields.get("service") not in ("registry", "nvcr.io")
        ):
            raise ProbeError("untrusted-authentication-realm")
        token_response = require_response(
            client.get(
                realm,
                params={"service": fields["service"], "scope": f"repository:{REPOSITORY}:pull"},
                auth=httpx.BasicAuth("$oauthtoken", key),
            ),
            "ngc-token",
        )
        payload = token_response.json()
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ProbeError("missing-registry-token")
        auth = {"Authorization": "Bearer " + token}
        response = client.get(path, headers={"Accept": ACCEPT, **auth})
    require_response(response, "image-manifest")
    index_digest = digest_bytes(response)
    document = response.json()
    if "manifests" in document:
        candidates = [
            row
            for row in document["manifests"]
            if row.get("platform", {}).get("os") == "linux"
            and row.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(candidates) != 1:
            raise ProbeError("ambiguous-linux-amd64-manifest")
        child_digest = candidates[0]["digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", child_digest):
            raise ProbeError("invalid-child-digest")
        response = require_response(
            client.get(
                f"{ORIGIN}/v2/{REPOSITORY}/manifests/{child_digest}",
                headers={"Accept": ACCEPT, **auth},
            ),
            "platform-manifest",
        )
        digest_bytes(response, child_digest)
        document = response.json()
    else:
        child_digest = index_digest
    return {
        "repository": REPOSITORY,
        "tag": TAG,
        "index_digest": index_digest,
        "linux_amd64_manifest_digest": child_digest,
        "config_digest": document["config"]["digest"],
        "compressed_layer_bytes": sum(row["size"] for row in document["layers"]),
        "layer_count": len(document["layers"]),
        "authenticated_manifest_access": bool(auth),
        "model_artifact_access_tested": False,
    }


def main():
    try:
        key = os.environ.get("NGC_API_KEY", "").strip()
        if not key:
            raise ProbeError("missing-runtime-secret")
        with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
            receipt = probe(client, key)
        print(json.dumps({"status": "passed", **receipt}), flush=True)
        return 0
    except ProbeError as error:
        print(json.dumps({"status": "failed", "phase": error.phase, "http_status": error.status}), flush=True)
    except Exception as error:
        # Exception text, response bodies and request headers may contain secrets.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

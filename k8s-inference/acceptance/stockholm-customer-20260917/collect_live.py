#!/usr/bin/env python3
"""Read-only Stockholm deployment/team-policy collector. Never issues a key."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POLICY_FIELDS = (
    "tenant_id", "models", "scopes", "max_concurrency", "request_budget",
    "gpu_seconds_budget", "rate_limit_requests", "rate_window_seconds",
)
REQUIRED_SCOPES = {
    "artifacts.write", "catalog.read", "inference.invoke", "mcp.invoke",
    "operations.acknowledge", "operations.cancel", "operations.read", "operations.result",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ensure_ascii=False,
    ).encode()).hexdigest()


class GuardError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def check(value: object, code: str) -> None:
    if not value:
        raise GuardError(code)


def private_json(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        check(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
              "private_input_requires_regular_0600_file")
        value = json.load(stream)
    check(isinstance(value, dict), "private_input_not_object")
    return value


def write_private(path: Path, value: object, secrets: tuple[str, ...] = ()) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    check(all(secret not in encoded for secret in secrets if secret), "receipt_contains_secret")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(encoded)


def policy(row: dict[str, Any]) -> dict[str, Any]:
    value = {name: row.get(name) for name in POLICY_FIELDS}
    value["models"] = sorted(set(value["models"]))
    value["scopes"] = sorted(set(value["scopes"]))
    return value


def team_policy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    teams = [row for row in rows if row["tenant_id"] == "stockholm"
             and row.get("name", "").startswith("stockholm-team-")
             and row.get("revoked_at") is None
             and (not row.get("expires_at") or datetime.fromisoformat(
                 row["expires_at"].replace("Z", "+00:00")) > datetime.now(UTC))]
    check(bool(teams), "active_stockholm_teams_missing")
    distinct = {digest(policy(row)) for row in teams}
    check(len(distinct) == 1, "stockholm_team_policies_diverge")
    selected = policy(teams[0])
    check(selected["max_concurrency"] == 5 and set(selected["scopes"]) == REQUIRED_SCOPES,
          "stockholm_team_policy_changed")
    return {"team_count": len(teams), "team_policy": selected,
            "team_policy_sha256": digest(selected)}


# Only the mounted admin credential is used, inside the CP pod, for read-only
# policy inspection. It is never returned or copied to the host. No SQL writes.
POD_READ = r'''
import hashlib, json, os, pathlib, urllib.request
from urllib.parse import urlsplit
secret = pathlib.Path(os.environ['FS2_ADMIN_TOKEN_FILE']).read_text().strip()
origin = os.environ['FS2_PUBLIC_BASE_URL']
request = urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens?tenant_id=stockholm',
    headers={'Authorization': 'Bearer '+secret, 'Host': urlsplit(origin).netloc})
with urllib.request.urlopen(request, timeout=15) as response:
    tokens = json.load(response)
fields = ('id','name','principal_id','tenant_id','models','scopes','max_concurrency',
    'request_budget','gpu_seconds_budget','rate_limit_requests','rate_window_seconds',
    'expires_at','revoked_at','fingerprint')
files = {}
for folder in ('/etc/fs2-serve', '/etc/fs2-scientific-batch'):
    for path in sorted(pathlib.Path(folder).rglob('*')):
        if path.is_file() and '..' not in path.parts:
            files[str(path)] = 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps({'public_endpoint':origin,
    'tokens':[{key:row.get(key) for key in fields} for row in tokens],
    'mounted_configuration_sha256':files},sort_keys=True))
'''


def kube(kubeconfig: Path, context: str, *arguments: str, script: str | None = None) -> Any:
    completed = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context,
         "--request-timeout=20s", *arguments],
        input=script, text=True, capture_output=True, timeout=45, check=False,
    )
    check(completed.returncode == 0, "kubectl_read_failed")
    return json.loads(completed.stdout)


def collect(kubeconfig: Path, context: str) -> dict[str, Any]:
    deployed = kube(kubeconfig, context, "-n", "fs2-system", "get", "deployments", "-o", "json")
    pods = kube(kubeconfig, context, "-n", "fs2-system", "get", "pods", "-o", "json")
    configuration = kube(kubeconfig, context, "-n", "fs2-system", "exec", "-i",
                         "deploy/fs2-serve-control-plane", "-c", "control-plane", "--",
                         "python", "-", script=POD_READ)
    deployments = []
    for item in deployed["items"]:
        deployments.append({
            "name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
            "generation": item["metadata"]["generation"],
            "observed_generation": item.get("status", {}).get("observedGeneration"),
            "replicas": item["spec"].get("replicas", 1),
            "ready_replicas": item.get("status", {}).get("readyReplicas", 0),
            "images": {c["name"]: c["image"] for c in item["spec"]["template"]["spec"]["containers"]},
        })
    pod_rows = [{
        "name": item["metadata"]["name"], "uid": item["metadata"]["uid"],
        "containers": [{"name": row["name"], "image_id": row.get("imageID"),
                        "ready": row.get("ready"), "restarts": row.get("restartCount", 0)}
                       for row in item.get("status", {}).get("containerStatuses", [])],
    } for item in pods["items"]]
    return {
        "schema": "fs2-serve.nebius.ai/stockholm-live-snapshot/v1",
        "collected_at": now(), "context": context,
        "public_endpoint": configuration["public_endpoint"],
        "deployments": sorted(deployments, key=lambda row: row["name"]),
        "pods": sorted(pod_rows, key=lambda row: row["name"]),
        "mounted_configuration_sha256": configuration["mounted_configuration_sha256"],
        **team_policy(configuration["tokens"]),
        # Only non-secret token metadata; needed to prove the disposable key has
        # the live policy before accepting its evidence. No real token material.
        "token_metadata": configuration["tokens"],
    }


def release_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compare deployment/config/policy, not collection time or restart counters."""
    return {"images": {row["name"]: row["images"] for row in snapshot["deployments"]},
            "configuration": snapshot["mounted_configuration_sha256"],
            "team_policy": snapshot["team_policy"],
            "public_endpoint": snapshot["public_endpoint"]}


def require_release(snapshot: dict[str, Any], expected_image: str) -> None:
    check("@sha256:" in expected_image, "immutable_cp_image_required")
    cp = next(row for row in snapshot["deployments"] if row["name"] == "fs2-serve-control-plane")
    check(cp["images"]["control-plane"] == expected_image, "control_plane_image_mismatch")
    check(cp["generation"] == cp["observed_generation"]
          and cp["ready_replicas"] == cp["replicas"] and cp["replicas"] > 0,
          "control_plane_rollout_incomplete")
    expected_digest = expected_image.split("@", 1)[1]
    running = [container for pod in snapshot["pods"] for container in pod["containers"]
               if container["name"] == "control-plane"]
    check(len(running) == cp["replicas"] and all(
        container["ready"] and expected_digest in (container["image_id"] or "")
        for container in running), "control_plane_pod_identity_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    snapshot = collect(args.kubeconfig, args.context)
    write_private(args.output, snapshot)
    print(json.dumps({"output": str(args.output), "team_count": snapshot["team_count"],
                      "team_policy_sha256": snapshot["team_policy_sha256"],
                      "scope": "read-only-no-inference-no-key-creation"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

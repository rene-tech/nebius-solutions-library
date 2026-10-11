"""Reproducible, internal-only s2pr2 pilot driver; never reads customer keys/data.

Run with components/scientific-ai's uv environment. Operator secrets stay in a
0700 directory outside Git; stdout contains identifiers/status, never key values.
No customer cluster, customer input, DNS, shared pool floor or S2 source is changed.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import secrets
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

S2_ROOT = Path("/home/tux/nebius-solutions-library-pr/k8s-inference")
CONTEXT = "nebius-mk8s-s2pr2-control-e00z83c35fh1x9my11"
API = "https://api.195.242.30.226.sslip.io"
PUBLIC = API + "/scientific-ai"
REGISTRY = "cr.eu-north1.nebius.cloud/e00dh8c3thpm0ptfea"
MODEL = "scientific-gromacs-pilot"


def command(*args):
    result = subprocess.run(args, cwd=S2_ROOT, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"operator command {args[0]} failed (details kept out of shared output)")
    return result.stdout


def keys():
    return json.loads(command("./stack.sh", "output", "models", "control", "-json", "tenant_keys"))["sciai"]


def write(path, value, *, private=False):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def now():
    return datetime.now(UTC).isoformat()


def call_tool(client, name, arguments):
    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    response.raise_for_status()
    result = response.json()["result"]
    if result.get("isError"):
        raise RuntimeError("MCP call failed")
    value = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    if value.get("error"):
        raise RuntimeError("MCP application request failed")
    return value


def prepare(directory, tag, api_tag=None):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise RuntimeError("private configuration directory must have mode 0700")
    selected = keys()
    secret = json.loads(
        command(
            "kubectl",
            "--context",
            CONTEXT,
            "-n",
            "tenant-demo",
            "get",
            "secret",
            "tenant-storage",
            "-o",
            "json",
        )
    )
    data = {key: base64.b64decode(value).decode() for key, value in secret["data"].items()}
    images = {}
    for name in ("api", "gromacs-sm89", "gromacs-sm120"):
        selected_tag = api_tag if name == "api" and api_tag else tag
        digest = command("crane", "digest", f"{REGISTRY}/scientific-ai/{name}:{selected_tag}").strip()
        images[name] = f"registry.serverless2.local/nebius/scientific-ai/{name}@{digest}"
    target = directory / "config.json"
    signing_key = (
        json.loads(target.read_bytes())["signing_key"] if target.exists() else secrets.token_urlsafe(48)
    )
    config = {
        "serverless_url": API,
        "public_url": PUBLIC,
        "model_id": MODEL,
        "profile_ref": "confidential-job-v1",
        "signing_key": signing_key,
        "runtime": {
            "revision": "gromacs-2026-4-" + tag,
            "checkpoint_compatibility_id": "gromacs-2026-4-native-v1",
            "images": {"rtx-pro-6000": images["gromacs-sm120"], "l40s": images["gromacs-sm89"]},
            "allowed_clusters": ["us-central1", "eu-north1"],
            "gpu_preference": ["rtx-pro-6000", "l40s"],
            # S2 only applies this to an unplaced run (both preferred slots
            # reserved), not to a placed run booting its new GPU node.
            "fallback_wait_s": 60,
        },
        "tenants": {
            "sciai": {
                "endpoint": data["endpoint"],
                "bucket": data["bucket"],
                "region": data["region"],
                "access_key": data["access_key"],
                "secret_key": data["secret_key"],
                "execution_key": selected["worker"],
                "prefix": "runs/scientific-ai-s2-gromacs-20261011",
            }
        },
    }
    write(target, config, private=True)
    write(
        directory / "pilot.tfvars.json",
        {
            "kube_context": CONTEXT,
            "image": images["api"],
            "private_config_file": str(target),
            "api_host": "api.195.242.30.226.sslip.io",
        },
        private=True,
    )
    write(directory / "images.json", images)
    print(
        json.dumps(
            {
                "private_config": str(target),
                "images": images,
                "tenant": "sciai",
                "storage_source": "existing internal demo workspace",
            }
        )
    )


def register(directory):
    config = json.loads((directory / "config.json").read_bytes())
    spec = {
        "id": MODEL,
        "kind": "job",
        "display_name": "Scientific AI GROMACS migration pilot",
        "description": "Owner: scientific-ai-serverless2-migration. Internal s2pr2 acceptance only.",
        "image": config["runtime"]["images"]["rtx-pro-6000"],
        "images": config["runtime"]["images"],
        "command": ["/opt/scientific-ai/bin/python", "-m", "scientific_ai.runner"],
        "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]},
        "cpu": "12",
        "memory": "24Gi",
        "disk_gi": 20,
        "scratch": "network",
        "grace_seconds": 300,
        "timeout_s": 1209600,
        "regions": ["us-central1", "eu-north1"],
        "parameters": [{"name": "protocol_ref", "type": "string", "default": config["runtime"]["revision"]}],
    }
    with httpx.Client(
        base_url=API, headers={"Authorization": "Bearer " + keys()["admin"]}, timeout=120
    ) as client:
        old = client.get("/v1/models/" + MODEL)
        if old.status_code == 404:
            response = client.post("/v1/models", json=spec)
        else:
            old.raise_for_status()
            response = client.put("/v1/models/" + MODEL, json=spec)
        if response.status_code not in (200, 201):
            print(
                json.dumps({"model_registration_http_status": response.status_code, "error": response.json()})
            )  # spec has no secrets or private data
            raise RuntimeError("model registration failed")
        write(directory / "model.json", response.json())
        print(json.dumps({"model_id": MODEL, "http_status": response.status_code}))


def fixture(steps):
    """Fresh 4096-atom LJ argon, NVE, 2 fs; runtime test, not a biomedical benchmark."""
    atoms = []
    for i in range(4096):
        x, y, z = i % 16, i // 16 % 16, i // 256
        atoms.append(
            f"{i + 1:5d}{'AR':<5}{'AR':>5}{i + 1:5d}"
            f"{0.1 + 0.5 * x:8.3f}{0.1 + 0.5 * y:8.3f}{0.1 + 0.5 * z:8.3f}"
        )
    files = {
        "argon.gro": "Synthetic GROMACS S2 recovery\n4096\n" + "\n".join(atoms) + "\n8 8 8\n",
        "topol.top": "[ defaults ]\n1 2 no 1 1\n[ atomtypes ]\nAR 39.948 0 A 0.34 0.997\n"
        "[ moleculetype ]\nArgon 1\n[ atoms ]\n1 AR 1 AR AR 1 0 39.948\n"
        "[ system ]\nSynthetic recovery\n[ molecules ]\nArgon 4096\n",
        "md.mdp": f"integrator = md\ndt = 0.002\nnsteps = {steps}\n"
        "nstlog = 1000\nnstenergy = 1000\nnstxout-compressed = 1000\n"
        "cutoff-scheme = Verlet\nnstlist = 100\ncoulombtype = Cut-off\n"
        "rcoulomb = 1.0\nvdwtype = Cut-off\nrvdw = 1.0\npbc = xyz\n"
        "tcoupl = no\npcoupl = no\nconstraints = none\ngen-vel = yes\n"
        "gen-temp = 120\ngen-seed = 1741\n",
    }
    target = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as tar,
    ):
        for name, text in files.items():
            data = text.encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    parameters = {
        "schema": "fs2-serve.nebius.ai/gromacs-workflow-request/v1",
        "threads": 6,
        "segment_minutes": 0.25,
        "checkpoint_minutes": 0.25,
        "max_wall_seconds": 3600,
        "jobs": [
            {
                "id": "argon",
                "steps": [
                    {
                        "id": "prepare",
                        "command": "grompp",
                        "args": ["-f", "md.mdp", "-c", "argon.gro", "-p", "topol.top", "-o", "run.tpr"],
                        "expected_outputs": ["run.tpr"],
                    },
                    {
                        "id": "md",
                        "command": "mdrun",
                        "args": [
                            "-s",
                            "run.tpr",
                            "-deffnm",
                            "md",
                            "-nb",
                            "gpu",
                            "-pme",
                            "cpu",
                            "-update",
                            "gpu",
                        ],
                        "expected_outputs": ["md.gro"],
                    },
                ],
            }
        ],
    }
    return target.getvalue(), parameters


def upload(client, data, media_type, compression="none"):
    r = client.post(
        "/v1/scientific-artifacts/uploads",
        json={
            "model_id": "gromacs",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "media_type": media_type,
            "compression": compression,
        },
    )
    r.raise_for_status()
    aid = r.json()["upload_id"]
    client.put(f"/v1/scientific-artifacts/uploads/{aid}/content", content=data).raise_for_status()
    r = client.post(f"/v1/scientific-artifacts/uploads/{aid}:finalize")
    r.raise_for_status()
    return r.json()["artifact"]


def submit(directory, name, steps, path):
    started = now()
    data, parameters = fixture(steps)
    with httpx.Client(
        base_url=PUBLIC, headers={"Authorization": "Bearer " + keys()["worker"]}, timeout=120
    ) as client:
        pointer = upload(client, data, "application/gzip", "gzip")
        manifest = json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": "synthetic-" + name,
                "entries": [
                    {
                        "name": "gromacs-inputs",
                        "semantic_type": "gromacs-input-bundle/v1",
                        "artifact": pointer,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request = {
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "run-workflow",
            "input_manifest": upload(client, manifest, "application/json"),
            "parameters": parameters,
            "client_context": {"display_name": "Synthetic S2 qualification " + name},
        }
        key = "s2-gromacs-20261011-" + name
        if path == "rest":
            response = client.post(
                "/v1/models/gromacs:submit", json=request, headers={"Idempotency-Key": key}
            )
            response.raise_for_status()
            value = response.json()
        else:
            value = call_tool(client, "submit_gromacs_workflow", {"request": request, "idempotency_key": key})
        write(
            directory / (name + ".json"),
            {
                "request": request,
                "response": value,
                "path": path,
                "started_at": started,
                "accepted_at": now(),
                "steps": steps,
            },
        )
        print(
            json.dumps(
                {k: value.get(k) for k in ("operation_id", "serverless_operation_id", "status", "reason")}
            )
        )


def status(directory, name):
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op = receipt["response"]["operation_id"]
    with httpx.Client(
        base_url=PUBLIC, headers={"Authorization": "Bearer " + keys()["worker"]}, timeout=120
    ) as client:
        response = client.get("/v1/operations/" + op)
        response.raise_for_status()
        value = response.json()
        write(directory / (name + "-status.json"), value)
        timeline_path = directory / (name + "-timeline.json")
        timeline = json.loads(timeline_path.read_bytes()) if timeline_path.exists() else []
        timeline.append({"observed_at": now(), "operation": value})
        write(timeline_path, timeline)
        print(
            json.dumps(
                {
                    k: value.get(k)
                    for k in (
                        "operation_id",
                        "serverless_operation_id",
                        "status",
                        "region",
                        "cluster",
                        "gpu_class",
                        "attempt_id",
                        "epoch",
                        "checkpoint_ref",
                        "reason",
                        "error",
                    )
                }
            )
        )
        if value["status"] == "SUCCEEDED":
            result = client.get("/v1/operations/" + op + "/result")
            result.raise_for_status()
            write(directory / (name + "-result.json"), result.json())
            manifest_id = result.json()["output_manifest"]["artifact_id"]
            manifest = client.get(f"/v1/artifacts/{manifest_id}/content")
            manifest.raise_for_status()
            write(directory / (name + "-outputs.json"), manifest.json())
            output = directory / (name + "-files")
            output.mkdir(exist_ok=True)
            for entry in manifest.json()["entries"]:
                pointer = entry["artifact"]
                download = client.get("/v1/artifacts/" + pointer["artifact_id"] + "/content")
                download.raise_for_status()
                assert len(download.content) == pointer["size_bytes"]
                assert hashlib.sha256(download.content).hexdigest() == pointer["sha256"]
                (output / pointer["sha256"]).write_bytes(download.content)
            native = result.json()["native_result"]
            md = [item for item in native["commands"] if item["command"][1] == "mdrun"]
            assert native["completed_steps"] == ["prepare", "md"]
            assert sum(item["command"][1] == "grompp" for item in native["commands"]) == 1
            assert md[-1]["checkpoint_step"] == receipt["steps"]
            print(
                json.dumps(
                    {
                        "result": "all hashes and native steps verified",
                        "files": len(manifest.json()["entries"]),
                        "final_step": md[-1]["checkpoint_step"],
                    }
                )
            )


def control(directory, name, action, path, gpu_class):
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op = receipt["response"]["operation_id"]
    with httpx.Client(
        base_url=PUBLIC, headers={"Authorization": "Bearer " + keys()["worker"]}, timeout=120
    ) as client:
        if action == "replay":
            key = "s2-gromacs-20261011-" + name
            if path == "mcp":
                value = call_tool(
                    client, "submit_gromacs_workflow", {"request": receipt["request"], "idempotency_key": key}
                )
            else:
                response = client.post(
                    "/v1/models/gromacs:submit", json=receipt["request"], headers={"Idempotency-Key": key}
                )
                response.raise_for_status()
                value = response.json()
            assert value["operation_id"] == op
        elif action == "cancel":
            if path == "mcp":
                value = call_tool(client, "cancel_scientific_run", {"operation_id": op})
            else:
                response = client.post("/v1/operations/" + op + ":cancel")
                response.raise_for_status()
                value = response.json()
        else:
            key = "s2-gromacs-resume-" + name + "-" + gpu_class
            args = {"idempotency_key": key, "gpu_class": gpu_class}
            if path == "mcp":
                value = call_tool(client, "resume_gromacs_workflow", {"operation_id": op, **args})
            else:
                response = client.post(
                    "/v1/operations/" + op + ":resume", json=args, headers={"Idempotency-Key": key}
                )
                response.raise_for_status()
                value = response.json()
            write(
                directory / (name + "-resumed.json"),
                {
                    **receipt,
                    "response": value,
                    "path": path,
                    "resumed_at": now(),
                    "original_operation_id": op,
                },
            )
        write(
            directory / (name + "-" + action + "-receipt.json"),
            {"at": now(), "path": path, "response": value},
        )
        print(
            json.dumps(
                {
                    "action": action,
                    "operation_id": value.get("operation_id"),
                    "status": value.get("status"),
                    "replay_matches": action == "replay",
                }
            )
        )


def boundaries(directory, name):
    """Real gateway checks with internal identities; no customer credentials."""
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op, key = receipt["response"]["operation_id"], "s2-gromacs-20261011-" + name
    selected, evidence = keys(), {"at": now()}
    with httpx.Client(base_url=PUBLIC, timeout=120) as client:
        evidence["anonymous_rest"] = client.get("/v1/scientific-models").status_code
        evidence["anonymous_mcp"] = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).status_code
        evidence["other_principal_operation"] = client.get(
            "/v1/operations/" + op, headers={"Authorization": "Bearer " + selected["admin"]}
        ).status_code
        evidence["other_principal_artifact"] = client.get(
            "/v1/artifacts/" + receipt["request"]["input_manifest"]["artifact_id"] + "/content",
            headers={"Authorization": "Bearer " + selected["admin"]},
        ).status_code
        assert evidence["anonymous_rest"] == evidence["anonymous_mcp"] == 401
        assert evidence["other_principal_operation"] == evidence["other_principal_artifact"] == 404
        headers = {"Authorization": "Bearer " + selected["worker"], "Idempotency-Key": key}

        def replay(_):
            response = client.post("/v1/models/gromacs:submit", json=receipt["request"], headers=headers)
            response.raise_for_status()
            return response.json()["operation_id"]

        with ThreadPoolExecutor(max_workers=6) as workers:
            identities = list(workers.map(replay, range(6)))
        assert identities == [op] * 6
        evidence["concurrent_replays"] = {"requests": 6, "unique_operations": len(set(identities))}
        altered = json.loads(json.dumps(receipt["request"]))
        altered["parameters"]["max_wall_seconds"] += 1
        evidence["different_intent_same_key"] = client.post(
            "/v1/models/gromacs:submit", json=altered, headers=headers
        ).status_code
        assert evidence["different_intent_same_key"] == 409
    write(directory / (name + "-boundaries.json"), evidence)
    print(json.dumps(evidence))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["prepare", "register", "submit", "status", "replay", "cancel", "resume", "boundaries"],
    )
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--tag", default="pilot-20261011-r2")
    parser.add_argument("--api-tag")
    parser.add_argument("--name", default="rest-cohort-1")
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument("--path", choices=["rest", "mcp"], default="rest")
    parser.add_argument("--gpu-class", choices=["l40s", "rtx-pro-6000"], default="l40s")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.private_dir, args.tag, args.api_tag)
    elif args.action == "register":
        register(args.private_dir)
    elif args.action == "submit":
        submit(args.private_dir, args.name, args.steps, args.path)
    elif args.action == "status":
        status(args.private_dir, args.name)
    elif args.action == "boundaries":
        boundaries(args.private_dir, args.name)
    else:
        control(args.private_dir, args.name, args.action, args.path, args.gpu_class)


if __name__ == "__main__":
    main()

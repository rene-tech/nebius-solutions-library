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
import re
import secrets
import subprocess
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import httpx
from protein_fixture import fixture as protein_fixture
from protein_fixture import validate_coordinates, validate_native

S2_ROOT = Path("/home/tux/nebius-solutions-library-pr/k8s-inference")
CONTEXT = "nebius-mk8s-s2pr2-control-e00z83c35fh1x9my11"
API = "https://api.195.242.30.226.sslip.io"
PUBLIC = API + "/scientific-ai"
REGISTRY = "cr.eu-north1.nebius.cloud/e00dh8c3thpm0ptfea"
MODEL = "scientific-gromacs-pilot"
TENANT = "sciai"
NAMESPACE = "tenant-sciai"
KEY_NAMES = {"admin": "admin", "worker": "worker"}
CONTEXTS = {
    "us-central1": "nebius-mk8s-s2pr2-us-central1-u00pdtgvqv6yjdjndr",
    "eu-north1": "nebius-mk8s-s2pr2-hub-e00nxtpw0h8ysw9vx5",
}
CLUSTERS = {
    "us-central1": "mk8scluster-u00pdtgvqv6yjdjndr",
    "eu-north1": "mk8scluster-e00nxtpw0h8ysw9vx5",
}
STORAGE_PREFIX = "runs/scientific-ai-s2-gromacs-20261011"
OPERATOR_ENV = {}


def configure(path):
    """Select an explicit internal fleet; never discover or mutate customer targets."""
    global S2_ROOT, CONTEXT, API, PUBLIC, REGISTRY, MODEL, TENANT, NAMESPACE
    global KEY_NAMES, CONTEXTS, CLUSTERS, STORAGE_PREFIX, OPERATOR_ENV
    target = json.loads(path.read_bytes())
    if target["tenant"] not in {"system", "sciai"}:
        raise ValueError("acceptance must use an existing internal tenant")
    if not target["api_url"].startswith("https://") or not target["storage_prefix"].startswith("runs/"):
        raise ValueError("an HTTPS fleet and owned test prefix are required")
    if set(target["contexts"]) != set(target["clusters"]):
        raise ValueError("every worker needs an explicit context and cluster identity")
    S2_ROOT = Path(target["operator_stack"])
    CONTEXT, API = target["control_context"], target["api_url"].rstrip("/")
    PUBLIC, REGISTRY = API + "/scientific-ai", target["registry"]
    MODEL, TENANT = target["model_id"], target["tenant"]
    NAMESPACE, KEY_NAMES = "tenant-" + TENANT, target["key_names"]
    CONTEXTS, CLUSTERS = target["contexts"], target["clusters"]
    STORAGE_PREFIX = target["storage_prefix"].rstrip("/")
    OPERATOR_ENV = {"TFVARS": target["tfvars"], "KUBECONFIG": target["kubeconfig"]}
    keys.cache_clear()


def command(*args):
    result = subprocess.run(
        args, cwd=S2_ROOT, env={**os.environ, **OPERATOR_ENV}, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError(f"operator command {args[0]} failed (details kept out of shared output)")
    return result.stdout


@lru_cache(maxsize=1)
def keys():
    values = json.loads(command("./stack.sh", "output", "platform", "control", "-json", "tenant_keys"))[
        TENANT
    ]
    return {role: values[name] for role, name in KEY_NAMES.items()}


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


def prepare(
    directory,
    tag,
    api_tag=None,
    *,
    automatic_recovery=False,
    storage_config=None,
    storage_tenant=None,
    delivery_retry=False,
):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise RuntimeError("private configuration directory must have mode 0700")
    selected = keys()
    if storage_config is not None:
        if storage_config.stat().st_mode & 0o077 or storage_tenant not in {"system", "sciai", "demo"}:
            raise ValueError("reuse only a private, approved internal storage configuration")
        source = json.loads(storage_config.read_bytes())["tenants"][storage_tenant]
        data = {key: source[key] for key in ("endpoint", "bucket", "region", "access_key", "secret_key")}
    else:
        if OPERATOR_ENV:
            raise ValueError("a new target requires an explicit internal storage source")
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
        **({"delivery_retry_enabled": True} if delivery_retry else {}),
        "signing_key": signing_key,
        "workload_identity": {
            "audience": PUBLIC + "/internal/storage",
            "issuers": {
                region: {
                    "issuer": "https://oidc.mk8s.api.nebius.cloud/" + cluster,
                    "jwks_url": "https://oidc.mk8s.api.nebius.cloud/" + cluster + "/openid/v1/jwks",
                }
                for region, cluster in CLUSTERS.items()
            },
        },
        "runtime": {
            "revision": "gromacs-2026-4-" + tag,
            "checkpoint_compatibility_id": "gromacs-2026-4-native-v1",
            "images": {"rtx-pro-6000": images["gromacs-sm120"], "l40s": images["gromacs-sm89"]},
            "allowed_clusters": list(CLUSTERS),
            "gpu_preference": ["rtx-pro-6000", "l40s"],
            # S2 only applies this to an unplaced run (both preferred slots
            # reserved), not to a placed run booting its new GPU node.
            "fallback_wait_s": 60,
            **(
                {
                    "recovery": {
                        "on_infrastructure_loss": "recover",
                        "max_recoveries": 1,
                        "wait_s": 0,
                        "deadline_s": 3600,
                    }
                }
                if automatic_recovery
                else {}
            ),
        },
        "tenants": {
            TENANT: {
                "endpoint": data["endpoint"],
                "bucket": data["bucket"],
                "region": data["region"],
                "access_key": data["access_key"],
                "secret_key": data["secret_key"],
                "execution_key": selected["worker"],
                "workload_identity_ref": "scientific-ai-storage",
                "namespace": NAMESPACE,
                "prefix": STORAGE_PREFIX,
            }
        },
    }
    write(target, config, private=True)
    write(
        directory / "pilot.tfvars.json",
        {
            "kube_context": CONTEXT,
            **({"kube_config_path": OPERATOR_ENV["KUBECONFIG"]} if OPERATOR_ENV else {}),
            "image": images["api"],
            "private_config_file": str(target),
            "api_host": API.removeprefix("https://"),
        },
        private=True,
    )
    write(directory / "images.json", images)
    print(
        json.dumps(
            {
                "private_config": str(target),
                "images": images,
                "tenant": TENANT,
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
        "description": "Owner: scientific-ai-serverless2-migration. Internal fleet acceptance only.",
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
        "regions": config["runtime"]["allowed_clusters"],
        "parameters": [{"name": "protocol_ref", "type": "string", "default": config["runtime"]["revision"]}],
    }
    if config.get("delivery_retry_enabled"):
        spec["finalize"] = {
            "command": "/opt/scientific-ai/bin/python -m scientific_ai.runner --finish-delivery",
            "on_exit_codes": [75],
            "timeout_s": 1800,
            "cpu": "1",
            "memory": "2Gi",
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


def submit(directory, name, steps, path, *, benchmem_tpr=None):
    started = now()
    expected = None
    if benchmem_tpr is None:
        data, parameters = fixture(steps)
        label = "Synthetic S2 qualification "
    else:
        data, parameters, expected = protein_fixture(benchmem_tpr, steps)
        label = "Public benchMEM S2 qualification "
    with httpx.Client(
        base_url=PUBLIC, headers={"Authorization": "Bearer " + keys()["worker"]}, timeout=120
    ) as client:
        pointer = upload(client, data, "application/gzip", "gzip")
        manifest = json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": "qualification-" + name,
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
            "client_context": {"display_name": label + name},
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
                **({"expected": expected} if expected else {}),
            },
        )
        print(
            json.dumps(
                {k: value.get(k) for k in ("operation_id", "serverless_operation_id", "status", "reason")}
            )
        )


def status(directory, name, path="rest"):
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op = receipt["response"]["operation_id"]
    with httpx.Client(
        base_url=PUBLIC, headers={"Authorization": "Bearer " + keys()["worker"]}, timeout=120
    ) as client:
        if path == "mcp":
            value = call_tool(client, "get_scientific_status", {"operation_id": op})
        else:
            response = client.get("/v1/operations/" + op)
            response.raise_for_status()
            value = response.json()
        write(directory / (name + "-status.json"), value)
        timeline_path = directory / (name + "-timeline.json")
        timeline = json.loads(timeline_path.read_bytes()) if timeline_path.exists() else []
        timeline.append({"observed_at": now(), "path": path, "operation": value})
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
            if path == "mcp":
                result = call_tool(client, "get_scientific_result", {"operation_id": op})
            else:
                response = client.get("/v1/operations/" + op + "/result")
                response.raise_for_status()
                result = response.json()
            write(directory / (name + "-result.json"), result)
            manifest_id = result["output_manifest"]["artifact_id"]
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
            native = result["native_result"]
            checked = validate_native(native, receipt)
            if receipt.get("expected", {}).get("protocol", {}).get("fixture") == "mpinat-benchMEM":
                final = [file for file in native["files"] if file["path"] == "md.gro"]
                assert len(final) == 1, "benchMEM must return its final coordinates"
                checked.update(validate_coordinates((output / final[0]["sha256"]).read_bytes()))
            write(directory / (name + "-native-validation.json"), {"at": now(), **checked})
            print(
                json.dumps(
                    {
                        "result": "all hashes and native steps verified",
                        "files": len(manifest.json()["entries"]),
                        **checked,
                    }
                )
            )
        return value


def wait_for(directory, name, path="rest", *, ready=False, timeout=1200):
    """Bounded operator test polling, not a production scheduling/recovery loop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = status(directory, name, path)
        if value["status"] == "SUCCEEDED":
            return value
        if value["status"] in {"FAILED", "CANCELLED", "EXPORT_FAILED", "TIMED_OUT"}:
            raise RuntimeError("synthetic acceptance operation ended unsuccessfully")
        if ready and value["status"] == "RUNNING" and value.get("attempt_id"):
            return value
        time.sleep(15)
    raise TimeoutError("synthetic acceptance deadline reached; operation was not silently resubmitted")


def recovery_cycle(directory, name):
    """Explicit operator fault drill, NOT automatic production failover."""
    wait_for(directory, name, ready=True)
    identity_boundaries(directory, name)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        observed = checkpoint(directory, name)
        if observed:
            state, snapshot = observed
            commands = [c for c in snapshot.state["commands"] if c["command"][1] == "mdrun"]
            if commands and commands[-1].get("checkpoint_step", 0) > 0:
                break
        time.sleep(10)
    else:
        raise TimeoutError("no native checkpoint for safe fault injection")
    interrupt(directory, name)
    control(directory, name, "resume", "rest", "l40s")
    resumed = name + "-resumed"
    wait_for(directory, resumed, ready=True)
    identity_boundaries(directory, resumed, stale_name=name)
    wait_for(directory, resumed, "mcp")


def automatic_recovery_cycle(directory, name):
    """Owned Workload eviction; no customer/node mutation or manual :resume.

    Deactivation holds back same-class replacement while S2 detects confirmed
    loss. The original public handle must follow exactly one L40S successor.
    """
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    wait_for(directory, name, ready=True)
    identity_boundaries(directory, name)
    for suffix in ("-identity-private.json", "-identity-boundaries.json"):
        write(
            directory / (name + "-before" + suffix),
            json.loads((directory / (name + suffix)).read_bytes()),
            private="private" in suffix,
        )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        observed = checkpoint(directory, name)
        if observed:
            state, snapshot = observed
            md = [c for c in snapshot.state["commands"] if c["command"][1] == "mdrun"]
            if (
                snapshot.kind == "checkpoint"
                and md
                and 0 < md[-1].get("checkpoint_step", 0) < receipt["steps"]
            ):
                break
        time.sleep(10)
    else:
        raise TimeoutError("no acknowledged mid-run checkpoint; no fault injected")
    if state["gpu_class"] != "rtx-pro-6000" or state["status"] != "RUNNING":
        raise RuntimeError("automatic cross-GPU qualification must start on RTX")
    s2_id = state["serverless_operation_id"]
    ctl = ("kubectl", "--context", CONTEXT, "-n", NAMESPACE)
    job = json.loads(command(*ctl, "get", "job", s2_id, "-o", "json"))
    workloads = json.loads(command(*ctl, "get", "workloads.kueue.x-k8s.io", "-o", "json"))["items"]
    owned = [
        w
        for w in workloads
        if any(
            r.get("uid") == job["metadata"]["uid"] and r.get("name") == s2_id and r.get("kind") == "Job"
            for r in w["metadata"].get("ownerReferences", [])
        )
    ]
    if len(owned) != 1 or not s2_id.startswith("op-"):
        raise RuntimeError("cannot uniquely identify this internal operation's Workload")
    workload = owned[0]["metadata"]["name"]
    patch = (
        *ctl,
        "patch",
        "workloads.kueue.x-k8s.io",
        workload,
        "--type=merge",
        "-p",
        '{"spec":{"active":false}}',
    )
    command(*patch, "--dry-run=server")
    evidence = {
        "at": now(),
        "public_operation_id": state["operation_id"],
        "old_s2_id": s2_id,
        "workload": workload,
        "workload_uid": owned[0]["metadata"]["uid"],
        "old_attempt_id": state["attempt_id"],
        "old_epoch": state["epoch"],
        "checkpoint_ref": state["checkpoint_ref"],
        "checkpoint_step": md[-1]["checkpoint_step"],
        "fault": "deactivate only the owned manager Workload after durable checkpoint",
        "manual_resume_called": False,
    }
    write(directory / (name + "-automatic-fault.json"), evidence)
    command(*patch)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        current = status(directory, name)
        if current["status"] in {"FAILED", "CANCELLED", "TIMED_OUT", "EXPORT_FAILED"}:
            raise RuntimeError("automatic recovery failed; no operator resume was attempted")
        if current["status"] == "RUNNING" and current["serverless_operation_id"] != s2_id:
            if current["gpu_class"] != "l40s" or current["epoch"] <= state["epoch"]:
                raise AssertionError("replacement did not move to a newer L40S attempt")
            evidence.update(
                recovered_at=now(),
                new_s2_id=current["serverless_operation_id"],
                new_attempt_id=current["attempt_id"],
                new_epoch=current["epoch"],
            )
            write(directory / (name + "-automatic-fault.json"), evidence)
            identity_boundaries(directory, name, stale_name=name + "-before")
            break
        time.sleep(10)
    else:
        raise TimeoutError("automatic recovery did not start within the bounded test interval")
    wait_for(directory, name, "mcp")
    evidence["completed_at"] = now()
    write(directory / (name + "-automatic-fault.json"), evidence)


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
                    "/v1/operations/" + op + ":resume",
                    json={"gpu_class": gpu_class},
                    headers={"Idempotency-Key": key},
                )
                response.raise_for_status()
                value = response.json()
            target = directory / (name + "-resumed.json")
            if target.exists():
                previous = json.loads(target.read_bytes())
                assert previous["response"]["operation_id"] == value["operation_id"]
                continuation = {
                    **previous,
                    "response": value,
                    "resume_replays": [*previous.get("resume_replays", []), {"at": now(), "path": path}],
                }
            else:
                continuation = {
                    **receipt,
                    "response": value,
                    "path": path,
                    "resumed_at": now(),
                    "original_operation_id": op,
                }
            write(target, continuation)
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
        evidence["unsupported_resume_options"] = {}
        for field, value in {
            "job_id": "unsupported-replica",
            "performance_mode": "optimized",
            "max_wall_seconds": 1209600,
            "gpu_class": "unqualified-gpu",
        }.items():
            rejected = client.post("/v1/operations/" + op + ":resume", json={field: value}, headers=headers)
            evidence["unsupported_resume_options"][field] = rejected.status_code
            assert rejected.status_code == 422
    write(directory / (name + "-boundaries.json"), evidence)
    print(json.dumps(evidence))


def storage_denial(directory, name):
    """Check a terminal job's equivalent private grant; never emit its token.

    This tests logical-job withdrawal. identity_boundaries separately exercises
    the required projected pod identity while an operation is active.
    """
    import jwt

    from scientific_ai.gateway import Gateway, Settings

    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op = receipt["response"]["operation_id"]
    gateway = Gateway(Settings.model_validate_json((directory / "config.json").read_bytes()))
    with gateway.client("Bearer " + keys()["worker"]) as (client, store, _):
        state = gateway.status(client, store, op)
        record = store.get("operations", op)
        root = store.get("operations", record["root_id"])
        current = client.status(root["latest_s2_id"])
        if current["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPORT_FAILED"}:
            raise RuntimeError("refusing terminal-denial assertion while the logical job is active")
        token = jwt.encode(
            {
                "iss": "scientific-ai",
                "aud": "job-storage",
                "owner": store.owner.model_dump(),
                "op": record["root_id"],
            },
            gateway.settings.signing_key.get_secret_value(),
            algorithm="HS256",
        )
    captured = directory / (name + "-identity-private.json")
    projected = json.loads(captured.read_bytes())["token"] if captured.exists() else ""
    with httpx.Client(base_url=PUBLIC, timeout=60) as http:
        response = http.post(
            "/internal/storage",
            headers={"Authorization": "Bearer " + token, "X-Scientific-Workload-Token": projected},
            json={"action": "get", "key": "blobs/" + "a" * 64, "attempt_id": current["attempt_id"]},
        )
    evidence = {
        "at": now(),
        "operation_id": op,
        "status": state["status"],
        "latest_operation_status": current["status"],
        "new_object_url_after_terminal_http": response.status_code,
        "previously_signed_url_max_ttl_seconds": 60,
        "per_pod_attestation_proved": False,
        "previously_accepted_projected_identity_included": bool(projected),
    }
    assert response.status_code == 403
    write(directory / (name + "-storage-denial.json"), evidence)
    print(json.dumps(evidence))


def identity_boundaries(directory, name, stale_name=None, *, multipart=False):
    """Use only this pilot's synthetic pod; never print its projected token.

    Save a 0600 token receipt for a later replaced-pod test. With --stale-name,
    try that old real token against the current attempt, even supplying the new
    attempt ID and the correct logical-job capability. No data is downloaded.
    """
    import jwt

    from scientific_ai.gateway import Gateway, Settings

    receipt = json.loads((directory / (name + ".json")).read_bytes())
    gateway = Gateway(Settings.model_validate_json((directory / "config.json").read_bytes()))
    with gateway.client("Bearer " + keys()["worker"]) as (client, store, _):
        record = store.get("operations", receipt["response"]["operation_id"])
        root = store.get("operations", record["root_id"])
        current = client.status(root["latest_s2_id"])
        if current["status"] != "RUNNING" or not current.get("attempt_id"):
            raise RuntimeError("identity qualification requires an active owned synthetic attempt")
        grant = jwt.encode(
            {
                "iss": "scientific-ai",
                "aud": "job-storage",
                "owner": store.owner.model_dump(),
                "op": record["root_id"],
            },
            gateway.settings.signing_key.get_secret_value(),
            algorithm="HS256",
        )
    context, pod = CONTEXTS[current["cluster"]], current["attempt_id"]
    metadata = json.loads(
        command("kubectl", "--context", context, "-n", NAMESPACE, "get", "pod", pod, "-o", "json")
    )
    if not pod.startswith(current["id"] + "-"):
        raise RuntimeError("refusing another operation's pod")
    token = command(
        "kubectl",
        "--context",
        context,
        "-n",
        NAMESPACE,
        "exec",
        pod,
        "-c",
        "main",
        "--",
        "/opt/scientific-ai/bin/python",
        "-c",
        "from pathlib import Path; print(Path('/var/run/secrets/workload-identity/token').read_text())",
    ).strip()
    write(
        directory / (name + "-identity-private.json"),
        {
            "token": token,
            "attempt_id": pod,
            "pod_uid": metadata["metadata"]["uid"],
            "cluster": current["cluster"],
        },
        private=True,
    )
    headers = {"Authorization": "Bearer " + grant}
    request = {"action": "get", "key": "blobs/" + "a" * 64, "attempt_id": pod}
    with httpx.Client(base_url=PUBLIC, timeout=60) as http:

        def check(identity):
            return http.post(
                "/internal/storage",
                headers={**headers, "X-Scientific-Workload-Token": identity},
                json=request,
            ).status_code

        evidence = {
            "at": now(),
            "attempt_id": pod,
            "cluster": current["cluster"],
            "current_identity": check(token),
            "missing_identity": check(""),
            "forged_identity": check("not-a-signed-pod-token"),
        }
        assert evidence["current_identity"] == 200, evidence
        assert evidence["missing_identity"] == evidence["forged_identity"] == 403, evidence
        if stale_name:
            previous = json.loads((directory / (stale_name + "-identity-private.json")).read_bytes())
            assert previous["attempt_id"] != pod
            evidence["stale_identity_with_current_attempt_id"] = check(previous["token"])
            assert evidence["stale_identity_with_current_attempt_id"] == 403, evidence
    if multipart:
        # Test the real worker credential/egress/multipart path. The object is
        # synthetic, confined to this operation, and never an engine output.
        script = """
import hashlib, io, json, socket, time
from pathlib import Path
import httpx
from scientific_ai.broker import BrokerObjects
from scientific_ai.runner import PrivateRun
b = PrivateRun.model_validate_json(Path('/var/run/serverless2/private/run.json').read_bytes())
data = b'x' * (129 * 1024**2)
digest = hashlib.sha256(data).hexdigest()
with httpx.Client(timeout=120, follow_redirects=False) as http:
    objects = BrokerObjects(http=http, url=b.broker_url, token=b.storage_token,
        attempt_id=socket.gethostname(), identity_token_file=Path('/var/run/secrets/workload-identity/token'))
    started = time.monotonic()
    objects.put('blobs/' + digest, io.BytesIO(data))
    written = time.monotonic()
    checked, size = hashlib.sha256(), 0
    with objects.get('blobs/' + digest) as response:
        while block := response.read(1024**2):
            checked.update(block)
            size += len(block)
    assert size == len(data) and checked.hexdigest() == digest
    print(json.dumps({'bytes': size, 'sha256': digest, 'verified': True,
        'upload_s': written-started, 'readback_s': time.monotonic()-written}))
"""
        evidence["multipart"] = json.loads(
            command(
                "kubectl",
                "--context",
                context,
                "-n",
                NAMESPACE,
                "exec",
                pod,
                "-c",
                "main",
                "--",
                "/opt/scientific-ai/bin/python",
                "-c",
                script,
            )
        )
    write(directory / (name + "-identity-boundaries.json"), evidence)
    print(json.dumps(evidence))


def trajectories(directory, name, engine_image):
    """Parse every returned XTC with real GROMACS, using an offline CPU container."""
    if not engine_image:
        raise RuntimeError("--engine-image is required; use a locally built GROMACS 2026.4 image")
    image_id = command("docker", "image", "inspect", engine_image, "--format", "{{.Id}}").strip()
    result = json.loads((directory / (name + "-result.json")).read_bytes())
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    checked, next_time, previous_source = [], 0.0, None
    for item in sorted(result["native_result"]["files"], key=lambda item: item["path"]):
        if not item["path"].endswith(".xtc"):
            continue
        source = (directory / (name + "-files") / item["sha256"]).resolve(strict=True)
        assert source.stat().st_size == item["size_bytes"]
        with source.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == item["sha256"]
        process = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "/opt/gromacs/bin/gmx",
                "--mount",
                f"type=bind,src={source},dst=/input.xtc,readonly",
                image_id,
                "check",
                "-f",
                "/input.xtc",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        output = process.stdout + process.stderr
        atoms = int(re.search(r"# Atoms\s+(\d+)", output)[1])
        first = float(re.search(r"Reading frame\s+0 time\s+([0-9.]+)", output)[1])
        counts = re.search(r"^Coords\s+(\d+)\s+([0-9.]+)", output, re.MULTILINE)
        frames, interval = int(counts[1]), float(counts[2])
        assert atoms == 4096 and interval == 2.0
        overlap_sha256 = None
        if checked and first == checked[-1]["last_ps"]:
            # A native checkpoint exactly on the output grid may write its last frame again with -noappend.
            # Accept ONE boundary overlap only when native extraction proves byte-identical frames.
            script = """
printf '0\\n' | /opt/gromacs/bin/gmx trjconv -f /previous.xtc -o /tmp/previous.xtc -dump "$1" >/dev/null 2>&1
printf '0\\n' | /opt/gromacs/bin/gmx trjconv -f /next.xtc -o /tmp/next.xtc -dump "$1" >/dev/null 2>&1
cmp /tmp/previous.xtc /tmp/next.xtc
sha256sum /tmp/previous.xtc
"""
            duplicate = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=64m",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--entrypoint",
                    "/bin/sh",
                    "--mount",
                    f"type=bind,src={previous_source},dst=/previous.xtc,readonly",
                    "--mount",
                    f"type=bind,src={source},dst=/next.xtc,readonly",
                    image_id,
                    "-ec",
                    script,
                    "boundary-check",
                    str(first),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            overlap_sha256 = duplicate.stdout.split()[0]
            assert re.fullmatch(r"[a-f0-9]{64}", overlap_sha256)
        else:
            assert first == next_time, f"trajectory gap before {item['path']}: {first} != {next_time}"
        last = first + (frames - 1) * interval
        next_time = last + interval
        previous_source = source
        checked.append(
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "atoms": atoms,
                "frames": frames,
                "duplicate_boundary_frames": int(overlap_sha256 is not None),
                "identical_boundary_frame_sha256": overlap_sha256,
                "first_ps": first,
                "last_ps": last,
                "interval_ps": interval,
                "gromacs_check_exit_code": process.returncode,
            }
        )
    assert checked and checked[-1]["last_ps"] == receipt["steps"] * 0.002
    raw_frames = sum(item["frames"] for item in checked)
    duplicate_frames = sum(item["duplicate_boundary_frames"] for item in checked)
    assert raw_frames - duplicate_frames == receipt["steps"] // 1000 + 1
    evidence = {
        "at": now(),
        "engine_image_id": image_id,
        "segments": checked,
        "continuous": True,
        "raw_frames": raw_frames,
        "identical_boundary_duplicates": duplicate_frames,
        "unique_frames": raw_frames - duplicate_frames,
        "raw_files_unchanged": True,
    }
    write(directory / (name + "-trajectory-check.json"), evidence)
    print(json.dumps(evidence))


def checkpoint(directory, name):
    """Operator evidence of the acknowledged object's native progress; read only."""
    from scientific_ai.gateway import Gateway, Settings

    receipt = json.loads((directory / (name + ".json")).read_bytes())
    op = receipt["response"]["operation_id"]
    gateway = Gateway(Settings.model_validate_json((directory / "config.json").read_bytes()))
    with gateway.client("Bearer " + keys()["worker"]) as (client, store, _):
        state = gateway.status(client, store, op)
        ref = state.get("checkpoint_ref")
        if not ref:
            print(json.dumps({"status": state["status"], "checkpoint": None}))
            return
        record = store.get("operations", op)
        snapshot = store.artifacts(record["root_id"]).load(ref)
        write(directory / (name + "-checkpoint.json"), snapshot.model_dump(mode="json"))
        md = [c for c in snapshot.state["commands"] if c["command"][1] == "mdrun"]
        print(
            json.dumps(
                {
                    "status": state["status"],
                    "checkpoint_ref": ref,
                    "kind": snapshot.kind,
                    "attempt_id": state.get("attempt_id"),
                    "gpu_class": state.get("gpu_class"),
                    "native_step": md[-1].get("checkpoint_step") if md else None,
                }
            )
        )
        return state, snapshot


def interrupt(directory, name):
    """Lose only a verified synthetic RTX attempt, then remove its scratch PVC.

    API cancellation prevents an RTX retry racing the explicit L40S resume.
    It happens AFTER force deletion, not instead of abrupt loss.
    """
    observed = checkpoint(directory, name)
    if not observed:
        raise RuntimeError("no acknowledged checkpoint; refusing destructive fault injection")
    state, snapshot = observed
    receipt = json.loads((directory / (name + ".json")).read_bytes())
    md = [c for c in snapshot.state["commands"] if c["command"][1] == "mdrun"]
    if (
        state["status"] != "RUNNING"
        or state["gpu_class"] != "rtx-pro-6000"
        or snapshot.kind != "checkpoint"
        or not md
        or not 0 < md[-1]["checkpoint_step"] < receipt["steps"]
    ):
        raise RuntimeError("fault requires a committed, strictly mid-simulation RTX checkpoint")
    context = CONTEXTS[state["cluster"]]
    kubectl = ("kubectl", "--context", context, "-n", NAMESPACE)
    pod = json.loads(command(*kubectl, "get", "pod", state["attempt_id"], "-o", "json"))
    s2_id = state["serverless_operation_id"]
    if not any(r["kind"] == "Job" and r["name"] == s2_id for r in pod["metadata"]["ownerReferences"]):
        raise RuntimeError("pod does not belong to this internal test operation")
    volumes = [
        v["persistentVolumeClaim"]["claimName"]
        for v in pod["spec"]["volumes"]
        if "persistentVolumeClaim" in v
    ]
    if volumes != [s2_id + "-work"]:
        raise RuntimeError("unexpected scratch volume; refusing to delete it")
    evidence = {
        "at": now(),
        "operation_id": state["operation_id"],
        "s2_id": s2_id,
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "node": pod["spec"]["nodeName"],
        "old_pvc": volumes[0],
        "checkpoint_ref": state["checkpoint_ref"],
        "native_step": md[-1]["checkpoint_step"],
    }
    write(directory / (name + "-fault.json"), evidence)
    command(*kubectl, "delete", "pod", evidence["pod"], "--grace-period=0", "--force", "--wait=false")
    control(directory, name, "cancel", "rest", "l40s")
    command(
        *kubectl,
        "wait",
        "--for=delete",
        "pods",
        "-l",
        "batch.kubernetes.io/job-name=" + s2_id,
        "--timeout=30s",
    )
    command(*kubectl, "delete", "pvc", volumes[0], "--wait=true", "--timeout=30s")
    evidence["old_scratch_deleted_at"] = now()
    write(directory / (name + "-fault.json"), evidence)
    print(json.dumps(evidence))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "prepare",
            "register",
            "submit",
            "status",
            "replay",
            "cancel",
            "resume",
            "boundaries",
            "checkpoint",
            "interrupt",
            "storage-denial",
            "trajectories",
            "identity-boundaries",
            "wait",
            "recovery-cycle",
            "automatic-recovery-cycle",
        ],
    )
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, help="Explicit internal fleet configuration")
    parser.add_argument("--storage-config", type=Path, help="Existing private internal storage configuration")
    parser.add_argument("--storage-tenant", choices=["system", "sciai", "demo"])
    parser.add_argument("--tag", default="pilot-20261011-r6")
    parser.add_argument("--api-tag", default="pilot-20261011-r10")
    parser.add_argument("--engine-image")
    parser.add_argument("--benchmem-tpr", type=Path)
    parser.add_argument("--automatic-recovery", action="store_true")
    parser.add_argument("--delivery-retry", action="store_true")
    parser.add_argument("--name", default="rest-cohort-1")
    parser.add_argument("--stale-name")
    parser.add_argument("--multipart", action="store_true")
    parser.add_argument("--steps", type=int, default=100000)
    parser.add_argument("--path", choices=["rest", "mcp"], default="rest")
    parser.add_argument("--gpu-class", choices=["l40s", "rtx-pro-6000"], default="l40s")
    args = parser.parse_args()
    if args.deployment:
        configure(args.deployment)
    if args.action == "prepare":
        prepare(
            args.private_dir,
            args.tag,
            args.api_tag,
            automatic_recovery=args.automatic_recovery,
            storage_config=args.storage_config,
            storage_tenant=args.storage_tenant,
            delivery_retry=args.delivery_retry,
        )
    elif args.action == "register":
        register(args.private_dir)
    elif args.action == "submit":
        submit(args.private_dir, args.name, args.steps, args.path, benchmem_tpr=args.benchmem_tpr)
    elif args.action == "status":
        status(args.private_dir, args.name, args.path)
    elif args.action == "boundaries":
        boundaries(args.private_dir, args.name)
    elif args.action == "checkpoint":
        checkpoint(args.private_dir, args.name)
    elif args.action == "interrupt":
        interrupt(args.private_dir, args.name)
    elif args.action == "storage-denial":
        storage_denial(args.private_dir, args.name)
    elif args.action == "trajectories":
        trajectories(args.private_dir, args.name, args.engine_image)
    elif args.action == "identity-boundaries":
        identity_boundaries(args.private_dir, args.name, args.stale_name, multipart=args.multipart)
    elif args.action == "wait":
        wait_for(args.private_dir, args.name, args.path)
    elif args.action == "recovery-cycle":
        recovery_cycle(args.private_dir, args.name)
    elif args.action == "automatic-recovery-cycle":
        automatic_recovery_cycle(args.private_dir, args.name)
    else:
        control(args.private_dir, args.name, args.action, args.path, args.gpu_class)


if __name__ == "__main__":
    main()

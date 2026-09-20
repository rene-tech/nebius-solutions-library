"""Benchmark the existing HTTP submit + MCP artifact + pinned-reader client.

Input references and trial-derived idempotency survive an empty worker restart.
This qualifies dataset integrity, not physical action/video alignment.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from extra import ROOT, artifact_bytes, digest, module

CLIENT = module("benchmark_lerobot_client", ROOT / "acceptance/lerobot-customer-20260917/client.py")


def execute(trial, client, token, directory, timeout):
    started = time.monotonic()
    case = trial["case_spec"]
    fixture_id = str(UUID(case["fixture_ref"].removeprefix("artifact://")))
    response = client.get(f"/v1/artifacts/{fixture_id}/content")
    response.raise_for_status()
    if digest(response.content) != case["fixture_sha256"]:
        raise RuntimeError("fixture_digest_changed")
    fixture = response.json()
    if fixture["model_id"] != "cosmos3-lerobot-augmentation":
        raise RuntimeError("fixture_model_mismatch")
    request = fixture["request"]
    source = request["parameters"]["source"]
    reader = Path(os.environ.get("FS2_LEROBOT_READER", str(
        ROOT / "models/general-media/lerobot-augmentation/runtime/.venv/bin/python")))
    archive = directory / "source.tar.zst"
    if not (directory / "source").exists():
        archive.write_bytes(artifact_bytes(client, source))
        subprocess.run([str(reader), str(Path(__file__).with_name("lerobot_reader.py")),
                        str(archive), str(directory / "source"), source["sha256"]], check=True, timeout=120)
    (directory / "parameters.json").write_text(json.dumps(request["parameters"]))
    key = "benchmark-" + str(trial["id"])
    while True:
        response = client.post("/v1/models/cosmos3-lerobot-augmentation:submit", json=request,
                               headers={"Idempotency-Key": key})
        if response.status_code != 429 or time.monotonic() - started >= timeout:
            break
        time.sleep(15)
    response.raise_for_status()
    accepted = response.json()
    operation_id = CLIENT.harness.operation_id(accepted)
    (directory / (operation_id + "-admission.json")).write_text(json.dumps(accepted))
    while True:
        response = client.get("/v1/operations/" + operation_id)
        response.raise_for_status()
        status = response.json()
        operation = status["operation"]
        if operation["status"] in {"succeeded", "failed", "cancelled", "expired"}:
            break
        if time.monotonic() - started >= timeout:
            raise RuntimeError("operation_wait_deadline")
        time.sleep(5)
    (directory / (operation_id + "-status.json")).write_text(json.dumps(operation))
    if operation["status"] != "succeeded":
        raise RuntimeError("lerobot_operation_" + operation["status"])
    state = {"operation_id": operation_id, "request": request, "run_id": str(trial["id"]),
             "idempotency_key": key, "endpoint": str(client.base_url).rstrip("/")}
    args = SimpleNamespace(reader_python=reader, max_bytes=256 * 1024**2, max_expanded_bytes=256 * 1024**2)

    async def collect():
        async with CLIENT.connect(state["endpoint"], token, state, directory) as public:
            await CLIENT.collect_outputs(public, args)

    asyncio.run(collect())
    return {"operation_id": operation_id, "elapsed_seconds": time.monotonic() - started,
            "semantic_valid": True, "operations": [operation], "lerobot_receipt": state,
            "semantic": {"status": "PASS", "scope": "dataset-integrity-not-physical-alignment"}}

"""Exercise retained Cosmos cold activation and distinct real media outputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solution", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.receipt.exists():
        raise FileExistsError(
            "choose a new receipt path; previous evidence is preserved"
        )
    info = args.bundle.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        raise ValueError("access bundle must be an owner-only regular file")
    bundle = json.loads(args.bundle.read_text())
    token = bundle["credentials"]["inference_access_token"]
    origin = bundle["endpoints"]["admin_portal_url"].split("/admin")[0]
    fixture_path = args.solution / "catalog/runtime/validators/assets/cosmos3-nano.json"
    validator_path = (
        args.solution / "catalog/runtime/validators/validate_cosmos3_nano.py"
    )
    spec = importlib.util.spec_from_file_location("cosmos_validator", validator_path)
    if spec is None or spec.loader is None:
        raise ValueError("model semantic validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.load_contract(fixture_path)
    kubectl = [
        "kubectl",
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
    ]

    def snapshot() -> dict:
        deployments = json.loads(
            subprocess.check_output(
                kubectl + ["-n", "fs2-models", "get", "deployments", "-o", "json"]
            )
        )
        nodes = json.loads(
            subprocess.check_output(kubectl + ["get", "nodes", "-o", "json"])
        )
        return {
            "at": datetime.now(UTC).isoformat(),
            "deployments": [
                {
                    "name": item["metadata"]["name"],
                    "replicas": item["spec"].get("replicas", 0),
                    "ready": item.get("status", {}).get("readyReplicas", 0),
                    "images": [
                        c["image"]
                        for c in item["spec"]["template"]["spec"]["containers"]
                    ],
                }
                for item in deployments["items"]
                if "cosmos3-nano" in item["metadata"]["name"]
            ],
            "nodes": [
                {
                    "name": item["metadata"]["name"],
                    "uid": item["metadata"]["uid"],
                    "created": item["metadata"]["creationTimestamp"],
                    "labels": item["metadata"]["labels"],
                }
                for item in nodes["items"]
            ],
        }

    evidence = {
        "schema": "fs2-serve.nebius.ai/cosmos-media-acceptance/v1",
        "model": fixture["model"],
        "before": snapshot(),
        "requests": [],
        "result": "RUNNING",
    }
    prior = json.loads(args.resume_from.read_text()) if args.resume_from else None
    if prior is not None:
        if prior["model"] != fixture["model"]:
            raise ValueError("resume model identity differs")
        evidence["original_before"] = prior["before"]
        evidence["resumed_after_client_failure"] = True

    def save() -> None:
        encoded = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if token in encoded:
            raise ValueError("credential detected in receipt")
        descriptor = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(encoded)

    save()
    with httpx.Client(
        base_url=origin,
        headers={"authorization": "Bearer " + token},
        timeout=45,
        follow_redirects=False,
        trust_env=False,
    ) as client:

        def get_with_retry(path: str, record: dict) -> httpx.Response:
            for retry in range(6):
                try:
                    value = client.get(path)
                    if value.status_code not in {429, 502, 503, 504}:
                        return value
                    record.setdefault("transient_http_statuses", []).append(
                        value.status_code
                    )
                except httpx.TransportError as error:
                    record.setdefault("transient_transport_errors", []).append(
                        type(error).__name__
                    )
                if retry < 5:
                    time.sleep(3)
            raise TimeoutError("public endpoint retry budget exhausted")

        for repetition in range(args.repetitions):
            for request in fixture["requests"]:
                started = time.monotonic()
                payload = request["request"]
                record = {
                    "case": request["id"],
                    "repetition": repetition + 1,
                    "input_sha256": hashlib.sha256(
                        module.canonical_json(payload)
                    ).hexdigest(),
                }
                evidence["requests"].append(record)
                try:
                    if (
                        prior is not None
                        and repetition == 0
                        and request is fixture["requests"][0]
                    ):
                        previous = prior["requests"][0]
                        if previous["input_sha256"] != record["input_sha256"]:
                            raise ValueError("resume request identity differs")
                        record["resumed_operation"] = True
                        response = get_with_retry(
                            "/v1/operations/" + previous["operation_id"], record
                        )
                    else:
                        response = client.post(
                            "/v1/models/cosmos3-nano:invoke",
                            headers={
                                "x-fs2-wait-seconds": "0",
                                "Idempotency-Key": "cosmos-ready-" + uuid4().hex,
                            },
                            json={"operation": "generate-media", "payload": payload},
                        )
                    record["admission_http_status"] = response.status_code
                    response.raise_for_status()
                    operation = response.json()
                    operation_id = operation["id"]
                    record["operation_id"] = operation_id
                    save()
                    while operation["status"] not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                        "preempted",
                        "expired",
                    }:
                        if time.monotonic() - started > 2400:
                            client.post(f"/v1/operations/{operation_id}:cancel")
                            raise TimeoutError("operation deadline exceeded")
                        time.sleep(3)
                        response = get_with_retry(
                            f"/v1/operations/{operation_id}", record
                        )
                        response.raise_for_status()
                        operation = response.json()
                    record["operation"] = {
                        key: operation.get(key)
                        for key in (
                            "id",
                            "status",
                            "model_revision",
                            "accepted_at",
                            "activation_started_at",
                            "ready_at",
                            "started_at",
                            "completed_at",
                            "cold_start_seconds",
                            "runtime",
                            "error_code",
                            "attempt",
                        )
                    }
                    if operation["status"] != "succeeded":
                        raise ValueError("operation did not succeed")
                    record["operation_end_to_end_seconds"] = (
                        datetime.fromisoformat(operation["completed_at"])
                        - datetime.fromisoformat(operation["accepted_at"])
                    ).total_seconds()
                    response = get_with_retry(
                        f"/v1/operations/{operation_id}/result", record
                    )
                    response.raise_for_status()
                    result = response.json()
                    record["media"] = module.validate_response(
                        result, request, request["id"]
                    )
                    record["generation_timings_ms"] = result["timings_ms"]
                    record["passed"] = True
                except Exception as error:
                    record["passed"] = False
                    record["failure_type"] = type(error).__name__
                    if isinstance(error, httpx.HTTPStatusError):
                        record["failure_http_status"] = error.response.status_code
                record["end_to_end_seconds"] = round(time.monotonic() - started, 3)
                save()
                print(
                    json.dumps(
                        {
                            key: record[key]
                            for key in (
                                "case",
                                "repetition",
                                "passed",
                                "end_to_end_seconds",
                            )
                        }
                    ),
                    flush=True,
                )
                if not record["passed"]:
                    evidence["after"] = snapshot()
                    evidence["result"] = "FAIL"
                    save()
                    return 1
    evidence["after"] = snapshot()
    first_outputs = [
        item["media"]["sha256"]
        for item in evidence["requests"]
        if item["repetition"] == 1
    ]
    evidence["distinct_case_outputs"] = len(set(first_outputs)) == len(
        fixture["requests"]
    )
    evidence["result"] = "PASS" if evidence["distinct_case_outputs"] else "FAIL"
    save()
    return 0 if evidence["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

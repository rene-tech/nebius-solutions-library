"""Direct isolated-runtime attribution proof, not public admission/billing qualification.

Use two explicit Pod port-forwards. All HTTP response bytes and fresh Kubernetes
observations stay in the operator-selected private evidence directory. No keys,
admissions, workload changes or production model routing are performed here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jsonschema

from fs2_serve.models import ClaimedOperation, OperationStatus
from fs2_serve.runtime import RuntimeClient
from fs2_serve.runtime_kubernetes import KubernetesRuntimeMetadataProvider

MODEL = "nv-reason-cxr-3b"
TEST_MODEL = "fs2-cxr-attribution-20260918"
IMAGE = "sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635"
REVISION = "056bd0383b35226554da9dc5866e095df174ae19"
SERVICE = "fs2-cxr-attribution-20260918"


class Reader:
    def __init__(self, args):
        self.command = [
            "kubectl",
            "--kubeconfig",
            args.kubeconfig,
            "--context",
            args.context,
            "--request-timeout=20s",
        ]
        self.observations = []

    async def get(self, path):
        process = await asyncio.create_subprocess_exec(
            *self.command,
            "get",
            "--raw",
            path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
        if process.returncode:
            raise RuntimeError("bounded_kubernetes_read_failed")
        value = json.loads(stdout)
        self.observations.append(
            {"at": datetime.now(UTC).isoformat(), "path": path, "body": value}
        )
        return value

    async def list(self, path):
        return (await self.get(path))["items"]


class DebugSink:
    def __init__(self):
        self.exchanges = []

    async def record(self, exchange):
        self.exchanges.append(exchange.model_dump(mode="json"))


def operation():
    now = datetime.now(UTC)
    return ClaimedOperation(
        id=uuid4(),
        tenant_id="isolated-qualification",
        principal_id="local-candidate-probe",
        token_id=uuid4(),
        model_id=TEST_MODEL,
        model_revision=REVISION,
        protocol="openai-chat",
        operation="chat",
        idempotency_key="direct-runtime-not-public-admission",
        status=OperationStatus.RUNNING,
        accepted_at=now,
        available_at=now,
        deadline_at=now + timedelta(minutes=5),
        payload_expires_at=now + timedelta(hours=1),
        attempt=1,
        max_attempts=1,
        fencing_token=1,
        request_content_type="application/json",
        worker_id="isolated-local-probe",
    )


def model(origin):
    # Explicit harness binding to the isolated Service, not a fabricated response
    # or a published catalog qualification. Production registry is not mutated.
    return SimpleNamespace(
        id=TEST_MODEL,
        dynamic_policy=None,
        gateway=SimpleNamespace(model_revision=REVISION),
        binding=SimpleNamespace(
            backend_class="local-kubernetes",
            service_origin=origin,
            endpoints={"openai-chat": "/v1/chat/completions"},
            backend_service_name=SERVICE,
            backend_namespace="fs2-models",
            backend_port=8000,
            backend_runtime_image_digest=IMAGE,
        ),
    )


def combined(text):
    parts, finish, done = [], None, False
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        if line[6:] == "[DONE]":
            done = True
            continue
        value = json.loads(line[6:])
        for choice in value.get("choices", []):
            parts.append(choice.get("delta", {}).get("content") or "")
            finish = choice.get("finish_reason") or finish
    if not done or finish != "stop":
        raise ValueError("incomplete_stream")
    return json.loads("".join(parts))


async def run(args):
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    requests = [json.loads(path.read_bytes()) for path in args.requests]
    for request in requests:
        for key in ("idempotency_key", "wait_seconds"):
            request.pop(key, None)
        request["model"] = MODEL
    if requests[0]["messages"] == requests[1]["messages"]:
        raise ValueError("require_two_distinct_retained_requests")
    reader, summary, seen = Reader(args), [], set()
    provider = KubernetesRuntimeMetadataProvider(reader)
    pods = await reader.list("/api/v1/namespaces/fs2-models/pods")
    candidates = [
        p
        for p in pods
        if p["metadata"].get("labels", {}).get("fs2-serve.nebius.ai/attribution-test")
        == "cxr-20260918"
    ]
    if len(candidates) != 2 or not all(
        any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in p["status"].get("conditions", [])
        )
        for p in candidates
    ):
        raise ValueError("requires_two_actual_ready_candidate_pods")
    expected_uids = {p["metadata"]["uid"] for p in candidates}
    for index, origin in enumerate(args.origins):
        for mode in ("json", "stream", "error"):
            request = requests[1 if mode == "stream" else 0]
            op, sink = operation(), DebugSink()
            body = dict(request, stream=mode == "stream")
            if mode == "error":
                body["model"] = "nonexistent-isolated-qualification-model"
            async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
                runtime = RuntimeClient(
                    activation_timeout_seconds=300,
                    runtime_timeout_seconds=300,
                    max_response_bytes=8 * 1024 * 1024,
                    client=client,
                    metadata_provider=provider,
                    debug_store=sink,
                )
                if mode != "stream":
                    result = await runtime.invoke(
                        model(origin), op, json.dumps(body).encode()
                    )
                    identity = result.runtime
                    if mode == "json":
                        payload = json.loads(result.body)
                        if payload["choices"][0]["finish_reason"] != "stop":
                            raise ValueError("incomplete_json")
                        jsonschema.validate(
                            json.loads(payload["choices"][0]["message"]["content"]),
                            request["response_format"]["json_schema"]["schema"],
                        )
                        assert result.status_code == 200
                    else:
                        assert 400 <= result.status_code < 500 and result.body == b""
                    receipt = {
                        "status": result.status_code,
                        "runtime": identity.model_dump(mode="json"),
                        "lifecycle": result.lifecycle.model_dump(mode="json")
                        if result.lifecycle
                        else None,
                        "exchanges": sink.exchanges,
                    }
                else:
                    async with client.stream(
                        "POST",
                        origin + "/v1/chat/completions",
                        json=body,
                        headers=runtime._correlation_headers(op),
                    ) as response:
                        response.raise_for_status()
                        chunks = [chunk async for chunk in response.aiter_bytes()]
                        jsonschema.validate(
                            combined(b"".join(chunks).decode()),
                            request["response_format"]["json_schema"]["schema"],
                        )
                        (
                            identity,
                            lifecycle,
                        ) = await runtime._trusted_runtime_observation(
                            op, model(origin), response
                        )
                        receipt = {
                            "status": response.status_code,
                            "headers": list(response.headers.multi_items()),
                            "body": b"".join(chunks).decode(),
                            "chunks": len(chunks),
                            "runtime": identity.model_dump(mode="json"),
                            "lifecycle": lifecycle.model_dump(mode="json")
                            if lifecycle
                            else None,
                        }
            if identity.pod_uid not in expected_uids or identity.node_uid is None:
                raise ValueError("exact_pod_and_node_attribution_missing")
            # No substitution when the node observer has not supplied GPU UUIDs.
            seen.add(identity.pod_uid)
            row = {
                "replica": index,
                "mode": mode,
                "operation_id": str(op.id),
                "runtime": identity.model_dump(mode="json"),
                "passed": True,
            }
            path = args.output / f"replica-{index}-{mode}.json"
            path.write_text(
                json.dumps(
                    {"request": body, "response": receipt, "receipt": row}, indent=2
                )
                + "\n"
            )
            row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            summary.append(row)
            print(json.dumps(row), flush=True)
    if seen != expected_uids:
        raise ValueError("did_not_observe_both_actual_replicas")
    (args.output / "kubernetes.json").write_text(
        json.dumps(reader.observations, indent=2) + "\n"
    )
    (args.output / "summary.json").write_text(
        json.dumps(
            {
                "scope": "direct isolated real-GPU responses; not public admission/billing",
                "all_passed": True,
                "replicas": sorted(seen),
                "rows": summary,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origins", nargs=2, required=True)
    parser.add_argument("--requests", type=Path, nargs=2, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    asyncio.run(run(parser.parse_args()))

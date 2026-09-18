from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from test_runtime_and_schema import claimed
from test_runtime_lifecycle_attribution import FakeReader, _iso, _pod

from fs2_serve.models import RuntimeIdentity
from fs2_serve.runtime import RuntimeClient
from fs2_serve.runtime_kubernetes import (
    GPU_ALLOCATION_OBSERVED_AT_ANNOTATION,
    GPU_UUIDS_ANNOTATION,
    RESPONSE_IDENTITY_ANNOTATION,
    KubernetesRuntimeMetadataProvider,
)

POD = str(UUID(int=1))
OTHER = str(UUID(int=2))
IMAGE = "sha256:" + "a" * 64
REVISION = "model-revision"
ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "fs2_identity_test", ROOT / "models/general-media/fs2_runtime_identity.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Reader(FakeReader):
    def __init__(self):
        pods = []
        for index, uid in enumerate((POD, OTHER)):
            pod = _pod(
                "qwen3-8b",
                uid=uid,
                annotations={
                    RESPONSE_IDENTITY_ANNOTATION: "asgi-v1",
                    "fs2.nebius/model-revision": REVISION,
                    "fs2.nebius/runtime-image-digest": IMAGE,
                    GPU_UUIDS_ANNOTATION: json.dumps([f"GPU-exact-{index}"]),
                    GPU_ALLOCATION_OBSERVED_AT_ANNOTATION: _iso(12),
                },
            )
            pod["metadata"]["name"] += f"-{index}"
            container = pod["spec"]["containers"][0]
            container.update(
                image="registry/runtime@" + IMAGE,
                args=["--middleware", "fs2_runtime_identity.RuntimeIdentityMiddleware"],
                ports=[{"name": "http", "containerPort": 8000}],
                env=[{"name": "FS2_RUNTIME_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}],
            )
            pod["status"]["podIP"] = f"10.0.0.{index + 1}"
            pod["status"]["containerStatuses"][0]["imageID"] = "registry/runtime@" + IMAGE
            pods.append(pod)
        super().__init__(pods=pods)
        self.services = [
            {
                "metadata": {"name": "qwen3-8b", "uid": "service-uid"},
                "spec": {
                    "selector": {"fs2-serve.nebius.ai/model-id": "qwen3-8b"},
                    "ports": [{"port": 8000, "targetPort": "http"}],
                },
            }
        ]
        self.slices = [
            {
                "metadata": {
                    "labels": {"kubernetes.io/service-name": "qwen3-8b"},
                    "ownerReferences": [{"kind": "Service", "uid": "service-uid"}],
                },
                "ports": [{"port": 8000}],
                "endpoints": [
                    {
                        "targetRef": {
                            "kind": "Pod",
                            "name": p["metadata"]["name"],
                            "namespace": "fs2-models",
                            "uid": p["metadata"]["uid"],
                        },
                        "conditions": {"ready": True},
                        "addresses": [p["status"]["podIP"]],
                    }
                    for p in pods
                ],
            }
        ]

    async def list(self, path):
        if path.endswith("/services"):
            return self.services
        if path.endswith("/endpointslices"):
            return self.slices
        return await super().list(path)


async def resolve(reader, **updates):
    args = dict(
        operation_id=uuid4(),
        model_id="qwen3-8b",
        pod_uid=POD,
        service_name="qwen3-8b",
        service_namespace="fs2-models",
        service_port=8000,
        runtime_image_digest=IMAGE,
        model_revision=REVISION,
    )
    return await KubernetesRuntimeMetadataProvider(reader).resolve_response_lifecycle(**(args | updates))


@pytest.mark.asyncio
async def test_two_ready_replicas_use_response_uid_and_observer_not_random_candidate():
    reader = Reader()
    assert (
        await KubernetesRuntimeMetadataProvider(reader).resolve_lifecycle(operation_id=uuid4(), model_id="qwen3-8b")
        is None
    )
    for index, uid in enumerate((POD, OTHER)):
        observed = await resolve(reader, pod_uid=uid)
        assert observed.runtime.pod_uid == uid
        assert observed.runtime.gpu_uuids == [f"GPU-exact-{index}"]
    reader.pods[0]["metadata"]["annotations"].pop(GPU_UUIDS_ANNOTATION)
    observed = await resolve(reader)
    assert observed.runtime.pod_uid == POD and observed.runtime.gpu_count == 1
    assert observed.runtime.gpu_uuids == [] and observed.device_allocation_observed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "uid",
        "namespace",
        "revision",
        "image",
        "image_id",
        "selector",
        "service_uid",
        "slice_service",
        "target_uid",
        "target_ip",
        "target_namespace",
        "not_ready",
        "terminating",
        "pod_deleting",
        "pod_not_ready",
        "unmarked",
        "no_middleware",
        "env_from_request",
        "port",
        "slice_port",
        "model",
        "duplicate_pod",
    ],
)
async def test_mismatched_or_untrusted_endpoint_never_attributes(fault):
    reader, update = Reader(), {}
    pod = reader.pods[0]
    endpoint = reader.slices[0]["endpoints"][0]
    if fault in {"uid", "namespace", "revision", "image", "model"}:
        update[
            {
                "uid": "pod_uid",
                "namespace": "service_namespace",
                "revision": "model_revision",
                "image": "runtime_image_digest",
                "model": "model_id",
            }[fault]
        ] = "wrong"
    elif fault == "image_id":
        pod["status"]["containerStatuses"][0]["imageID"] = "wrong"
    elif fault == "selector":
        reader.services[0]["spec"]["selector"] = {"different": "model"}
    elif fault == "service_uid":
        reader.services[0]["metadata"]["uid"] = "new-service"
    elif fault == "slice_service":
        reader.slices[0]["metadata"]["labels"]["kubernetes.io/service-name"] = "other"
    elif fault == "target_uid":
        endpoint["targetRef"]["uid"] = OTHER
    elif fault == "target_ip":
        endpoint["addresses"] = ["10.99.99.99"]
    elif fault == "target_namespace":
        endpoint["targetRef"]["namespace"] = "other"
    elif fault == "not_ready":
        endpoint["conditions"]["ready"] = False
    elif fault == "terminating":
        endpoint["conditions"]["terminating"] = True
    elif fault == "pod_deleting":
        pod["metadata"]["deletionTimestamp"] = _iso(200)
    elif fault == "pod_not_ready":
        pod["status"]["conditions"][1]["status"] = "False"
    elif fault == "unmarked":
        pod["metadata"]["annotations"].pop(RESPONSE_IDENTITY_ANNOTATION)
    elif fault == "no_middleware":
        pod["spec"]["containers"][0]["args"] = []
    elif fault == "env_from_request":
        pod["spec"]["containers"][0]["env"][0] = {"name": "FS2_RUNTIME_POD_UID", "value": POD}
    elif fault == "port":
        reader.services[0]["spec"]["ports"][0]["targetPort"] = 9999
    elif fault == "slice_port":
        reader.slices[0]["ports"][0]["port"] = 9999
    elif fault == "duplicate_pod":
        reader.pods.append(pod)
    assert await resolve(reader, **update) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 422])
async def test_real_runtime_client_asgi_middleware_and_two_pod_verifier(registry, monkeypatch, status):
    monkeypatch.setenv("FS2_RUNTIME_POD_UID", OTHER)

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-fs2-runtime-pod-uid", POD.encode()),
                    (b"x-fs2-gpu-count", b"99"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}',
                "more_body": False,
            }
        )

    model = registry.get("qwen3-8b")
    model = replace(
        model,
        gateway=replace(
            model.gateway, model_revision=REVISION, binding=replace(model.binding, backend_runtime_image_digest=IMAGE)
        ),
    )
    operation = claimed(registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.RuntimeIdentityMiddleware(app))) as client:
        runtime = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=10000,
            client=client,
            metadata_provider=KubernetesRuntimeMetadataProvider(Reader()),
        )
        result = await runtime.invoke(model, operation, b"{}")
    assert result.runtime.pod_uid == OTHER and result.runtime.gpu_uuids == ["GPU-exact-1"]
    assert result.status_code == status
    assert (result.body != b"") is (status == 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_operation", "wrong_attempt", "malformed_uid", "duplicate", "partial"])
async def test_bad_response_hints_do_not_fallback_to_singleton_or_fail_inference(registry, fault):
    operation = claimed(registry)
    headers = [
        ("content-type", "application/json"),
        ("x-fs2-runtime-pod-uid", POD),
        ("x-fs2-runtime-operation-id", str(operation.id)),
        ("x-fs2-runtime-attempt", str(operation.attempt)),
    ]
    if fault == "wrong_operation":
        headers[2] = (headers[2][0], str(uuid4()))
    if fault == "wrong_attempt":
        headers[3] = (headers[3][0], "99")
    if fault == "malformed_uid":
        headers[1] = (headers[1][0], "not-a-uid")
    if fault == "duplicate":
        headers.append(headers[1])
    if fault == "partial":
        headers.pop()
    reader = Reader()
    reader.pods = reader.pods[:1]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers=headers, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
            )
        )
    ) as client:
        runtime = RuntimeClient(
            activation_timeout_seconds=2,
            runtime_timeout_seconds=2,
            max_response_bytes=10000,
            client=client,
            metadata_provider=KubernetesRuntimeMetadataProvider(reader),
        )
        result = await runtime.invoke(registry.get("qwen3-8b"), operation, b"{}")
    assert result.status_code == 200 and result.runtime == RuntimeIdentity()


@pytest.mark.asyncio
async def test_middleware_streams_each_chunk_without_buffer_and_preserves_cancellation(monkeypatch):
    monkeypatch.setenv("FS2_RUNTIME_POD_UID", POD)
    delivered = []
    operation = str(uuid4()).encode()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert delivered[-1]["body"] == b"first"
        raise asyncio.CancelledError()

    async def send(message):
        delivered.append(message)

    with pytest.raises(asyncio.CancelledError):
        await module.RuntimeIdentityMiddleware(app)(
            {"type": "http", "headers": [(b"x-fs2-operation-id", operation), (b"x-request-id", operation + b":2")]},
            None,
            send,
        )
    assert dict(delivered[0]["headers"])[b"x-fs2-runtime-attempt"] == b"2"
    assert len(delivered) == 2

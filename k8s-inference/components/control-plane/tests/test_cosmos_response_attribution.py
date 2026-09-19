"""Real Cosmos adapter response -> RuntimeClient -> exact same-Pod attribution."""
from __future__ import annotations

import ast
import copy
import json
import sys
from dataclasses import replace
from types import ModuleType
from uuid import uuid4

import httpx
import pytest
import yaml
from test_cosmos_native_runtime import MP4, model_for
from test_runtime_and_schema import claimed
from test_runtime_response_attribution import IMAGE, OTHER, POD, REVISION, ROOT, Reader

from fs2_serve.models import RuntimeIdentity
from fs2_serve.runtime import RuntimeClient
from fs2_serve.runtime_kubernetes import (
    COSMOS_RESPONSE_IDENTITY_VERSION,
    GPU_UUIDS_ANNOTATION,
    RESPONSE_IDENTITY_ANNOTATION,
    KubernetesRuntimeMetadataProvider,
)

MANIFEST = ROOT / "models/general-media/k8s/cosmos3-nano.yaml"


def adapter_module(monkeypatch, uid):
    monkeypatch.setenv("FS2_RUNTIME_POD_UID", uid)
    source = next(d["data"]["adapter.py"] for d in yaml.safe_load_all(MANIFEST.read_text())
                  if d and d.get("kind") == "ConfigMap" and "adapter.py" in d.get("data", {}))
    module = ModuleType("cosmos_identity_" + uid.replace("-", ""))
    sys.modules[module.__name__] = module
    exec(compile(source, str(MANIFEST) + "#adapter.py", "exec"), module.__dict__)  # noqa: S102
    return module, source


class CosmosReader(Reader):
    def __init__(self):
        super().__init__()
        for pod in self.pods:
            pod["metadata"]["labels"]["fs2-serve.nebius.ai/model-id"] = "cosmos3-nano"
            pod["metadata"]["annotations"][RESPONSE_IDENTITY_ANNOTATION] = COSMOS_RESPONSE_IDENTITY_VERSION
            gpu = pod["spec"]["containers"][0]
            gpu["name"] = "vllm-omni"
            gpu["args"] = ["serve", "--port", "8000"]
            gpu["ports"] = [{"name": "upstream", "containerPort": 8000}]
            adapter = copy.deepcopy(gpu)
            adapter.update(name="bounded-json-adapter", command=["python3", "/adapter/adapter.py"], args=[],
                           ports=[{"name": "adapter", "containerPort": 8080}],
                           resources={"requests": {"cpu": "250m"}, "limits": {"cpu": "2"}})
            pod["spec"]["containers"].append(adapter)
            state = pod["status"]["containerStatuses"][0]
            state["name"] = "vllm-omni"
            pod["status"]["containerStatuses"].append({**copy.deepcopy(state), "name": "bounded-json-adapter"})
        self.services[0]["metadata"]["name"] = "cosmos3-nano"
        self.services[0]["spec"] = {"selector": {"fs2-serve.nebius.ai/model-id": "cosmos3-nano"},
                                    "ports": [{"port": 8080, "targetPort": "adapter"}]}
        self.slices[0]["metadata"]["labels"]["kubernetes.io/service-name"] = "cosmos3-nano"
        self.slices[0]["ports"] = [{"port": 8080}]


async def resolve(reader, uid=POD):
    return await KubernetesRuntimeMetadataProvider(reader).resolve_response_lifecycle(
        operation_id=uuid4(), model_id="cosmos3-nano", pod_uid=uid, service_name="cosmos3-nano",
        service_namespace="fs2-models", service_port=8080, runtime_image_digest=IMAGE, model_revision=REVISION)


def test_adapter_bundles_exact_shared_identity_middleware_and_local_upstream(monkeypatch):
    adapter, source = adapter_module(monkeypatch, POD)
    shared = (ROOT / "models/general-media/fs2_runtime_identity.py").read_text()
    def definition(text):
        return next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef)
                    and n.name == "RuntimeIdentityMiddleware")
    assert ast.dump(definition(source)) == ast.dump(definition(shared))
    assert adapter.UPSTREAM_BASE_URL == "http://127.0.0.1:8000"
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    pod = next(d for d in docs if d.get("kind") == "Deployment")["spec"]["template"]
    assert pod["metadata"]["annotations"][RESPONSE_IDENTITY_ANNOTATION] == COSMOS_RESPONSE_IDENTITY_VERSION
    container = next(c for c in pod["spec"]["containers"] if c["name"] == "bounded-json-adapter")
    env = next(e for e in container["env"] if e["name"] == "FS2_RUNTIME_POD_UID")
    assert env == {"name": "FS2_RUNTIME_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}


@pytest.mark.asyncio
async def test_two_cosmos_replicas_without_instrumentation_remain_unknown():
    reader = CosmosReader()
    observed = await KubernetesRuntimeMetadataProvider(reader).resolve_lifecycle(
        operation_id=uuid4(), model_id="cosmos3-nano")
    assert observed is None  # Historical successful operation is not backfilled.


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [POD, OTHER])
async def test_exact_response_pod_with_two_ready_replicas_and_no_inferred_gpu_uuid(uid):
    reader = CosmosReader()
    observation = await resolve(reader, uid)
    assert observation.runtime.pod_uid == uid
    assert observation.runtime.gpu_count == 1
    index = 0 if uid == POD else 1
    assert observation.runtime.gpu_uuids == [f"GPU-exact-{index}"]
    reader.pods[index]["metadata"]["annotations"].pop(GPU_UUIDS_ANNOTATION)
    observation = await resolve(reader, uid)
    assert observation.runtime.gpu_uuids == []
    assert observation.device_allocation_observed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["gpu_absent", "gpu_image", "gpu_image_id", "gpu_port", "gpu_allocation",
                                      "adapter_image", "adapter_image_id", "adapter_command", "adapter_gpu",
                                      "adapter_uid_env", "adapter_annotation", "endpoint_uid", "endpoint_port"])
async def test_adapter_must_bind_same_pod_actual_gpu_and_service_endpoint(fault):
    reader = CosmosReader()
    pod = reader.pods[0]
    gpu, adapter = pod["spec"]["containers"]
    if fault == "gpu_absent":
        pod["spec"]["containers"] = [adapter]
    elif fault == "gpu_image":
        gpu["image"] = "different@sha256:" + "b" * 64
    elif fault == "gpu_image_id":
        pod["status"]["containerStatuses"][0]["imageID"] = "wrong"
    elif fault == "gpu_port":
        gpu["ports"][0]["containerPort"] = 9000
    elif fault == "gpu_allocation":
        gpu["resources"] = {}
    elif fault == "adapter_image":
        adapter["image"] = "different@sha256:" + "b" * 64
    elif fault == "adapter_image_id":
        pod["status"]["containerStatuses"][1]["imageID"] = "wrong"
    elif fault == "adapter_command":
        adapter["command"] = ["python3", "/different.py"]
    elif fault == "adapter_gpu":
        adapter["resources"] = {"requests": {"nvidia.com/gpu": "1"}, "limits": {"nvidia.com/gpu": "1"}}
    elif fault == "adapter_uid_env":
        adapter["env"] = [{"name": "FS2_RUNTIME_POD_UID", "value": POD}]
    elif fault == "adapter_annotation":
        pod["metadata"]["annotations"][RESPONSE_IDENTITY_ANNOTATION] = "asgi-v1"
    elif fault == "endpoint_uid":
        reader.slices[0]["endpoints"][0]["targetRef"]["uid"] = OTHER
    elif fault == "endpoint_port":
        reader.slices[0]["ports"][0]["port"] = 8000
    assert await resolve(reader) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [POD, OTHER])
@pytest.mark.parametrize("outcome", ["mp4", "upstream_error", "validation_error"])
async def test_real_adapter_runtime_client_preserve_binary_errors_and_verified_identity(
    registry, monkeypatch, uid, outcome,
):
    adapter, _ = adapter_module(monkeypatch, uid)
    calls = []
    async def generated(client, request):
        calls.append(request.mode)
        if outcome == "upstream_error":
            raise httpx.ConnectError("upstream unavailable")
        return MP4, "video/mp4"
    monkeypatch.setattr(adapter, "generate_video", generated)
    # Model/ffmpeg alignment is covered by the media adapter suite. This real
    # one-frame MP4 is the compact RuntimeClient structural-validation fixture.
    monkeypatch.setattr(adapter, "verified_video_output", lambda raw, _: raw)
    adapter.app.state.client = object()
    model = model_for(registry)
    model = replace(model, gateway=replace(model.gateway, model_revision=REVISION,
                    binding=replace(model.binding, backend_service_name="cosmos3-nano", backend_port=8080,
                                    backend_runtime_image_digest=IMAGE)))
    operation = claimed(registry).model_copy(update={"model_id": model.id, "protocol": "native"})
    body = {"mode": "text-to-video", "prompt": "A blue cube.", "output_delivery": "artifact"}
    if outcome == "validation_error":
        body["mode"] = "invalid"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=adapter.app)) as client:
        result = await RuntimeClient(activation_timeout_seconds=2, runtime_timeout_seconds=2,
                        max_response_bytes=10000, client=client,
                        metadata_provider=KubernetesRuntimeMetadataProvider(CosmosReader())).invoke(
                            model, operation, json.dumps(body).encode())
    assert result.runtime.pod_uid == uid
    assert result.runtime.gpu_uuids == ["GPU-exact-0" if uid == POD else "GPU-exact-1"]
    assert result.runtime != RuntimeIdentity()
    assert result.status_code == {"mp4": 200, "upstream_error": 502, "validation_error": 422}[outcome]
    if outcome == "mp4":
        assert result.body == MP4 and result.semantic_outcome == "protocol_valid"
    else:
        assert result.body == b""  # Existing public error contract is unchanged.
    assert len(calls) == (0 if outcome == "validation_error" else 1)

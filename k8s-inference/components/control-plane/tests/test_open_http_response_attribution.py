"""Exact same-GPU-container identity for the bounded stdlib HTTP wrapper."""

from __future__ import annotations

import copy
import http.client
import importlib.util
import json
import threading
from dataclasses import replace

import httpx
import pytest
from test_runtime_and_schema import claimed
from test_runtime_response_attribution import (
    IMAGE,
    OTHER,
    POD,
    REVISION,
    ROOT,
    Reader,
    resolve,
)

from fs2_serve.runtime import RuntimeClient
from fs2_serve.runtime_kubernetes import (
    GPU_UUIDS_ANNOTATION,
    OPEN_HTTP_RESPONSE_IDENTITY_VERSION,
    RESPONSE_IDENTITY_ANNOTATION,
    KubernetesRuntimeMetadataProvider,
)


class OpenReader(Reader):
    def __init__(self):
        super().__init__()
        for pod in self.pods:
            pod["metadata"]["annotations"][RESPONSE_IDENTITY_ANNOTATION] = OPEN_HTTP_RESPONSE_IDENTITY_VERSION
            runtime = pod["spec"]["containers"][0]
            runtime["command"] = ["python3", "/opt/fs2/runtime/common/server.py"]
            runtime["args"] = []
            runtime["env"].append({"name": "FS2_PORT", "value": "8000"})


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [POD, OTHER])
async def test_exact_response_selects_gpu_replica_without_inventing_gpu_uuid(uid):
    reader = OpenReader()
    observed = await resolve(reader, pod_uid=uid)
    assert observed.runtime.pod_uid == uid
    assert observed.runtime.gpu_count == 1
    index = 0 if uid == POD else 1
    assert observed.runtime.gpu_uuids == [f"GPU-exact-{index}"]
    reader.pods[index]["metadata"]["annotations"].pop(GPU_UUIDS_ANNOTATION)
    observed = await resolve(reader, pod_uid=uid)
    assert observed.runtime.pod_uid == uid
    assert observed.runtime.gpu_uuids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "command",
        "args",
        "no_gpu",
        "cpu_relay",
        "second_gpu",
        "uid_env",
        "image",
        "image_id",
        "revision",
        "service",
        "endpoint",
        "port",
        "listener",
        "duplicate_listener",
        "annotation",
    ],
)
async def test_http_version_cannot_attribute_different_worker_or_unverified_route(fault):
    reader = OpenReader()
    pod = reader.pods[0]
    container = pod["spec"]["containers"][0]
    if fault == "command":
        container["command"] = ["python3", "/different.py"]
    elif fault == "args":
        container["args"] = ["different.py"]
    elif fault == "no_gpu":
        container["resources"] = {}
    elif fault in {"cpu_relay", "second_gpu"}:
        second = copy.deepcopy(container)
        second.update(name="another-worker", command=["python3", "/other.py"])
        pod["spec"]["containers"].append(second)
        if fault == "cpu_relay":
            container["resources"] = {}
    elif fault == "uid_env":
        container["env"][0] = {"name": "FS2_RUNTIME_POD_UID", "value": POD}
    elif fault == "image":
        container["image"] = "different@sha256:" + "b" * 64
    elif fault == "image_id":
        pod["status"]["containerStatuses"][0]["imageID"] = "different"
    elif fault == "revision":
        pod["metadata"]["annotations"]["fs2.nebius/model-revision"] = "different"
    elif fault == "service":
        reader.services[0]["spec"]["selector"] = {"different": "model"}
    elif fault == "endpoint":
        reader.slices[0]["endpoints"][0]["targetRef"]["uid"] = OTHER
    elif fault == "port":
        reader.services[0]["spec"]["ports"][0]["targetPort"] = 9000
    elif fault == "listener":
        container["env"][-1]["value"] = "9000"
    elif fault == "duplicate_listener":
        container["env"].append({"name": "FS2_PORT", "value": "8000"})
    elif fault == "annotation":
        pod["metadata"]["annotations"][RESPONSE_IDENTITY_ANNOTATION] = "asgi-v1"
    assert await resolve(reader) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [POD, OTHER])
@pytest.mark.parametrize("fail", [False, True])
async def test_real_http_response_runtime_client_and_two_replica_verifier(registry, monkeypatch, uid, fail):
    source = ROOT / "models/structure/runtime/common/server.py"
    spec = importlib.util.spec_from_file_location("open_http_identity_integration", source)
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    assert wrapper.RESPONSE_IDENTITY_VERSION == OPEN_HTTP_RESPONSE_IDENTITY_VERSION
    monkeypatch.setenv("FS2_RUNTIME_POD_UID", uid)

    class Adapter:
        paths = native_response_paths = {"/v1/chat/completions"}

        def infer(self, request):
            if fail:
                raise ValueError("bounded fixture error")
            return {"choices": [{"message": {"content": "unchanged"}, "finish_reason": "stop"}]}

        def render_native_response(self, path, request, output):
            return output

    wrapper.STATE.adapter = Adapter()
    wrapper.STATE.load_state = "ready"
    httpd = wrapper.BoundedHTTPServer(("127.0.0.1", 0), wrapper.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    async def transport(request):
        connection = http.client.HTTPConnection(*httpd.server_address, timeout=2)
        try:
            connection.request(request.method, request.url.path, body=request.content, headers=dict(request.headers))
            response = connection.getresponse()
            return httpx.Response(response.status, headers=response.getheaders(), content=response.read())
        finally:
            connection.close()

    model = registry.get("qwen3-8b")
    model = replace(
        model,
        gateway=replace(
            model.gateway,
            model_revision=REVISION,
            binding=replace(
                model.binding, backend_service_name="qwen3-8b", backend_port=8000, backend_runtime_image_digest=IMAGE
            ),
        ),
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            result = await RuntimeClient(
                activation_timeout_seconds=2,
                runtime_timeout_seconds=2,
                max_response_bytes=10000,
                client=client,
                metadata_provider=KubernetesRuntimeMetadataProvider(OpenReader()),
            ).invoke(model, claimed(registry), b'{"messages":[{"role":"user","content":"fixture"}]}')
        assert result.status_code == (400 if fail else 200)
        assert result.runtime.pod_uid == uid
        assert result.runtime.gpu_uuids == ["GPU-exact-0" if uid == POD else "GPU-exact-1"]
        if not fail:
            assert json.loads(result.body)["choices"][0]["message"]["content"] == "unchanged"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)

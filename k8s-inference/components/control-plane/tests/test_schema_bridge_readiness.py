from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fs2_serve.schema_bridge_readiness import KubernetesSchemaBridgeReader

BRIDGE = "registry.nebius.cloud/unit/control-plane@sha256:" + "8" * 64
PREDECESSOR = "registry.nebius.cloud/unit/control-plane@sha256:" + "7" * 64


def _reader(tmp_path: Path) -> KubernetesSchemaBridgeReader:
    token = tmp_path / "token"
    token.write_text("t" * 64, encoding="utf-8")
    return KubernetesSchemaBridgeReader(
        base_url="https://kubernetes.default.svc",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        namespace="fs2-system",
        deployment_name="fs2-serve-control-plane",
        release_name="fs2-serve-control-plane",
        bridge_image_ref=BRIDGE,
        predecessor_image_ref=PREDECESSOR,
    )


def _deployment() -> dict[str, object]:
    return {
        "metadata": {"uid": "deployment-uid", "resourceVersion": "41", "generation": 9},
        "spec": {"replicas": 2},
        "status": {
            "observedGeneration": 9,
            "updatedReplicas": 2,
            "readyReplicas": 2,
            "availableReplicas": 2,
        },
    }


def _pods(image: str = BRIDGE) -> dict[str, object]:
    return {
        "metadata": {},
        "items": [
            {
                "metadata": {"name": f"gateway-{index}", "uid": f"pod-{index}", "resourceVersion": "5"},
                "spec": {
                    "initContainers": [{"name": "wait-schema", "image": image}],
                    "containers": [{"name": "control-plane", "image": image}],
                },
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            }
            for index in range(2)
        ],
    }


@pytest.mark.asyncio
async def test_bridge_drain_evidence_binds_stable_deployment_pods_and_api_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter((_deployment(), _pods(), _deployment()))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(next(responses)).encode(),
            headers={
                "content-type": "application/json",
                "date": "Thu, 17 Sep 2026 12:00:00 GMT",
                "audit-id": "audit-1234",
            },
            request=request,
        )

    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    evidence = await _reader(tmp_path).verify()

    assert evidence.deployment_generation == evidence.deployment_observed_generation == 9
    assert evidence.runtime_pod_count == 2
    assert evidence.deployment_available_replicas == 2
    assert len(evidence.runtime_pod_set_digest) == 64
    assert evidence.kubernetes_audit_id == "audit-1234:audit-1234"
    assert evidence.kubernetes_observed_at.isoformat() == "2026-09-17T12:00:00+00:00"


@pytest.mark.asyncio
async def test_bridge_drain_rejects_any_predecessor_runtime_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter((_deployment(), _pods(PREDECESSOR)))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=next(responses),
            headers={"date": "Thu, 17 Sep 2026 12:00:00 GMT", "audit-id": "audit-1234"},
            request=request,
        )

    original = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    with pytest.raises(RuntimeError, match="exact bridge image"):
        await _reader(tmp_path).verify()

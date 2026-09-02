from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/model_autoscaling_acceptance.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "model_autoscaling_acceptance", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_localization_observation_retains_only_bounded_cache_result() -> None:
    module = _module()

    class FakeKubectl:
        def logs(self, namespace: str, pod: str, container: str) -> str:
            assert (namespace, pod, container) == (
                "fs2-models",
                "qwen3-8b-b300-test",
                "localize-model",
            )
            return (
                '2026-09-02T10:00:00Z {"outcome":"cache-hit",'
                '"elapsed_seconds":0.41,"repository":"must-not-leak"}\n'
            )

    observation = module._capture_localization_observation(
        FakeKubectl(),
        "fs2-models",
        {
            "metadata": {"name": "qwen3-8b-b300-test"},
            "spec": {"initContainers": [{"name": "localize-model"}]},
        },
    )

    assert observation == {
        "state": "observed",
        "reason": None,
        "observations": [
            {
                "container": "localize-model",
                "outcome": "cache-hit",
                "source": None,
                "elapsed_seconds": 0.41,
            }
        ],
    }
    assert "must-not-leak" not in json.dumps(observation)
    module._require_localization_outcome(observation, "cache-hit")


def test_public_receipt_hashes_runtime_pool_capacity_and_operation_identities() -> None:
    module = _module()
    private = {
        "schema": "fs2-serve.nebius.ai/model-autoscaling-acceptance/v1",
        "model_id": "qwen3-8b",
        "target": {
            "namespace": "fs2-models",
            "deployment": "qwen3-8b-b300",
            "service": "qwen3-8b-b300",
            "expected_floor": 0,
        },
        "result": "PASS",
        "clock": {"kind": "linux-monotonic", "domain": "private-boot-id"},
        "operation": {
            "id": "0198ca75-59c0-7c0e-8f8e-4d19b5ee8ccc",
            "status": "succeeded",
            "runtime": {
                "pod_uid": "private-pod-uid",
                "node_uid": "private-node-uid",
                "gpu_uuids": ["GPU-private"],
                "gpu_count": 1,
                "preemptible": True,
            },
        },
        "semantic_calls": [
            {
                "ordinal": 1,
                "operation_id": "0198ca75-59c0-7c0e-8f8e-4d19b5ee8ccc",
                "result_sha256": "a" * 64,
            }
        ],
        "observed_runtime_identities": {
            "pod_uids": ["private-pod-uid"],
            "node_uids": ["private-node-uid"],
        },
        "runtime_identity_observation": {
            "pod": {
                "name": "private-pod-name",
                "uid": "private-pod-uid",
                "node_name": "private-node-name",
            },
            "node": {
                "metadata": {
                    "name": "private-node-name",
                    "uid": "private-node-uid",
                    "labels": {
                        "capacity.fs2.nebius/pool-id": "private-pool-id",
                        "accelerator.fs2.nebius/pool-id": "private-accelerator-pool-id",
                        "nebius.com/capacity-block-id": "private-capacity-block-id",
                    },
                },
                "status": {"capacity": {"nvidia.com/gpu": "1"}},
            },
            "localization": {
                "state": "observed",
                "reason": None,
                "observations": [
                    {
                        "container": "localize-model",
                        "outcome": "cache-hit",
                        "source": None,
                        "elapsed_seconds": 0.2,
                    }
                ],
            },
        },
    }

    public = module.public_evidence(private, "b" * 64)
    serialized = json.dumps(public, sort_keys=True)

    for raw_identity in (
        "private-boot-id",
        "private-pod-uid",
        "private-node-uid",
        "private-pod-name",
        "private-node-name",
        "private-pool-id",
        "private-accelerator-pool-id",
        "private-capacity-block-id",
        "GPU-private",
        "0198ca75-59c0-7c0e-8f8e-4d19b5ee8ccc",
    ):
        assert raw_identity not in serialized
    assert public["operation"]["id_sha256"] == module._identity_digest(
        "0198ca75-59c0-7c0e-8f8e-4d19b5ee8ccc"
    )
    assert (
        public["runtime_identity_observation"]["localization"]["observations"][0][
            "outcome"
        ]
        == "cache-hit"
    )


def test_failure_cleanup_cancels_acknowledges_and_restores_floor(
    monkeypatch: Any,
) -> None:
    module = _module()
    operation_id = "0198ca75-59c0-7c0e-8f8e-4d19b5ee8ccc"
    calls: list[tuple[str, str]] = []

    class FakeKubectl:
        def __init__(self, kubeconfig: Path, context: str) -> None:
            assert kubeconfig == Path("/tmp/kubeconfig")
            assert context == "test-context"

    def fake_http(
        origin: str,
        context: object,
        token: str,
        method: str,
        path: str,
        **_: object,
    ) -> tuple[int, dict[str, str], dict[str, Any], bytes]:
        calls.append((method, path))
        if path.endswith(":cancel"):
            return 200, {}, {"status": "cancelled"}, b"{}"
        if path.endswith(":acknowledge"):
            return 200, {}, {"status": "cancelled"}, b"{}"
        get_count = sum(1 for item in calls if item == ("GET", path))
        return 200, {}, {"status": "queued" if get_count == 1 else "cancelled"}, b"{}"

    floor = module.ClusterSnapshot(0, 0, 0, True, False, 0, ())
    monkeypatch.setattr(
        module, "validate_origin", lambda *_: ("https://example.test", None)
    )
    monkeypatch.setattr(module, "Kubectl", FakeKubectl)
    monkeypatch.setattr(module, "_json_http", fake_http)
    monkeypatch.setattr(module, "_wait_for_floor", lambda *_: floor)
    args = argparse.Namespace(
        endpoint="https://example.test",
        tls_mode="verified",
        kubeconfig=Path("/tmp/kubeconfig"),
        context="test-context",
        cleanup_timeout_seconds=30,
        accepted_operation_ids=[operation_id],
        expected_floor=0,
    )

    result = module.failure_cleanup(args, "private-token")

    assert result["result"] == "PASS"
    assert result["operations"] == [
        {
            "operation_id_sha256": module._identity_digest(operation_id),
            "cancelled": True,
            "acknowledged": True,
        }
    ]
    assert result["floor_restored"] is True
    assert ("POST", f"/v1/operations/{operation_id}:cancel") in calls
    assert ("POST", f"/v1/operations/{operation_id}:acknowledge") in calls

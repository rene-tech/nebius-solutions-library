from __future__ import annotations

import base64
import importlib.util
import json
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "run_live_fast_start_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_live_fast_start_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_wire_request_keeps_public_protocol_identity() -> None:
    path, payload, modality, protocol = MODULE._wire_request(
        "qwen3-8b",
        {
            "protocol": "openai-chat",
            "operation": "chat",
            "payload": {"model": "qwen3-8b", "stream": False},
        },
    )

    assert path == "/v1/chat/completions"
    assert payload == {"model": "qwen3-8b", "stream": False}
    assert modality == "text"
    assert protocol == "openai-chat"


def test_wire_request_rejects_streaming_gateway_claim() -> None:
    with pytest.raises(MODULE.BenchmarkError, match="request_contract_invalid"):
        MODULE._wire_request(
            "qwen3-8b",
            {
                "protocol": "openai-chat",
                "operation": "chat",
                "payload": {"model": "qwen3-8b", "stream": True},
            },
        )


def test_text_result_reports_response_not_ttft() -> None:
    inference, digest = MODULE._validate_result(
        {
            "choices": [{"message": {"content": "EXPECTED"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        },
        "text",
        "EXPECTED",
        2.0,
    )

    assert inference == {
        "modality": "text",
        "first_output_kind": "response",
        "valid_output": True,
        "http_status": 200,
        "input_units": {"unit": "tokens", "count": 8},
        "output_units": {"unit": "tokens", "count": 4},
        "request_count": 1,
        "warmup_count": 0,
        "concurrency": 1,
        "throughput": {"unit": "output-tokens-per-second", "value": 2.0},
    }
    assert digest == MODULE.sha256_bytes(b"EXPECTED")


def test_media_result_validates_bytes_and_digest() -> None:
    media = b"small-video-fixture"
    inference, digest = MODULE._validate_result(
        {
            "data_base64": base64.b64encode(media).decode("ascii"),
            "bytes": len(media),
            "sha256": MODULE.sha256_bytes(media),
            "frames": 25,
        },
        "video",
        None,
        5.0,
    )

    assert inference["valid_output"] is True
    assert inference["first_output_kind"] == "media"
    assert inference["output_units"] == {"unit": "frames", "count": 25}
    assert inference["throughput"] == {"unit": "frames-per-second", "value": 5.0}
    assert digest == MODULE.sha256_bytes(media)


def _compatibility_tuple(
    capacity_state: str = "prepared-node-zero-pod",
) -> dict[str, object]:
    deployment = {
        "metadata": {
            "annotations": {
                "fs2.nebius/model-content-digest": "sha256:" + "a" * 64,
                "fs2-serve.nebius.ai/workload-pool-ref": "h100-1x",
            },
            "labels": {"app.kubernetes.io/version": "revision-1"},
        },
        "spec": {
            "template": {
                "metadata": {"annotations": {}},
                "spec": {
                    "nodeSelector": {
                        "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                        "capacity.fs2.nebius/type": "preemptible",
                    },
                    "containers": [
                        {
                            "name": "runtime",
                            "image": "registry.example/model@sha256:" + "b" * 64,
                            "command": ["serve"],
                            "args": ["--port", "8000"],
                            "resources": {"limits": {"nvidia.com/gpu": "1"}},
                        }
                    ],
                    "volumes": [],
                },
            }
        },
    }
    observation = MODULE.ClusterObservation(
        observed_at="2026-09-02T00:00:00.000Z",
        replicas=1,
        ready_replicas=1,
        endpoints=1,
        capacity_requested=True,
        capacity_available=True,
        pod=None,
        node=None,
        deployment=deployment,
    )
    return MODULE.build_compatibility_tuple(
        source_commit="c" * 40,
        bundle={"cluster": {"project_id": "project-test", "region": "eu-test1"}},
        context="cluster-test",
        namespace="fs2-models",
        model_id="qwen3-8b",
        model_deployment={
            "spec": {
                "artifact": {"manifestDigest": "sha256:" + "9" * 64},
                "runtime": {"templateRef": {"digest": "sha256:" + "8" * 64}},
                "cache": {"tier": "SharedFilesystem"},
            },
            "status": {"cache": {"digest": "sha256:" + "9" * 64}},
        },
        observation=observation,
        capacity_state=capacity_state,
        mechanism="shared-cache",
        payload_digest="d" * 64,
        client_placement="external",
        interface_protocol="openai-chat",
        endpoint_path="/v1/chat/completions",
        semantic_validator_digest="e" * 64,
        benchmark_client_digest="f" * 64,
        gpu_identity={},
        storage_class=None,
        storage_mode=None,
    )


def test_compatibility_tuple_separates_protocol_and_client_path() -> None:
    value = _compatibility_tuple()

    assert value["interface_protocol"] == "openai-chat"
    assert value["endpoint_path"] == "/v1/chat/completions"
    assert value["streaming"] is False
    assert value["gpu_count"] == 1
    assert value["gpu_product"] is None
    assert value["model_content_digest"] == "sha256:" + "a" * 64
    assert value["artifact_manifest_digest"] == "sha256:" + "9" * 64
    assert value["runtime_template_digest"] == "sha256:" + "8" * 64
    assert value["runtime_image_digest"] == "sha256:" + "b" * 64

    schema = json.loads(
        (SCRIPT.parent / "fast-start-benchmark-receipt.schema.json").read_text()
    )
    assert set(value) == set(schema["$defs"]["compatibilityTuple"]["required"])


def test_text_result_without_usage_cannot_pass() -> None:
    inference, _ = MODULE._validate_result(
        {"choices": [{"message": {"content": "EXPECTED"}}]},
        "text",
        "EXPECTED",
        1.0,
    )

    assert inference["valid_output"] is False
    assert inference["output_units"] is None
    assert inference["throughput"] is None


def test_capacity_wait_is_measured_from_activation() -> None:
    clocks = {
        "request_started": 7_000_000_000,
        "capacity_available": 11_000_000_000,
        "endpoint_ready": 13_000_000_000,
        "first_byte": None,
        "first_semantic": None,
        "completed": None,
        "return_to_floor": None,
    }

    durations = MODULE._attempt_durations(clocks, 1_000_000_000)

    assert durations["capacity_wait"] == 10.0
    assert durations["gpu_capacity_available_to_ready"] == 2.0


def test_failure_attempt_is_validated_and_persisted_new_only(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    args = SimpleNamespace(
        ordinal=2,
        requested_level="L2",
        raw_output=tmp_path / "attempt.raw.json",
        output=tmp_path / "attempt.json",
        runtime_log_output=None,
    )
    wall = "2026-09-02T00:00:00.000Z"
    timestamps = {
        "activation_accepted": wall,
        "gpu_capacity_requested": None,
        "gpu_capacity_available": wall,
        "endpoint_ready": None,
        "request_started": None,
        "first_response_byte": None,
        "first_semantic_output": None,
        "request_completed": None,
        "return_to_floor": None,
    }
    clocks = {
        "request_started": None,
        "capacity_available": 1_000_000_000,
        "endpoint_ready": None,
        "first_byte": None,
        "first_semantic": None,
        "completed": None,
        "return_to_floor": None,
    }
    attempt = MODULE._write_attempt(
        args=args,
        token="x" * 40,
        attempt_id="qwen3-8b-00000000-0000-4000-8000-000000000000",
        status="FAIL",
        failure_code="operation_failed",
        compatibility=_compatibility_tuple(),
        timestamps=timestamps,
        clocks=clocks,
        activation_ns=1_000_000_000,
        inference=MODULE._failure_inference("text"),
        semantic_digest=None,
        runtime_log=b"",
        observations=[],
        operation_id=None,
        cleanup={"status": "complete", "failure_code": None},
    )

    assert attempt["status"] == "FAIL"
    assert json.loads(args.output.read_text())["failure_code"] == "operation_failed"
    with pytest.raises(MODULE.BenchmarkError, match="output_path_invalid"):
        MODULE.atomic_write(args.output, b"replacement")


def test_release_cancels_nonterminal_before_acknowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "id": "00000000-0000-4000-8000-000000000000",
        "model_id": "qwen3-8b",
        "model_revision": "revision-1",
        "protocol": "openai-chat",
        "operation": "chat",
    }
    responses = iter(
        [
            {**identity, "status": "queued"},
            {**identity, "status": "cancelled"},
            {**identity, "status": "cancelled"},
        ]
    )
    calls: list[tuple[str, str]] = []

    def fake_http(
        _origin: str,
        _context: ssl.SSLContext,
        _token: str,
        method: str,
        path: str,
        **_kwargs: object,
    ) -> object:
        calls.append((method, path))
        value = next(responses)
        return MODULE.HttpResult(
            200,
            {},
            json.dumps(value).encode(),
            "2026-09-02T00:00:00.000Z",
            "2026-09-02T00:00:00.000Z",
            1,
            1,
        )

    monkeypatch.setattr(MODULE, "http_json", fake_http)
    MODULE._release_operation(
        origin="https://203.0.113.1",
        context=ssl.create_default_context(),
        token="x" * 40,
        operation_id=identity["id"],
        model_id="qwen3-8b",
        model_revision="revision-1",
        protocol="openai-chat",
        operation="chat",
        deadline=MODULE.time.monotonic() + 10,
    )

    assert calls == [
        ("GET", f"/v1/operations/{identity['id']}"),
        ("POST", f"/v1/operations/{identity['id']}:cancel"),
        ("POST", f"/v1/operations/{identity['id']}:acknowledge"),
    ]


def test_wait_for_floor_requires_the_exact_zero_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = iter(
        [
            MODULE.ClusterObservation(
                "2026-09-02T00:00:00.000Z",
                1,
                0,
                0,
                True,
                True,
                None,
                None,
                {},
            ),
            MODULE.ClusterObservation(
                "2026-09-02T00:00:02.000Z",
                0,
                0,
                0,
                False,
                True,
                None,
                None,
                {},
            ),
        ]
    )
    monkeypatch.setattr(
        MODULE, "observe_cluster", lambda *args, **kwargs: next(observations)
    )
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)

    restored = MODULE.wait_for_floor(
        object(),
        namespace="fs2-models",
        deployment="model-runtime",
        service="model",
        scaled_object="model-scaler",
        scaled_deployment="model-runtime",
        expected_floor=0,
        deadline=MODULE.time.monotonic() + 10,
    )

    assert restored.replicas == 0
    assert restored.ready_replicas == 0
    assert restored.endpoints == 0


def test_failure_cleanup_verifies_floor_even_when_release_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fail_release(*args: object, **kwargs: object) -> None:
        raise MODULE.BenchmarkError("http_request_failed")

    def record_floor(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(MODULE, "_release_operation", fail_release)
    monkeypatch.setattr(MODULE, "wait_for_floor", record_floor)
    args = SimpleNamespace(
        namespace="fs2-models",
        deployment="model-runtime",
        service="model",
        scaled_object="model-scaler",
        scaled_deployment="model-runtime",
        model_id="qwen3-8b",
        expected_floor=1,
        cooldown_seconds=30,
    )

    with pytest.raises(MODULE.BenchmarkError, match="operation_release_failed"):
        MODULE.restore_floor_after_failure(
            object(),
            args,
            origin="https://203.0.113.1",
            context=ssl.create_default_context(),
            token="x" * 40,
            operation_id="00000000-0000-4000-8000-000000000000",
            model_revision="revision-1",
            protocol="openai-chat",
            operation="chat",
        )

    assert len(calls) == 1
    assert calls[0]["expected_floor"] == 1

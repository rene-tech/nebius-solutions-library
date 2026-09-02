from __future__ import annotations

import base64
import importlib.util
import json
import ssl
import sys
from copy import deepcopy
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


def _cosmos_result_fixture() -> tuple[dict[str, object], dict[str, object]]:
    _validator, contract = MODULE._load_cosmos_validator()
    request = contract["requests"][0]
    media = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isom"
    oracle = request["oracle"]
    result = {
        "model": "nvidia/Cosmos3-Nano",
        "revision": "7a312c868bcce8e40b3eb40861300a9d0ba3fde1",
        "mode": "text-to-video",
        "mime_type": oracle["mime_type"],
        "width": oracle["width"],
        "height": oracle["height"],
        "frames": oracle["frames"],
        "fps": oracle["fps"],
        "data_base64": base64.b64encode(media).decode("ascii"),
        "bytes": len(media),
        "sha256": MODULE.sha256_bytes(media),
        "timings_ms": {"queue": 0, "upstream": 1, "total": 1},
    }
    return result, request["request"]


def test_media_result_uses_checked_in_cosmos_contract() -> None:
    result, request_payload = _cosmos_result_fixture()
    inference, digest = MODULE._validate_result(
        result,
        "video",
        None,
        5.0,
        model_id="cosmos3-nano",
        request_payload=request_payload,
    )

    assert inference["valid_output"] is True
    assert inference["first_output_kind"] == "media"
    assert inference["output_units"] == {"unit": "frames", "count": 25}
    assert inference["throughput"] == {"unit": "frames-per-second", "value": 5.0}
    assert digest == result["sha256"]


@pytest.mark.parametrize("corruption", ["not-mp4", "wrong-revision", "unlisted-request"])
def test_media_result_rejects_self_declared_or_unbound_cosmos_output(
    corruption: str,
) -> None:
    result, request_payload = _cosmos_result_fixture()
    if corruption == "not-mp4":
        media = b"small-video-fixture"
        result.update(
            {
                "data_base64": base64.b64encode(media).decode("ascii"),
                "bytes": len(media),
                "sha256": MODULE.sha256_bytes(media),
            }
        )
    elif corruption == "wrong-revision":
        result["revision"] = "unqualified-revision"
    else:
        request_payload = {**request_payload, "seed": 9999}

    inference, digest = MODULE._validate_result(
        result,
        "video",
        None,
        5.0,
        model_id="cosmos3-nano",
        request_payload=request_payload,
    )

    assert inference["valid_output"] is False
    assert digest == ""


def _model_deployment_binding_fixture() -> tuple[dict[str, object], object]:
    namespace = "fs2-models"
    model_id = "qwen3-8b"
    model_uid = "00000000-0000-4000-8000-000000000001"
    spec_digest = "sha256:" + "7" * 64

    def resource(
        api_version: str,
        kind: str,
        name: str,
        uid: str,
        generation: int,
        digest_character: str,
        spec: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        value = {
            "apiVersion": api_version,
            "kind": kind,
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": uid,
                "generation": generation,
                "annotations": {"fs2-serve.nebius.ai/spec-digest": spec_digest},
                "ownerReferences": [
                    {
                        "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
                        "kind": "ModelDeployment",
                        "name": model_id,
                        "uid": model_uid,
                        "controller": True,
                        "blockOwnerDeletion": True,
                    }
                ],
            },
            "spec": spec,
        }
        status = {
            "identity": f"{api_version}/{kind}/{namespace}/{name}",
            "apiVersion": api_version,
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "uid": uid,
            "generation": generation,
            "digest": "sha256:" + digest_character * 64,
        }
        return value, status

    deployment = {
        "template": {
            "metadata": {"annotations": {}},
            "spec": {
                "nodeSelector": {
                    "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                    "accelerator.fs2.nebius/pool-id": "h100-1x",
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
        },
    }
    deployment, deployment_status = resource(
        "apps/v1",
        "Deployment",
        "qwen3-8b-burst-h100-1x",
        "00000000-0000-4000-8000-000000000002",
        3,
        "3",
        deployment,
    )
    deployment["metadata"]["annotations"].update(
        {
            "fs2.nebius/model-content-digest": "sha256:" + "a" * 64,
            "fs2-serve.nebius.ai/workload-pool-ref": "h100-1x",
        }
    )
    service, service_status = resource(
        "v1",
        "Service",
        model_id,
        "00000000-0000-4000-8000-000000000003",
        1,
        "4",
        {"ports": [{"name": "http", "port": 8000}]},
    )
    scaled_object, scaled_object_status = resource(
        "keda.sh/v1alpha1",
        "ScaledObject",
        "fs2-model-qwen3-8b-burst-h100-1x",
        "00000000-0000-4000-8000-000000000004",
        2,
        "5",
        {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "qwen3-8b-burst-h100-1x",
            }
        },
    )
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
        scaled_deployment=deployment,
        service=service,
        scaled_object=scaled_object,
    )
    model_deployment = {
        "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
        "kind": "ModelDeployment",
        "metadata": {
            "name": model_id,
            "namespace": namespace,
            "uid": model_uid,
            "generation": 4,
        },
        "spec": {
            "artifact": {"manifestDigest": "sha256:" + "9" * 64},
            "runtime": {"templateRef": {"digest": "sha256:" + "8" * 64}},
            "cache": {"tier": "SharedFilesystem"},
        },
        "status": {
            "observedGeneration": 4,
            "specDigest": spec_digest,
            "cache": {"digest": "sha256:" + "9" * 64},
            "resources": [deployment_status, service_status, scaled_object_status],
            "endpoint": {
                "namespace": namespace,
                "serviceName": model_id,
                "servicePort": 8000,
                "uid": service_status["uid"],
                "digest": service_status["digest"],
            },
        },
    }
    return model_deployment, observation


def _compatibility_tuple(
    capacity_state: str = "prepared-node-zero-pod",
) -> dict[str, object]:
    model_deployment, observation = _model_deployment_binding_fixture()
    return MODULE.build_compatibility_tuple(
        source_commit="c" * 40,
        bundle={"cluster": {"project_id": "project-test", "region": "eu-test1"}},
        context="cluster-test",
        namespace="fs2-models",
        model_id="qwen3-8b",
        model_deployment=model_deployment,
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
    assert value["model_revision"] == "dynamic:sha256:" + "7" * 64

    schema = json.loads((SCRIPT.parent / "fast-start-benchmark-receipt.schema.json").read_text())
    assert set(value) == set(schema["$defs"]["compatibilityTuple"]["required"])


def test_model_deployment_binding_requires_converged_exact_resources() -> None:
    model_deployment, observation = _model_deployment_binding_fixture()

    assert (
        MODULE._model_deployment_resource_binding(
            model_deployment,
            observation,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )
        == "sha256:" + "7" * 64
    )

    stale = deepcopy(model_deployment)
    stale["status"]["observedGeneration"] = 3
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_status_stale"):
        MODULE._model_deployment_resource_binding(
            stale,
            observation,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )

    wrong_endpoint = deepcopy(model_deployment)
    wrong_endpoint["status"]["endpoint"]["uid"] = "foreign-service-uid"
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_endpoint_identity_mismatch"):
        MODULE._model_deployment_resource_binding(
            wrong_endpoint,
            observation,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )


def test_model_deployment_binding_rejects_foreign_owner_and_generation() -> None:
    model_deployment, observation = _model_deployment_binding_fixture()
    foreign = deepcopy(observation)
    foreign.scaled_object["metadata"]["ownerReferences"][0]["uid"] = "foreign-owner"
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_resource_ownership_mismatch"):
        MODULE._model_deployment_resource_binding(
            model_deployment,
            foreign,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )

    wrong_generation = deepcopy(observation)
    wrong_generation.service["metadata"]["generation"] += 1
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_resource_identity_mismatch"):
        MODULE._model_deployment_resource_binding(
            model_deployment,
            wrong_generation,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )

    wrong_revision = deepcopy(observation)
    wrong_revision.deployment["metadata"]["annotations"]["fs2-serve.nebius.ai/spec-digest"] = "sha256:" + "6" * 64
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_resource_revision_mismatch"):
        MODULE._model_deployment_resource_binding(
            model_deployment,
            wrong_revision,
            namespace="fs2-models",
            model_id="qwen3-8b",
        )


def _runtime_observation(*, pod_count: int = 1, ready_replicas: int = 1) -> object:
    _model_deployment, bound_observation = _model_deployment_binding_fixture()
    pod_uid = "11111111-1111-4111-8111-111111111111"
    node_uid = "22222222-2222-4222-8222-222222222222"
    pod = {
        "metadata": {"uid": pod_uid},
        "spec": {
            "nodeName": "gpu-node-1",
            "containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}],
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    node = {
        "metadata": {
            "name": "gpu-node-1",
            "uid": node_uid,
            "labels": {
                "accelerator.fs2.nebius/class": "nvidia-h100-sxm5-80gb",
                "accelerator.fs2.nebius/pool-id": "h100-1x",
                "capacity.fs2.nebius/type": "preemptible",
                "nvidia.com/gpu.product": "NVIDIA H100 80GB HBM3",
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    return MODULE.ClusterObservation(
        observed_at="2026-09-02T00:00:00.000Z",
        replicas=1,
        ready_replicas=ready_replicas,
        endpoints=1,
        capacity_requested=True,
        capacity_available=True,
        pod=pod,
        node=node,
        deployment=bound_observation.deployment,
        pod_count=pod_count,
        ready_endpoint_pod_uids=(pod_uid,),
        scaled_deployment=bound_observation.scaled_deployment,
        service=bound_observation.service,
        scaled_object=bound_observation.scaled_object,
    )


def _runtime_binding_kwargs() -> dict[str, object]:
    model_deployment, _observation = _model_deployment_binding_fixture()
    return {
        "model_deployment": model_deployment,
        "namespace": "fs2-models",
        "model_id": "qwen3-8b",
        "expected_model_revision": "dynamic:sha256:" + "7" * 64,
    }


def test_runtime_compatibility_is_finalized_from_exact_ready_gpu() -> None:
    initial = _compatibility_tuple()
    finalized = MODULE._finalize_runtime_compatibility(
        initial,
        _runtime_observation(),
        {
            "product": "NVIDIA H100 80GB HBM3",
            "compute_capability": "9.0",
            "memory_bytes": 85_000_000_000,
            "driver_version": "580.95.05",
            "cuda_version": "13.0",
        },
    )

    assert initial["gpu_product"] is None
    assert finalized["gpu_product"] == "NVIDIA H100 80GB HBM3"
    assert finalized["gpu_compute_capability"] == "9.0"
    assert finalized["gpu_memory_bytes"] == 85_000_000_000
    assert finalized["driver_version"] == "580.95.05"
    assert finalized["cuda_version"] == "13.0"


def test_runtime_compatibility_rejects_incomplete_or_mismatched_gpu() -> None:
    compatibility = _compatibility_tuple()
    identity = {
        "product": "NVIDIA H100 80GB HBM3",
        "compute_capability": "9.0",
        "memory_bytes": 85_000_000_000,
        "driver_version": "580.95.05",
    }
    with pytest.raises(MODULE.BenchmarkError, match="gpu_identity_incomplete"):
        MODULE._finalize_runtime_compatibility(
            compatibility,
            _runtime_observation(),
            identity,
        )

    with pytest.raises(MODULE.BenchmarkError, match="gpu_identity_count_mismatch"):
        MODULE._finalize_runtime_compatibility(
            {**compatibility, "gpu_count": 2},
            _runtime_observation(),
            {**identity, "cuda_version": "13.0"},
        )


def test_null_runtime_uses_exact_single_pod_kubernetes_proof() -> None:
    observation = _runtime_observation()
    authority = MODULE._assert_runtime_attribution(
        {
            "runtime": {
                "pod_uid": None,
                "node_uid": None,
                "gpu_uuids": [],
                "gpu_count": 0,
                "preemptible": None,
            }
        },
        observation,
        1,
        observation.pod["metadata"]["uid"],
        **_runtime_binding_kwargs(),
    )

    assert authority == "kubernetes-single-pod-proof-null-operation-runtime"


def test_null_runtime_accepts_exact_ready_endpoint_while_deployment_status_lags() -> None:
    observation = _runtime_observation(ready_replicas=0)
    authority = MODULE._assert_runtime_attribution(
        {
            "runtime": {
                "pod_uid": None,
                "node_uid": None,
                "gpu_uuids": [],
                "gpu_count": 0,
                "preemptible": None,
            }
        },
        observation,
        1,
        observation.pod["metadata"]["uid"],
        **_runtime_binding_kwargs(),
    )

    assert authority == "kubernetes-single-pod-proof-null-operation-runtime"


def test_null_runtime_rejects_unbound_model_resource_or_revision() -> None:
    operation = {
        "runtime": {
            "pod_uid": None,
            "node_uid": None,
            "gpu_uuids": [],
            "gpu_count": 0,
            "preemptible": None,
        }
    }
    observation = _runtime_observation()
    observation.service["metadata"]["ownerReferences"][0]["uid"] = "foreign-owner"
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_resource_ownership_mismatch"):
        MODULE._assert_runtime_attribution(
            operation,
            observation,
            1,
            observation.pod["metadata"]["uid"],
            **_runtime_binding_kwargs(),
        )

    revision_observation = _runtime_observation()
    with pytest.raises(MODULE.BenchmarkError, match="model_deployment_revision_changed"):
        MODULE._assert_runtime_attribution(
            operation,
            revision_observation,
            1,
            revision_observation.pod["metadata"]["uid"],
            **{
                **_runtime_binding_kwargs(),
                "expected_model_revision": "dynamic:sha256:" + "6" * 64,
            },
        )


def test_runtime_attribution_rejects_mismatch_and_ambiguous_pods() -> None:
    observation = _runtime_observation()
    mismatched_runtime = {
        "runtime": {
            "pod_uid": "33333333-3333-4333-8333-333333333333",
            "node_uid": observation.node["metadata"]["uid"],
            "gpu_uuids": ["GPU-test"],
            "gpu_count": 1,
            "preemptible": False,
        }
    }
    with pytest.raises(MODULE.BenchmarkError, match="operation_runtime_identity_mismatch"):
        MODULE._assert_runtime_attribution(
            mismatched_runtime,
            observation,
            1,
            observation.pod["metadata"]["uid"],
            **_runtime_binding_kwargs(),
        )

    with pytest.raises(MODULE.BenchmarkError, match="operation_runtime_identity_mismatch"):
        MODULE._assert_runtime_attribution(
            {
                "runtime": {
                    "pod_uid": None,
                    "node_uid": None,
                    "gpu_uuids": [],
                    "gpu_count": 0,
                    "preemptible": None,
                }
            },
            _runtime_observation(pod_count=2),
            1,
            observation.pod["metadata"]["uid"],
            **_runtime_binding_kwargs(),
        )


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
    monkeypatch.setattr(MODULE, "observe_cluster", lambda *args, **kwargs: next(observations))
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

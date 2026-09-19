from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_scientific_batch_production as production

from fs2_serve.scientific_batch.kubernetes import MODEL_LABEL, _reported_failure
from fs2_serve.scientific_batch.models import BatchStatus, FailureKind, WorkloadState
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository
from fs2_serve.scientific_batch.worker_errors import (
    ERROR_DETAILS,
    LEROBOT_MODEL_ID,
    RETRYABLE_CODES,
    TERMINATION_SCHEMA,
    worker_error_code,
    worker_error_detail,
)


def report(code: str = "PLATFORM_RESPONSE_INVALID", **updates: object) -> str:
    return json.dumps(
        {
            "schema": TERMINATION_SCHEMA,
            "code": code,
            "detail": ERROR_DETAILS[code],
            "retryable": False,
            **updates,
        }
    )


@pytest.mark.parametrize("code", ERROR_DETAILS)
def test_static_worker_report_is_recognized(code: str) -> None:
    assert worker_error_code(report(code)) == code
    assert len(ERROR_DETAILS[code]) <= 256


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not JSON",
        "[]",
        "{}",
        "x" * 1025,
        "\ud800",
        report(detail="private token=secret /tenant/input"),
        report(detail="x" * 257),
        report(extra="not allowed"),
        report(schema="another-schema"),
        report(retryable=1),
        report(retryable=True),
        report().replace('"PLATFORM_RESPONSE_INVALID"', '"UNRECOGNIZED"'),
        report()[:-1] + ',"retryable":false}',
    ],
)
def test_unrecognized_or_unsafe_reports_are_not_propagated(raw: object) -> None:
    assert worker_error_code(raw) is None


@pytest.mark.parametrize("code", RETRYABLE_CODES)
def test_only_declared_transient_codes_accept_retryable_flag(code: str) -> None:
    assert worker_error_code(report(code, retryable=True)) == code
    assert worker_error_code(report(code, retryable=False)) == code


def test_prior_alignment_worker_report_remains_readable_during_rollout() -> None:
    old = "The generated video frame count differs from the source episode."
    code = "COSMOS_MEDIA_ALIGNMENT_INVALID"
    assert worker_error_code(report(code, detail=old)) == code
    assert worker_error_detail(LEROBOT_MODEL_ID, code) == ERROR_DETAILS[code]
    assert "dimensions, frame count or FPS" in ERROR_DETAILS[code]
    assert "no resizing or retiming" in ERROR_DETAILS[code]
    assert worker_error_code(report("DATASET_INVALID", detail=old)) is None
    assert worker_error_code(report(code, detail=old + " private input")) is None
    assert worker_error_code(report(code, detail=old, retryable=True)) is None


def test_worker_and_control_plane_static_tables_do_not_drift() -> None:
    path = (
        Path(__file__).parents[3]
        / "models/general-media/lerobot-augmentation/runtime/src/fs2_lerobot_augmentation/cli.py"
    )
    nodes = ast.parse(path.read_text()).body
    assignments = {
        target.id: node.value
        for node in nodes
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert ast.literal_eval(assignments["ERROR_DETAILS"]) == dict(ERROR_DETAILS)
    assert ast.literal_eval(assignments["TERMINATION_SCHEMA"]) == TERMINATION_SCHEMA
    call = assignments["RETRYABLE_CODES"]
    assert isinstance(call, ast.Call)
    assert frozenset(ast.literal_eval(call.args[0])) == RETRYABLE_CODES


@pytest.mark.parametrize("job_failed", [False, True])
@pytest.mark.asyncio
async def test_observer_propagates_named_worker_error_for_both_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_failed: bool,
) -> None:
    original = production._live_workload

    def workload(*args, **kwargs):
        value = original(*args, **kwargs)
        value["metadata"]["labels"][MODEL_LABEL] = LEROBOT_MODEL_ID
        if job_failed:
            value["status"]["failed"] = 1
        return value

    monkeypatch.setattr(production, "_live_workload", workload)
    attempt_id = uuid4()
    ref, decision = production._collection_ref(attempt_id)
    cluster, client = production._collection_cluster(
        tmp_path,
        ref,
        attempt_id,
        pod=production._staged_pod(
            stage={
                "exitCode": 65,
                "reason": "Error",
                "message": report(),
                "finishedAt": "2026-09-03T04:00:00Z",
            },
            collector_terminated=job_failed,
        ),
        now=production.STAGE_FINISHED_AT + timedelta(seconds=1),
    )
    try:
        observation = await cluster.observe(ref, scheduling=decision)
        assert observation.state is WorkloadState.FAILED
        assert observation.failure_kind is FailureKind.APPLICATION
        assert observation.failure_code == "PLATFORM_RESPONSE_INVALID"
        assert not observation.failure_kind.retryable
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "reason,expected_kind",
    [
        ("OOMKilled", FailureKind.APPLICATION),
        ("Preempted", FailureKind.PREEMPTION),
        ("NodeLost", FailureKind.INFRASTRUCTURE),
    ],
)
def test_system_failure_precedence_is_preserved(reason: str, expected_kind: FailureKind) -> None:
    pod = production._staged_pod(stage={"exitCode": 137, "reason": reason, "message": report()})
    state, kind, code = _reported_failure([reason], [pod["status"]], model_id=LEROBOT_MODEL_ID)
    assert state is (WorkloadState.PREEMPTED if reason == "Preempted" else WorkloadState.FAILED)
    assert kind is expected_kind and code == reason


def test_execution_timeout_code_is_not_replaced_by_a_worker_report() -> None:
    pod = production._staged_pod(stage={"exitCode": 65, "message": report()})
    assert _reported_failure(
        ["MaximumExecutionTimeExceeded"],
        [pod["status"]],
        model_id=LEROBOT_MODEL_ID,
    ) == (WorkloadState.FAILED, FailureKind.APPLICATION, "EXECUTION_TIMEOUT")


@pytest.mark.parametrize("mode", ["other-model", "collector", "exit-zero", "exit-bool", "unknown", "ambiguous"])
def test_only_named_nonzero_recognized_model_stage_is_used(mode: str) -> None:
    stage = {"exitCode": 65, "reason": "Error", "message": report()}
    pod = production._staged_pod(stage=stage)
    model = LEROBOT_MODEL_ID
    if mode == "other-model":
        model = "another-scientific-model"
    elif mode == "collector":
        pod["status"]["containerStatuses"][0]["name"] = "artifact-collector"
    elif mode == "exit-zero":
        stage["exitCode"] = 0
    elif mode == "exit-bool":
        stage["exitCode"] = True
    elif mode == "unknown":
        stage["message"] = "Traceback private input payload"
    statuses = [pod["status"]]
    if mode == "ambiguous":
        other = production._staged_pod(stage={"exitCode": 65, "message": report("DATASET_INVALID")})
        statuses.append(other["status"])
    assert _reported_failure(["Error"], statuses, model_id=model) == (
        WorkloadState.FAILED,
        FailureKind.APPLICATION,
        "Error",
    )


class ProjectionConnection:
    def __init__(self) -> None:
        self.updates = []
        self.events = []

    async def fetchrow(self, query, *args):
        self.updates.append((query, args))
        return {"attempt": 1}

    async def execute(self, query, *args):
        self.events.append((query, args))


@pytest.mark.parametrize(
    "model,code,detail",
    [
        (LEROBOT_MODEL_ID, "DATASET_INVALID", ERROR_DETAILS["DATASET_INVALID"]),
        (LEROBOT_MODEL_ID, "PLATFORM_RESPONSE_INVALID", ERROR_DETAILS["PLATFORM_RESPONSE_INVALID"]),
        (LEROBOT_MODEL_ID, "Error", None),
        (LEROBOT_MODEL_ID, "OOMKilled", None),
        ("another-scientific-model", "DATASET_INVALID", None),
    ],
)
@pytest.mark.asyncio
async def test_terminal_operation_projection_uses_static_detail_only(model, code, detail) -> None:
    connection = ProjectionConnection()
    operation_id = uuid4()
    previous = SimpleNamespace(status=BatchStatus.RUNNING)
    current = SimpleNamespace(status=BatchStatus.FAILED, model_id=model, failure_code=code, operation_id=operation_id)
    await PostgresScientificBatchRepository._project_operation(connection, previous, current)
    query, args = connection.updates[0]
    assert "error_detail=$6" in query and "reserved_gpu_seconds=0" in query
    assert args == (operation_id, "failed", "failed", 422, code, detail)
    assert len(connection.events) == 1


@pytest.mark.asyncio
async def test_running_projection_clears_prior_errors_without_terminal_text() -> None:
    connection = ProjectionConnection()
    operation_id = uuid4()
    await PostgresScientificBatchRepository._project_operation(
        connection,
        SimpleNamespace(status=BatchStatus.QUEUED),
        SimpleNamespace(status=BatchStatus.RUNNING, operation_id=operation_id),
    )
    query, args = connection.updates[0]
    assert "error_code=NULL,error_detail=NULL" in query and args == (operation_id,)

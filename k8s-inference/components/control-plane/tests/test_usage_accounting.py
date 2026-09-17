"""Admission estimates cannot become measured GPU consumption by projection."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from fs2_serve.lifecycle import LifecycleRollup, LifecycleSubject, LifecycleWorkloadSummary
from fs2_serve.usage_accounting import lifecycle_usage_from_counts, project_attempt, reconciliation_report
from fs2_serve.usage_reconciliation import export_snapshot

NOW = datetime(2026, 9, 15, tzinfo=UTC)


def workload(*, operation=None, scientific=True, phases=None, outcome="succeeded", quality="measured", gaps=()):
    op = operation or uuid4()
    subject = LifecycleSubject(
        subject_id=uuid4(),
        workload_kind="scientific_batch" if scientific else "online",
        operation_id=op,
        request_id=op,
        batch_id=uuid4() if scientific else None,
        workload_id=uuid4(),
        attempt_id=uuid4() if scientific else None,
        tenant_id="stockholm-test",
        principal_id="user-test",
        model_id="test-model",
        model_revision="test",
        protocol="scientific-batch-v1" if scientific else "openai-chat",
        accepted_at=NOW,
        trace_id="1" * 32,
    )
    rollup = LifecycleRollup(
        rollup_id=uuid4(),
        subject_id=subject.subject_id,
        generated_at=NOW,
        event_watermark=1,
        events_sha256="f" * 64,
        terminal=True,
        outcome=outcome,
        quota_reserved_gpu_seconds=1000,
        scheduler_occupied_gpu_seconds=100,
        device_allocated_gpu_seconds=100,
        active_gpu_seconds=40,
        occupied_idle_gpu_seconds=60,
        phase_gpu_seconds=phases
        if phases is not None
        else {
            "active_compute": 40,
            "artifact_load": 30,
            "resident_idle": 20,
            "unclassified": 10,
        },
        reconciliation_delta_seconds=0,
        device_scheduler_delta_seconds=0,
        tolerance_seconds=1,
        reconciled=True,
        quality=quality,
        data_gaps=list(gaps),
    )
    return LifecycleWorkloadSummary(subject=subject, rollup=rollup)


def test_startup_and_unknown_are_not_idle_and_history_is_unchanged():
    value = workload()
    before = value.model_dump_json()
    projected = project_attempt(value)
    assert projected["occupied"] == 100
    assert projected["classified_idle"] == 20
    assert projected["startup"] == 30
    assert projected["unknown"] == 10
    assert projected["legacy_nonactive_residual_gpu_seconds"] == 60
    assert value.model_dump_json() == before


def test_retry_and_failure_are_visible_once_without_charging_reservations_as_usage():
    op = uuid4()
    failed = workload(operation=op, outcome="failed")
    succeeded = workload(operation=op)
    result = reconciliation_report(
        [failed, failed, succeeded],
        expected_operation_ids={op},
        admission_snapshots=[{"admission_budget_consumed_gpu_seconds": 22 * 3600}],
    )
    assert len(result["attempts"]) == 2
    assert {row["outcome"] for row in result["attempts"]} == {"failed", "succeeded"}
    assert result["exclusive_scientific_usage"]["occupied"]["value"] == 200
    assert result["exclusive_scientific_usage"]["subjects"] == 2
    assert result["exclusive_scientific_usage"]["covered_operations"] == 1
    assert result["admission_snapshots"][0]["admission_budget_consumed_gpu_seconds"] == 79200
    assert result["billing"] is False and result["ledger_mutated"] is False
    assert "Do not sum across keys" in result["admission_snapshot_basis"]


def test_overlapping_shared_serving_is_not_summed_into_owner_usage():
    online = [workload(scientific=False), workload(scientific=False)]
    result = reconciliation_report(online, expected_operation_ids=set())
    assert result["online_subjects_excluded_from_additive_totals"] == 2
    assert all(row["occupied"] == 100 for row in result["attempts"])
    assert result["exclusive_scientific_usage"]["occupied"]["value"] is None


def test_missing_subject_and_missing_rollup_never_report_complete_zero():
    value = workload()
    missing = value.model_copy(update={"rollup": None})
    for values, expected in (
        ([value], {value.subject.operation_id, uuid4()}),
        ([missing], {value.subject.operation_id}),
    ):
        result = reconciliation_report(values, expected_operation_ids=expected)
        assert not result["exclusive_scientific_usage"]["occupied_complete"]
        assert result["exclusive_scientific_usage"]["occupied"]["value"] is None


def test_incomplete_classification_preserves_occupied_and_known_lower_bound():
    value = workload(gaps=["phase_classification_incomplete"])
    usage = reconciliation_report([value], expected_operation_ids={value.subject.operation_id})[
        "exclusive_scientific_usage"
    ]
    assert usage["occupied_complete"] and not usage["phases_complete"]
    assert usage["occupied"]["state"] == "available"
    assert usage["classified_idle"]["value"] == 20
    assert usage["classified_idle"]["state"] == "estimated"
    assert usage["unknown"]["value"] == 10
    assert usage["queue"]["value"] is None
    assert usage["queue"]["unit"] == "seconds"


@pytest.mark.parametrize("quality", ["application_observed", "estimated", "unavailable"])
def test_quality_is_not_upgraded_by_projection(quality):
    value = workload(quality=quality)
    usage = reconciliation_report([value], expected_operation_ids={value.subject.operation_id})[
        "exclusive_scientific_usage"
    ]
    assert usage["quality"] == quality
    assert usage["occupied"]["state"] == ("unavailable" if quality == "unavailable" else "estimated")


def test_complete_classified_phases_partition_occupied():
    value = workload(phases={"active_compute": 40, "warmup": 30, "workflow_wait": 20, "teardown": 10})
    usage = reconciliation_report([value], expected_operation_ids={value.subject.operation_id})[
        "exclusive_scientific_usage"
    ]
    assert usage["phases_complete"] and usage["occupied_complete"]
    assert sum(usage[name]["value"] for name in ("active", "classified_idle", "startup", "other", "unknown")) == 100


def test_conflicting_duplicate_subject_snapshots_are_refused():
    value = workload()
    changed = value.model_copy(update={"rollup": value.rollup.model_copy(update={"outcome": "failed"})})
    with pytest.raises(ValueError, match="conflicting rollups"):
        reconciliation_report([value, changed], expected_operation_ids={value.subject.operation_id})


class ReadOnlyConnection:
    def __init__(self, *, overflow=False):
        self.readonly = False
        self.overflow = overflow

    def transaction(self, **kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return self

    async def __aenter__(self):
        self.readonly = True

    async def __aexit__(self, *args):
        self.readonly = False

    async def fetchval(self, sql):
        assert self.readonly and sql == "SELECT transaction_timestamp()"
        return NOW

    async def fetch(self, sql, *args):
        assert self.readonly and sql.lstrip().startswith("SELECT ")
        assert "request_body" not in sql and "token_hash" not in sql
        if self.overflow and "FROM fs2_operations" in sql:
            return [{"id": uuid4()}] * 2
        return []


async def test_export_uses_readonly_consistent_snapshot_without_migrations():
    result = await export_snapshot(
        ReadOnlyConnection(), tenant_id="stockholm-test", from_at=NOW, to_at=NOW.replace(day=16)
    )
    assert result["attempts"] == []
    assert result["operations"] == []
    assert result["exclusive_scientific_usage"]["occupied"]["value"] is None


async def test_export_refuses_truncation_instead_of_partial_total():
    with pytest.raises(ValueError, match="operation export exceeds limit"):
        await export_snapshot(
            ReadOnlyConnection(overflow=True),
            tenant_id="stockholm-test",
            from_at=NOW,
            to_at=NOW.replace(day=16),
            limit=1,
        )


def test_legacy_counter_without_lifecycle_does_not_supply_measurements():
    result = lifecycle_usage_from_counts({"gpu_seconds_used": 79200, "idle": 577.44})
    assert result.occupied.value is result.classified_idle.value is None

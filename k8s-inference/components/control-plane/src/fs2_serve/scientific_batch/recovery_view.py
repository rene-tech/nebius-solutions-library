"""Payload-free customer/operator recovery projection from durable attempts."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from ..models import StrictModel
from .models import AttemptOutcome, BatchStatus, ScientificAttemptState, ScientificBatchState
from .pool_recovery import RECOVERY_CODES, PoolRecoveryPolicy, retry_pools


class ScientificPoolRecovery(StrictModel):
    state: Literal["retrying", "recovered", "capacity_unavailable", "cancelled", "failed"]
    cause: str
    attempt_number: int
    max_attempts: int
    failed_pool_id: str | None
    eligible_pool_ids: tuple[str, ...]
    avoided_pool_ids: tuple[str, ...]
    retry_not_before: datetime | None = None
    admitted_wait_seconds: float | None = Field(default=None, ge=0)
    message: str


def recovery_view(
    state: ScientificBatchState,
    attempt: ScientificAttemptState,
    *,
    policy: PoolRecoveryPolicy | None = None,
) -> ScientificPoolRecovery | None:
    stage, plan = state.stage(attempt.stage_id), state.plan.stage(attempt.stage_id)
    failures = [
        prior
        for prior in stage.attempts
        if prior.shard_id == attempt.shard_id
        and prior.attempt_number <= attempt.attempt_number
        and prior.failure_code in RECOVERY_CODES
    ]
    if not failures:
        return None
    failure = max(failures, key=lambda item: item.attempt_number)
    latest = stage.latest_attempt(attempt.shard_id) or attempt
    outcome: Literal["retrying", "recovered", "capacity_unavailable", "cancelled", "failed"] = (
        "recovered" if latest.outcome is AttemptOutcome.SUCCEEDED else "retrying"
    )
    if state.status is BatchStatus.CANCELLED or latest.outcome is AttemptOutcome.CANCELLED:
        outcome = "cancelled"
    elif latest.outcome is AttemptOutcome.FAILED and latest.attempt_number >= plan.max_attempts:
        outcome = "capacity_unavailable" if latest.failure_code in RECOVERY_CODES else "failed"
    elif state.status is BatchStatus.FAILED:
        outcome = "capacity_unavailable" if state.failure_code in RECOVERY_CODES else "failed"
    admission = failure.scheduling_admission
    next_number = attempt.attempt_number + (attempt.outcome is AttemptOutcome.FAILED)
    pools = retry_pools(state, stage_id=attempt.stage_id, shard_id=attempt.shard_id, attempt_number=next_number)
    original = state.scheduling.stage(attempt.stage_id).resolved_pool_preference
    return ScientificPoolRecovery(
        state=outcome,
        cause=failure.failure_code or "admitted_pool_unavailable",
        attempt_number=attempt.attempt_number,
        max_attempts=plan.max_attempts,
        failed_pool_id=admission.resolved_pool_id if admission else None,
        eligible_pool_ids=pools,
        avoided_pool_ids=tuple(pool for pool in original if pool not in pools),
        retry_not_before=(policy.retry_at(attempt) if policy is not None and outcome == "retrying" else None),
        admitted_wait_seconds=(
            max(0, (failure.completed_at - admission.admitted_at).total_seconds())
            if admission is not None and admission.admitted_at is not None and failure.completed_at
            else None
        ),
        message={
            "retrying": "Automatic capacity recovery within the original qualified pools and attempt budget.",
            "recovered": "The replacement attempt completed after automatic capacity recovery.",
            "capacity_unavailable": (
                "The bounded capacity recovery budget is exhausted. No qualified capacity started the attempt; "
                "retry as a new operation after capacity returns or contact the platform operator."
            ),
            "cancelled": "The customer cancelled the operation during or after capacity recovery.",
            "failed": "The operation stopped after capacity recovery; inspect the terminal attempt's failure cause.",
        }[outcome],
    )

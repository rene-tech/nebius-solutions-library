"""A failed artifact peer cannot strand a still-running native worker's GPU."""

from datetime import timedelta
from uuid import uuid4

import pytest

from fs2_serve.scientific_batch.kubernetes import _reported_failure, _stalled_collection
from fs2_serve.scientific_batch.models import FailureKind, WorkloadState
from test_scientific_batch_production import (
    STAGE_FINISHED_AT, _collection_cluster, _collection_ref, _staged_pod,
)


def failed_collector(reason='Error', exit_code=1):
    pod = _staged_pod(stage=None)
    pod['status']['containerStatuses'][1]['state'] = {'terminated': {
        'exitCode': exit_code, 'reason': reason,
        'finishedAt': STAGE_FINISHED_AT.isoformat(),
    }}
    return pod


@pytest.mark.asyncio
async def test_observer_reports_failed_collector_before_model_ack_timeout(tmp_path):
    attempt_id = uuid4()
    ref, decision = _collection_ref(attempt_id)
    cluster, client = _collection_cluster(
        tmp_path, ref, attempt_id,
        pod=failed_collector(), now=STAGE_FINISHED_AT + timedelta(seconds=1))
    try:
        result = await cluster.observe(ref, scheduling=decision)
        assert result.state is WorkloadState.FAILED
        assert result.failure_kind is FailureKind.APPLICATION
        assert result.failure_code == 'artifact_collector_failed'
        assert not result.failure_kind.retryable
    finally:
        await client.aclose()


def test_successfully_finished_collector_does_not_kill_finalizing_worker():
    assert _stalled_collection([failed_collector('Completed', 0)['status']], now=STAGE_FINISHED_AT) is None


def test_running_peers_remain_running():
    assert _stalled_collection([_staged_pod(stage=None)['status']], now=STAGE_FINISHED_AT) is None


def test_collector_oom_preserves_the_existing_failure_classification():
    statuses = [failed_collector('OOMKilled', 137)['status']]
    actual = _stalled_collection(statuses, now=STAGE_FINISHED_AT)
    assert actual == _reported_failure(['OOMKilled'], statuses)
    assert actual[2] != 'artifact_collector_failed'

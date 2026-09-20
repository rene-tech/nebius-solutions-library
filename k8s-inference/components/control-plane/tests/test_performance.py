# ruff: noqa: F811 -- imported pytest fixture injected into test parameters
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_users_apps_postgres import database  # noqa: F401

from fs2_serve.performance import (
    CampaignCreate,
    ClaimRequest,
    PerformanceRepository,
    TrialLease,
    TrialResult,
    summarize_profiles,
)
from fs2_serve.store import ConflictError


def campaign(**changes):
    return CampaignCreate.model_validate(
        {
            "name": "test-" + uuid4().hex,
            "source_commit": "a" * 40,
            "catalog_sha256": "b" * 64,
            "max_parallel": 2,
            "cases": [
                {
                    "case_id": "small",
                    "model_id": "boltz2",
                    "workload_class": "short-protein",
                    "fixture_sha256": "c" * 64,
                    "adapter": "native",
                    "fixture_ref": "fixtures/boltz.json",
                }
            ],
            **changes,
        }
    )


def result(trial, **changes):
    return TrialResult.model_validate(
        {
            "worker": trial["worker"],
            "fence": trial["fence"],
            "status": "succeeded",
            "operation_id": str(uuid4()),
            "semantic_valid": True,
            "receipt_sha256": "d" * 64,
            "artifact_uri": "s3://test-campaign/receipt.json",
            "elapsed_seconds": 10,
            **changes,
        }
    )


@pytest.mark.postgres
async def test_campaign_idempotency_and_bounded_concurrent_claims(database):
    repo = PerformanceRepository(database.pool)
    spec = campaign()
    created = await repo.create(spec, "test")
    assert (await repo.create(spec, "test"))["id"] == created["id"]
    with pytest.raises(ConflictError):
        await repo.create(spec.model_copy(update={"max_parallel": 1}), "test")
    claims = await asyncio.gather(*[repo.claim(created["id"], ClaimRequest(worker=f"worker-{n}")) for n in range(8)])
    claimed = [row for row in claims if row is not None]
    assert len(claimed) == 2
    assert len({row["id"] for row in claimed}) == 2
    finished = result(claimed[0])
    await repo.finish(claimed[0]["id"], finished)
    await repo.finish(claimed[0]["id"], finished)
    with pytest.raises(ConflictError):
        await repo.finish(claimed[0]["id"], finished.model_copy(update={"elapsed_seconds": 11}))
    assert await repo.claim(created["id"], ClaimRequest(worker="third")) is not None
    details = await repo.detail(created["id"])
    assert len(details["trials"]) == 3
    assert next(row for row in await repo.list() if row["id"] == created["id"])["succeeded"] == 1


@pytest.mark.postgres
async def test_expired_lease_is_fenced_and_same_trial_reclaimed(database):
    repo = PerformanceRepository(database.pool)
    spec = campaign(max_parallel=1)
    created = await repo.create(spec, "test")
    first = await repo.claim(created["id"], ClaimRequest(worker="first"))
    await database.pool.execute(
        "UPDATE fs2_benchmark_trials SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", first["id"]
    )
    second = await repo.claim(created["id"], ClaimRequest(worker="second"))
    assert second["id"] == first["id"]
    assert second["fence"] == first["fence"] + 1
    with pytest.raises(ConflictError):
        await repo.finish(first["id"], result(first))
    with pytest.raises(ConflictError):
        await repo.heartbeat(first["id"], TrialLease(worker="first", fence=first["fence"]))
    await repo.heartbeat(second["id"], TrialLease(worker="second", fence=second["fence"]))
    await repo.finish(second["id"], result(second))
    attempts = await database.pool.fetch(
        "SELECT outcome FROM fs2_benchmark_attempts WHERE trial_id=$1 ORDER BY fence", first["id"]
    )
    assert [row["outcome"] for row in attempts] == ["lease-expired", "succeeded"]


@pytest.mark.postgres
async def test_unsupported_models_remain_in_denominator(database):
    spec = campaign()
    spec.cases[0].unavailable_reason = "No compatible GPU in this cluster"
    repo = PerformanceRepository(database.pool)
    created = await repo.create(spec, "test")
    assert await repo.claim(created["id"], ClaimRequest(worker="worker")) is None
    details = await repo.detail(created["id"])
    assert len(details["trials"]) == 3
    assert all(row["status"] == "unsupported" for row in details["trials"])


@pytest.mark.postgres
async def test_first_repetition_covers_other_models_before_repeating(database):
    spec = campaign()
    other = spec.cases[0].model_copy(update={"case_id": "other", "model_id": "mosaic"})
    spec.cases.append(other)
    repo = PerformanceRepository(database.pool)
    created = await repo.create(spec, "test")
    first = await repo.claim(created["id"], ClaimRequest(worker="first"))
    second = await repo.claim(created["id"], ClaimRequest(worker="second"))
    assert first["repetition"] == second["repetition"] == 1
    assert first["model_id"] != second["model_id"]


def test_metrics_unknown_not_zero_and_no_unobserved_hardware_recommendation():
    spec = campaign()
    trial = {"worker": "worker", "fence": 1, "status": "succeeded", "case_spec": spec.cases[0].model_dump()}
    trial["result"] = result(trial).model_dump(mode="json")
    summary = summarize_profiles([trial, trial, trial])[0]
    assert summary["metrics"]["elapsed_seconds"]["median"] == 10
    assert summary["metrics"]["execution_seconds"] == {"count": 0, "median": None, "min": None, "max": None}
    assert summary["placement_evidence"] == "insufficient"
    assert summary["automatic_placement"] is False


def test_contract_rejects_duplicate_cases_nonfinite_times_and_fake_success():
    spec = campaign()
    with pytest.raises(ValidationError):
        campaign(cases=spec.cases * 2)
    with pytest.raises(ValidationError):
        result({"worker": "test", "fence": 1}, semantic_valid=False)
    with pytest.raises(ValidationError):
        result({"worker": "test", "fence": 1}, elapsed_seconds=float("nan"))
    with pytest.raises(ValidationError):
        result({"worker": "test", "fence": 1}, artifact_uri="s3://bucket/result?secret=signed")

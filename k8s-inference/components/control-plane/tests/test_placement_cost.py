# ruff: noqa: F811 -- pytest fixture injection
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_performance import campaign
from test_users_apps_postgres import database  # noqa: F401

from fs2_serve.performance import CampaignCreate, ClaimRequest, PerformanceRepository, TrialResult
from fs2_serve.placement_cost import cost_profiles, ranked_pools

IMAGE = "repo/test@sha256:" + "a" * 64


def measured_campaign():
    target = {
        "model_id": "boltz2",
        "runtime_image": IMAGE,
        "accelerators_per_replica": 1,
        "workload_class": "short-protein",
        "cache_condition": "cold",
    }
    spec = campaign().model_dump(mode="json")
    base_case = spec["cases"][0]
    spec["placement_target"] = target
    spec["cases"] = [
        {
            **base_case,
            "case_id": pool,
            "requested_pool": pool,
            "cache_condition": "cold",
            "compute_rate": {
                "usd_per_replica_hour": rate,
                "source": "https://example.com/rates",
                "observed_at": "2026-10-08T10:00:00Z",
                "allocation_basis": "GPU plus CPU and RAM share",
            },
        }
        for pool, rate in [("cheap-hour-slow-request", "1"), ("expensive-hour-fast-request", "3")]
    ]
    spec = CampaignCreate.model_validate(spec).model_dump(mode="json")
    trials = []
    for case in spec["cases"]:
        for repeat in range(3):
            trials.append(
                {
                    "id": case["case_id"] + str(repeat),
                    "case_spec": case,
                    "status": "succeeded",
                    "result": {
                        "semantic_valid": True,
                        "allocated_compute_seconds": 100 if case["case_id"] == "cheap-hour-slow-request" else 20,
                        "hardware": {"pool": case["requested_pool"], "runtime_image": IMAGE, "gpu_count": 1},
                    },
                }
            )
    return {"id": "campaign", "spec_sha256": "b" * 64, "spec": spec, "trials": trials}


def test_price_per_request_not_price_per_hour():
    profiles = cost_profiles(measured_campaign())
    assert [p["pool"] for p in profiles] == ["expensive-hour-fast-request", "cheap-hour-slow-request"]
    assert Decimal(profiles[0]["usd_per_successful_request"]) == Decimal(1) / 60
    assert ranked_pools(["unknown", "cheap-hour-slow-request", "expensive-hour-fast-request"], profiles) == [
        "expensive-hour-fast-request",
        "cheap-hour-slow-request",
        "unknown",
    ]


def test_failed_allocations_count_in_cost_of_success():
    evidence = measured_campaign()
    failure = deepcopy(evidence["trials"][0])
    failure["id"], failure["status"] = "failed-request", "failed"
    failure["result"]["semantic_valid"] = False
    failure["result"]["allocated_compute_seconds"] = 300
    evidence["trials"].append(failure)
    profile = next(p for p in cost_profiles(evidence) if p["pool"] == failure["case_spec"]["requested_pool"])
    assert Decimal(profile["usd_per_successful_request"]) == Decimal(1) / 18
    assert profile["successes"] == 3 and profile["attempted_requests"] == 4


@pytest.mark.parametrize("change", ["missing-allocation", "pending", "wrong-image", "wrong-gpu-count", "bad-semantic"])
def test_unqualified_evidence_does_not_become_a_zero_cost(change):
    evidence = measured_campaign()
    trial = evidence["trials"][0]
    if change == "missing-allocation":
        trial["result"].pop("allocated_compute_seconds")
    elif change == "pending":
        trial["status"] = "running"
    elif change == "wrong-image":
        trial["result"]["hardware"]["runtime_image"] = "repo/changed@sha256:" + "c" * 64
    elif change == "wrong-gpu-count":
        trial["result"]["hardware"]["gpu_count"] = 8
    else:
        trial["result"]["semantic_valid"] = False
    assert len(cost_profiles(evidence)) == 1


def test_rates_require_matched_inputs_and_explicit_placement_intent():
    evidence = measured_campaign()
    evidence["spec"]["cases"][1]["fixture_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="identical input"):
        CampaignCreate.model_validate(evidence["spec"])
    evidence["spec"].pop("placement_target")
    assert cost_profiles(evidence) == []


def test_old_campaign_payload_and_idempotency_hash_are_unchanged():
    value = campaign().model_dump(mode="json")
    assert "placement_target" not in value
    assert "compute_rate" not in value["cases"][0]


@pytest.mark.postgres
async def test_durable_cost_revision_never_promotes_partial_or_wrong_runtime(database):
    evidence = measured_campaign()
    repository = PerformanceRepository(database.pool)
    created = await repository.create(CampaignCreate.model_validate(evidence["spec"]), "qa")
    arguments = dict(model_id="boltz2", runtime_image=IMAGE, accelerators_per_replica=1, cache_condition="cold")
    assert await repository.placement_profiles(**arguments) == []
    for i in range(6):
        claim = await repository.claim(created["id"], ClaimRequest(worker="worker"))
        pool = claim["case_spec"]["requested_pool"]
        await repository.finish(
            claim["id"],
            TrialResult(
                worker="worker",
                fence=claim["fence"],
                status="succeeded",
                operation_id=uuid4(),
                semantic_valid=True,
                allocated_compute_seconds=100 if pool.startswith("cheap-") else 20,
                receipt_sha256="c" * 64,
                artifact_uri="s3://internal-test/receipt.json",
                hardware={
                    "pool": pool,
                    "runtime_image": IMAGE,
                    "gpu_count": 1,
                    "gpus_per_node": 1,
                    "gpu_product": "test-only",
                    "cpu_arch": "amd64",
                    "driver_version": "test",
                    "runtime_fingerprint": "d" * 64,
                    "topology": "single-device",
                    "local_storage": "absent",
                },
            ),
        )
        if i < 5:
            assert await repository.placement_profiles(**arguments) == []
    profiles = await repository.placement_profiles(**arguments)
    assert profiles[0]["pool"] == "expensive-hour-fast-request"
    assert await repository.placement_profiles(**{**arguments, "runtime_image": "changed@sha256:" + "f" * 64}) == []
    new_spec = CampaignCreate.model_validate(evidence["spec"]).model_copy(update={"name": "newer-" + uuid4().hex})
    await repository.create(new_spec, "qa")
    assert await repository.placement_profiles(**arguments) == profiles

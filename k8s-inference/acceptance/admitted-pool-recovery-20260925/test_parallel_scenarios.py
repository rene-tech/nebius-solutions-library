"""Read-only runtime evidence and narrowly disjoint additional matrix cases."""
from copy import deepcopy

import pytest

import run_acceptance as m

IMAGE = "registry/gromacs@sha256:" + "1" * 64


def case(mode):
    init = mode == "init"
    return {"scenario": "recovery" if init else "synthetic-no-eligible-capacity", "release": {"runtime_image": IMAGE},
            "fixture": {"windows": ["window-03", "window-04"] if init else ["window-01", "window-02"]},
            "injection_mode": "task-only-create-webhook", "injector": {
                "config": {"tenant": m.TENANT, "namespace": "fs2-models", "runtime_image": IMAGE, "dead_pool": "h100-1x",
                           "pause_seconds": 150 if init else 0, "synthetic_no_capacity": not init,
                           "shard": "window-03" if init else "window-01"},
                "resources": [{"kind": "Namespace", "name": m.TENANT + "-" + mode}]}}


def test_only_disjoint_explicit_extra_cases_can_overlap():
    m.disjoint_parallel_injectors(case("init"), case("synthetic"))
    m.disjoint_parallel_injectors(case("synthetic"), case("init"))


@pytest.mark.parametrize("change", [
    lambda c: c["fixture"].update(windows=["window-01", "window-03"]),
    lambda c: c.update(scenario="cancel"),
    lambda c: c["injector"]["config"].update(tenant="customer"),
    lambda c: c["injector"]["config"].update(shard="window-01"),
    lambda c: c["injector"]["config"].update(runtime_image="other"),
    lambda c: c["injector"]["resources"][0].update(name=m.TENANT),
])
def test_overlap_scope_and_wrong_instance_are_rejected(change):
    left = case("init")
    change(left)
    with pytest.raises(m.GateError):
        m.disjoint_parallel_injectors(left, case("synthetic"))


def test_identical_extra_cases_cannot_overlap():
    with pytest.raises(m.GateError, match="not_disjoint"):
        m.disjoint_parallel_injectors(case("init"), case("init"))


def observations():
    return [{"pods": [{"containers": [{"name": "scientific-stage", "started": True,
             "image": "sha256:" + "2" * 64, "image_id": IMAGE, "requested_image": IMAGE}]}]}]


def test_executed_image_id_not_status_config_digest_is_authoritative():
    value = observations()
    assert m.verify_runtime(value, IMAGE)["image_ids"] == [IMAGE]
    del value[0]["pods"][0]["containers"][0]["requested_image"]
    assert m.verify_runtime(value, IMAGE)["observations"] == 1


@pytest.mark.parametrize("field,value", [("image_id", ""), ("image_id", "sha256:" + "2" * 64),
                                         ("requested_image", "other"), ("started", False)])
def test_wrong_or_missing_execution_proof_is_rejected(field, value):
    proof = deepcopy(observations())
    proof[0]["pods"][0]["containers"][0][field] = value
    with pytest.raises(m.GateError, match="unproven"):
        m.verify_runtime(proof, IMAGE)

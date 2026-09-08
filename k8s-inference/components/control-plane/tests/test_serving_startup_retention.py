"""Evaluate the real generated KEDA expression with Prometheus, not a mock parser."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from fs2_serve.model_deployment import startup_retention_promql


def series(metric: str, values: str, **labels: str) -> dict[str, str]:
    selector = ",".join(f"{key}={json.dumps(value)}" for key, value in labels.items())
    return {"series": f"{metric}{{{selector}}}", "values": values}


def fixture(
    *,
    desired: str = "0+0x3 1+0x100",
    created: str = "-3600+0x104",
    phase: str = "Pending",
    ready: str = "0+0x104",
    owner: str = "model-burst",
    rs_desired: str = "1+0x104",
    deleting: bool = False,
) -> list[dict[str, str]]:
    target = {"namespace": "models", "deployment": "model-burst"}
    pod = {"namespace": "models", "pod": "model-burst-abc-pod", "uid": "first-uid"}
    rs = {"namespace": "models", "replicaset": "model-burst-abc"}
    rows = [
        series("kube_deployment_spec_replicas", desired, **target),
        series("kube_deployment_created", created, **target),
        series("kube_pod_status_ready", ready, **pod, condition="true"),
        series("kube_pod_status_phase", "1+0x104", **pod, phase=phase),
        series(
            "kube_pod_owner",
            "1+0x104",
            **pod,
            owner_kind="ReplicaSet",
            owner_is_controller="true",
            owner_name="model-burst-abc",
        ),
        series(
            "kube_replicaset_owner",
            "1+0x104",
            **rs,
            owner_kind="Deployment",
            owner_is_controller="true",
            owner_name=owner,
        ),
        series("kube_replicaset_spec_replicas", rs_desired, **rs),
    ]
    if deleting:
        rows.append(series("kube_pod_deletion_timestamp", "90+0x104", **pod))
    return rows


def test_startup_retention_lifecycle_with_promtool(tmp_path: Path) -> None:
    promtool = shutil.which("promtool")
    if promtool is None:
        pytest.skip("promtool is required to evaluate the generated autoscaling expression")
    expression = startup_retention_promql(
        namespace="models", deployment="model-burst", timeout_seconds=900, target_queue_depth=2
    )
    cases = [
        ("scale from zero holds during a 155-second pull", fixture(), "3m", 2),
        ("unready Running also holds", fixture(phase="Running"), "3m", 2),
        ("Ready releases startup hold immediately", fixture(ready="0+0x5 1+0x98"), "3m", 0),
        ("budget expires despite repeated container restarts", fixture(), "17m", 0),
        ("already requested count, not unready count", fixture(desired="0+0x3 3+0x100"), "3m", 6),
        ("never creates demand from zero", fixture(desired="0+0x104", created="0+0x104"), "3m", 0),
        ("foreign Deployment cannot retain this segment", fixture(owner="other-model"), "3m", 0),
        ("retired ReplicaSet cannot retain capacity", fixture(rs_desired="0+0x104"), "3m", 0),
        ("terminating Pod cannot retain capacity", fixture(deleting=True), "3m", 0),
        ("Succeeded is not startup", fixture(phase="Succeeded"), "3m", 0),
        ("Failed is not startup", fixture(phase="Failed"), "3m", 0),
        ("old unhealthy Pod does not create a new deadline", fixture(desired="1+0x104"), "3m", 0),
        ("scale-in cannot reset the deadline", fixture(desired="3+0x3 2+0x100"), "3m", 0),
        ("first creation needs no historical zero", fixture(desired="1+0x104", created="0+0x104"), "3m", 2),
        ("first creation still expires", fixture(desired="1+0x104", created="0+0x104"), "16m", 0),
        (
            "every independent activation is protected",
            fixture(desired="0+0x3 1+0x3 0+0x3 1+0x92"),
            "4m",
            2,
        ),
        ("missing series does not invent replicas", [], "3m", 0),
    ]
    # A replacement Pod has a new UID, but no new replica-demand increase.
    replaced = fixture()
    replaced.extend(
        {
            "series": row["series"].replace("first-uid", "replacement-uid").replace("-pod", "-replacement"),
            "values": "_ " * 40 + row["values"],
        }
        for row in fixture()
        if 'uid="first-uid"' in row["series"]
    )
    cases.append(("new Pod UID cannot renew an expired activation", replaced, "17m", 0))
    # One Ready replica and one initializing replica retain all three desired
    # replicas; AverageValue/threshold=2 yields exactly three, not six or one.
    mixed = fixture(desired="0+0x3 3+0x100")
    mixed.extend(
        {"series": row["series"].replace("first-uid", "ready-uid").replace("-pod", "-ready"), "values": "1+0x104"}
        for row in fixture(ready="1+0x104", phase="Running")
        if 'uid="first-uid"' in row["series"]
    )
    cases.append(("mixed readiness holds allocated count", mixed, "3m", 6))
    tests = {
        "rule_files": [],
        "evaluation_interval": "15s",
        "tests": [
            {
                "name": name,
                "interval": "15s",
                "input_series": rows,
                "promql_expr_test": [
                    {"expr": expression, "eval_time": at, "exp_samples": [{"labels": "{}", "value": expected}]}
                ],
            }
            for name, rows, at, expected in cases
        ],
    }
    path = tmp_path / "startup-retention.test.yaml"
    path.write_text(yaml.safe_dump(tests, sort_keys=False))
    result = subprocess.run(  # noqa: S603 - installed binary and locally generated test fixture, no shell
        [promtool, "test", "rules", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "updates",
    [
        {"namespace": 'models"}'},
        {"deployment": 'model"}'},
        {"timeout_seconds": 59},
        {"timeout_seconds": 7201},
        {"target_queue_depth": 0},
    ],
)
def test_startup_query_rejects_invalid_identity_and_bounds(updates: dict) -> None:
    args = {"namespace": "models", "deployment": "model-burst", "timeout_seconds": 900, "target_queue_depth": 1}
    with pytest.raises(ValueError):
        startup_retention_promql(**(args | updates))

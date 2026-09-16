from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/model_network_policy_transition.py"
SPEC = importlib.util.spec_from_file_location("model_network_policy_transition", SCRIPT)
assert SPEC and SPEC.loader
transition = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transition)

PROFILE = "gateway-zero-egress-tcp-8000-v1"
POLICY = f"fs2-runtime-profile-{PROFILE}"
IMAGE = {
    "repository": "registry.example.test/fs2/control-plane",
    "digest": "sha256:" + "a" * 64,
}


def prepare_contract() -> dict[str, object]:
    profiles = [PROFILE]
    return {
        "phase": "prepare",
        "cluster_id": "mk8scluster-test",
        "namespace": "fs2-models",
        "profiles": profiles,
        "profiles_sha256": transition.hashlib.sha256(json.dumps(profiles, separators=(",", ":")).encode()).hexdigest(),
        "allow_policy_names": [POLICY],
        "control_plane_image": IMAGE,
        "inventory_receipt_sha256": None,
    }


def deployment(*, name: str = "qwen3-8b", profile: str = PROFILE) -> dict[str, object]:
    labels = {
        "app.kubernetes.io/component": "model-runtime",
        "app.kubernetes.io/part-of": "fs2-serve",
        "fs2-serve.nebius.ai/network-profile": profile,
    }
    return {
        "metadata": {"name": name, "uid": f"uid-{name}", "labels": labels},
        "spec": {"template": {"metadata": {"labels": labels}}},
    }


def test_inventory_receipt_binds_every_profiled_deployment() -> None:
    receipt = transition.inventory_receipt(
        prepare_contract(),
        {"items": [deployment(name="qwen3-8b"), deployment(name="glm-5-2-fp8")]},
        captured_at="2026-09-16T18:00:00Z",
    )

    assert list(receipt["deployments"]) == ["glm-5-2-fp8", "qwen3-8b"]
    assert receipt["deployments"]["qwen3-8b"]["profile"] == PROFILE
    assert receipt["payload_sha256"] == transition._sha256(
        {key: value for key, value in receipt.items() if key != "payload_sha256"}
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item["metadata"]["labels"].pop("fs2-serve.nebius.ai/network-profile"),
        lambda item: item["spec"]["template"]["metadata"]["labels"].update(
            {"fs2-serve.nebius.ai/network-profile": "unknown"}
        ),
        lambda item: item["metadata"]["labels"].pop("app.kubernetes.io/component"),
    ],
)
def test_inventory_refuses_missing_mismatched_or_unknown_labels(mutate) -> None:
    item = deployment()
    mutate(item)
    with pytest.raises(transition.ReceiptError):
        transition.inventory_receipt(
            prepare_contract(),
            {"items": [item]},
            captured_at="2026-09-16T18:00:00Z",
        )


def test_inventory_refuses_empty_namespace() -> None:
    with pytest.raises(transition.ReceiptError, match="empty"):
        transition.inventory_receipt(
            prepare_contract(),
            {"items": []},
            captured_at="2026-09-16T18:00:00Z",
        )


def rollback_contract() -> dict[str, object]:
    contract = prepare_contract()
    contract.update(
        {
            "phase": "rollback-remove-deny",
            "inventory_receipt_sha256": "b" * 64,
        }
    )
    return contract


def test_deny_absent_receipt_requires_allow_profiles_and_no_deny() -> None:
    receipt = transition.deny_absent_receipt(
        rollback_contract(),
        {"items": [{"metadata": {"name": POLICY}}]},
        captured_at="2026-09-16T18:05:00Z",
    )

    assert receipt["default_deny_absent"] is True
    assert receipt["allow_policy_names"] == [POLICY]
    assert receipt["enforcement_payload_sha256"] == "b" * 64


@pytest.mark.parametrize(
    "items, message",
    [
        ([{"metadata": {"name": "default-deny"}}], "still present"),
        ([], "missing"),
    ],
)
def test_deny_absent_receipt_refuses_unsafe_rollback(items, message) -> None:
    with pytest.raises(transition.ReceiptError, match=message):
        transition.deny_absent_receipt(
            rollback_contract(),
            {"items": items},
            captured_at="2026-09-16T18:05:00Z",
        )

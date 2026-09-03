from __future__ import annotations

from copy import deepcopy

from fs2_serve.model_deployment_records import ModelDeploymentObservedStatus
from fs2_serve.postgres import _upgrade_legacy_model_deployment_status


def _legacy_status() -> dict[str, object]:
    return {
        "observed_generation": 1,
        "phase": "Cold",
        "spec_digest": f"sha256:{'a' * 64}",
        "retry_count": 0,
        "last_reconcile_time": "2026-09-03T00:00:00Z",
        "conditions": [],
        "fast_start": {
            "mode": "Fixed",
            "fallbackPolicy": "AllowLowerLevel",
            "requestedLevel": "L2",
            "qualifiedLevel": "Off",
            "assignedLevel": "Off",
            "requestedTargetSeconds": 120,
            "qualification": {
                "state": "Fallback",
                "reason": "RequestedLevelUnqualified",
                "message": "legacy evidence cannot qualify the requested level",
            },
            "pools": [
                {
                    "poolRef": "h100-reserved-8x",
                    "qualifiedLevel": "Off",
                    "reason": "InsufficientBenchmarkSamples",
                    "mechanisms": ["shared-cache"],
                    "selectedMechanism": "shared-cache",
                    "selectedCompatibilityTupleDigest": f"sha256:{'b' * 64}",
                    "receiptDigests": [f"sha256:{'c' * 64}"],
                    "paths": [
                        {
                            "mechanism": "shared-cache",
                            "compatibilityTupleDigest": f"sha256:{'b' * 64}",
                            "qualifiedLevel": "Off",
                            "reason": "InsufficientBenchmarkSamples",
                            "receiptDigests": [f"sha256:{'c' * 64}"],
                        }
                    ],
                }
            ],
        },
    }


def test_legacy_unbound_path_is_retained_but_cannot_qualify() -> None:
    raw = _legacy_status()
    upgraded = _upgrade_legacy_model_deployment_status(raw)

    assert upgraded is not raw
    assert raw["fast_start"]["pools"][0]["paths"]
    pool = upgraded["fast_start"]["pools"][0]
    assert pool["paths"] == []
    assert "selectedMechanism" not in pool
    assert "selectedCompatibilityTupleDigest" not in pool
    retained = pool["retainedPaths"][0]
    assert retained["identityState"] == "LegacyUnbound"
    assert retained["observedPoolRef"] == "h100-reserved-8x"
    assert retained["mismatches"] == [{"code": "LegacyUnbound", "field": "$.identity"}]
    assert retained["receiptDigests"] == [f"sha256:{'c' * 64}"]

    decoded = ModelDeploymentObservedStatus.model_validate(upgraded)
    decoded_pool = decoded.fast_start.pools[0]
    assert decoded_pool.paths == []
    assert decoded_pool.selected_identity_digest is None
    assert decoded_pool.retained_paths[0].identity_digest is None


def test_current_identity_bound_path_is_unchanged() -> None:
    raw = _legacy_status()
    path = raw["fast_start"]["pools"][0]["paths"][0]
    path["identityDigest"] = f"sha256:{'d' * 64}"
    raw["fast_start"]["pools"][0]["selectedIdentityDigest"] = f"sha256:{'d' * 64}"
    before = deepcopy(raw)

    upgraded = _upgrade_legacy_model_deployment_status(raw)

    assert upgraded is raw
    assert upgraded == before
    ModelDeploymentObservedStatus.model_validate(upgraded)

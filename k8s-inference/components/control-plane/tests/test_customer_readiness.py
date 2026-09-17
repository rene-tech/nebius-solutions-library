from __future__ import annotations

import json
from pathlib import Path

import pytest

from fs2_serve.customer_readiness import (
    INDEX_SCHEMA,
    VERDICT_SCHEMA,
    CustomerReadinessFile,
    load_customer_readiness,
)


def _verdict(app_id: str = "cosmos3-nano", *, ready: bool = False) -> dict[str, object]:
    return {
        "schema": VERDICT_SCHEMA,
        "app_id": app_id,
        "evaluated_at": "2026-09-15T18:00:00+00:00",
        "valid_until": "2099-09-16T18:00:00+00:00",
        "release_identity": {
            "source_revision": "a" * 40,
            "runtime_images": {"control-plane": "sha256:" + "b" * 64},
            "configuration_sha256": "sha256:" + "c" * 64,
            "model_revision": "cosmos@revision",
            "client_build_sha256": "sha256:" + "d" * 64,
            "tenant_policy_sha256": "sha256:" + "e" * 64,
            "public_endpoint": "https://inference.example.test/mcp",
            "public_tool_catalog_sha256": "sha256:" + "f" * 64,
        },
        "verdict": "customer-ready" if ready else "not-ready",
        "ready": ready,
        "required_capability_ids": ["video-to-video"],
        "excluded_by_explicit_decision": [],
        "capabilities": [
            {
                "capability_id": "video-to-video",
                "advertised": True,
                "requested": True,
                "required": True,
                "state": "qualified" if ready else "untested",
                "scenarios": [
                    {
                        "scenario_id": "v2v-url",
                        "state": "qualified" if ready else "untested",
                        "evidence_id": "evidence-v2v" if ready else None,
                        "reasons": [] if ready else ["no evidence exists for the exact scenario"],
                    }
                ],
            }
        ],
    }


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_loads_a_single_gate_verdict_without_a_manual_wrapper(tmp_path: Path):
    loaded = load_customer_readiness(_write(tmp_path / "cosmos.json", _verdict()))
    assert loaded["cosmos3-nano"].verdict == "not-ready"
    assert loaded["cosmos3-nano"].capabilities[0].state == "untested"


def test_loads_an_index_for_multiple_independent_apps(tmp_path: Path):
    index = {
        "schema": INDEX_SCHEMA,
        "verdicts": [_verdict("cosmos3-nano", ready=True), _verdict("qwen3-8b", ready=True)],
    }
    loaded = load_customer_readiness(_write(tmp_path / "index.json", index))
    assert set(loaded) == {"cosmos3-nano", "qwen3-8b"}
    assert all(item.ready for item in loaded.values())


@pytest.mark.parametrize(
    "value",
    [
        {"schema": INDEX_SCHEMA, "verdicts": "not-a-list"},
        {"schema": VERDICT_SCHEMA, **{key: value for key, value in _verdict().items() if key != "app_id"}},
        {**_verdict(), "ready": True},
        {"schema": "unknown", "verdicts": []},
    ],
)
def test_invalid_or_self_contradictory_gate_output_fails_closed(tmp_path: Path, value: object):
    with pytest.raises(ValueError):
        load_customer_readiness(_write(tmp_path / "invalid.json", value))


def test_missing_file_has_no_implied_qualification(tmp_path: Path):
    assert load_customer_readiness(None) == {}
    assert load_customer_readiness(tmp_path / "missing.json") == {}


def test_projected_file_reloads_without_a_control_plane_restart(tmp_path: Path):
    path = tmp_path / "verdict-index.json"
    source = CustomerReadinessFile(path)
    assert source.read() == {}
    _write(path, _verdict(ready=False))
    assert source.read()["cosmos3-nano"].verdict == "not-ready"
    _write(path, _verdict(ready=True))
    assert source.read()["cosmos3-nano"].verdict == "customer-ready"
    path.unlink()
    assert source.read() == {}


def test_cached_positive_verdict_expires_fail_closed(tmp_path: Path):
    path = tmp_path / "verdict-index.json"
    value = _verdict(ready=True)
    value["evaluated_at"] = "2026-09-14T00:00:00+00:00"
    value["valid_until"] = "2026-09-15T00:00:00+00:00"
    source = CustomerReadinessFile(_write(path, value))
    expired = source.read()["cosmos3-nano"]
    assert expired.verdict == "not-ready" and not expired.ready
    assert expired.capabilities[0].state == "stale"
    assert expired.capabilities[0].scenarios[0].state == "stale"


def test_qualified_capability_in_expired_negative_verdict_also_becomes_stale(tmp_path: Path):
    path = tmp_path / "verdict-index.json"
    value = _verdict(ready=True)
    value["ready"] = False
    value["verdict"] = "not-ready"
    value["capabilities"].append(
        {
            "capability_id": "unqualified-capability",
            "advertised": True,
            "requested": True,
            "required": True,
            "state": "untested",
            "scenarios": [
                {
                    "scenario_id": "untested-scenario",
                    "state": "untested",
                    "evidence_id": None,
                    "reasons": ["no evidence exists for the exact scenario"],
                }
            ],
        }
    )
    value["required_capability_ids"].append("unqualified-capability")
    value["required_capability_ids"].sort()
    value["evaluated_at"] = "2026-09-14T00:00:00+00:00"
    value["valid_until"] = "2026-09-15T00:00:00+00:00"
    source = CustomerReadinessFile(_write(path, value))
    expired = source.read()["cosmos3-nano"]
    assert expired.capabilities[0].state == "stale"
    assert expired.capabilities[1].state == "untested"

"""Offline public media preparation tests; no key issuance, cluster, or inference."""

import hashlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import public_media as runner
import pytest
import yaml
from jsonschema import Draft202012Validator

from fs2_serve.model_input_contracts import _cosmos_mode_schema


def test_rendered_adapter_pin_differs_from_preview_only_by_one_terminal_newline():
    documents = yaml.safe_load_all((runner.ROOT / "models/general-media/k8s/cosmos3-nano.yaml").read_text())
    config = next(
        row
        for row in documents
        if row and row.get("kind") == "ConfigMap" and row["metadata"]["name"] == "cosmos3-nano-adapter"
    )
    source = config["data"]["adapter.py"].encode()
    assert source.endswith(b"\n") and not source.endswith(b"\n\n")
    assert hashlib.sha256(source).hexdigest() == runner.SOURCE_ADAPTER_SHA256
    assert hashlib.sha256(source[:-1]).hexdigest() == runner.ADAPTER_SHA256


def canary():
    token = "synthetic-canary-for-offline-test"  # noqa: S105 - inert offline fixture, never a platform credential
    row = {
        "id": "00000000-0000-4000-8000-000000000123",
        "name": runner.PREFIX + "test",
        "principal_id": runner.PREFIX + "test",
        "tenant_id": "robotics",
        "models": ["cosmos3-nano"],
        "scopes": [
            "catalog.read",
            "inference.invoke",
            "mcp.invoke",
            "operations.acknowledge",
            "operations.cancel",
            "operations.read",
            "operations.result",
        ],
        "max_concurrency": 1,
        "request_budget": None,
        "gpu_seconds_budget": None,
        "rate_limit_requests": None,
        "rate_window_seconds": None,
        "revoked_at": None,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "fingerprint": hashlib.sha256(token.encode()).hexdigest(),
    }
    return {"schema": "fs2-customer-key/v1", "disposable": True, "token_id": row["id"], "secret": token}, {
        "token_metadata": [row],
        "team_policy": runner.policy(row),
    }


def test_exact_cosmos_only_concurrency_one_policy_without_scope_expansion():
    key, snapshot = canary()
    assert runner.validate_key(key, snapshot) == key["secret"]
    assert "artifacts.write" not in snapshot["team_policy"]["scopes"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("models", ["*"]),
        ("max_concurrency", 2),
        ("tenant_id", "stockholm"),
        ("principal_id", "timmothy"),
        ("name", "timmothy-cosmos3"),
        ("revoked_at", "now"),
        ("expires_at", None),
        ("scopes", ["inference.invoke", "artifacts.write"]),
    ],
)
def test_changed_or_existing_customer_key_refused(field, value):
    key, snapshot = canary()
    snapshot["token_metadata"][0][field] = value
    with pytest.raises(ValueError):
        runner.validate_key(key, snapshot)


@pytest.mark.parametrize("mode", ["video-to-video", "transfer-video"])
@pytest.mark.parametrize(
    "reference",
    [
        runner.SOURCE_URL,
        {
            "artifact_id": "00000000-0000-4000-8000-000000000123",
            "sha256": runner.SOURCE_SHA256,
            "size_bytes": 25013,
            "media_type": "video/mp4",
            "compression": "none",
        },
    ],
)
def test_public_payloads_match_mode_contract(mode, reference):
    value = runner.payload(mode, reference)
    Draft202012Validator(_cosmos_mode_schema(mode)).validate(value)
    assert value["num_frames"] == 33 and value["fps"] == 20
    assert value["num_inference_steps"] == 35 and value["guidance_scale"] == 6


def test_release_comparison_ignores_warmth_but_not_adapter_identity():
    snapshot = {
        "deployments": [{"images": {"control-plane": "image@sha256:test"}}],
        "mounted_configuration_sha256": {},
        "team_policy": {},
        "adapter_sha256": runner.ADAPTER_SHA256,
        "serving": [{"name": "cosmos", "replicas": 0, "ready": 0, "spec_digest": "same"}],
    }
    hot = deepcopy(snapshot)
    hot["serving"][0].update(replicas=1, ready=1)
    assert runner.release(snapshot) == runner.release(hot)
    hot["adapter_sha256"] = "changed"
    assert runner.release(snapshot) != runner.release(hot)


def test_https_fixture_is_pinned_without_signed_query_or_redirect_contract():
    assert "@b0e54e88c322695dab188e6ed160c4d6d071c39d/" in runner.SOURCE_URL
    assert "?" not in runner.SOURCE_URL
    assert len(runner.SOURCE_SHA256) == 64

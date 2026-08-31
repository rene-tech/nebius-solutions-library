from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import CATALOG_ROOT, REPO_ROOT, _canonical_fixture_builder
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fs2_serve_catalog.attestations import (
    create_signed_attestation,
    public_key_id,
    public_key_value,
)

from fs2_serve.mcp_server import _protocol_tool_names
from fs2_serve.registry import Registry
from fs2_serve.route_revalidation import RouteRevalidator


def reloadable_registry(tmp_path: Path) -> tuple[Registry, Any, Path, dict[str, str]]:
    catalog_root = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, catalog_root)
    builder = _canonical_fixture_builder()
    catalog, evidence_root, qualification, _ = builder.live_fixture(catalog_root)
    builder.secure_evidence_tree(evidence_root)
    binding_path = tmp_path / "serving-bindings.json"
    binding_path.write_text(json.dumps(builder.binding_value(catalog, qualification)) + "\n", encoding="utf-8")
    trust = dict(builder.trusted_attestors)
    registry = Registry.load(
        catalog_root,
        binding_path,
        repo_root=REPO_ROOT,
        evidence_root=evidence_root,
        trusted_attestors_loader=lambda: dict(trust),
        validation_time=builder.validation_time,
        max_attempts=2,
        max_gpu_seconds_per_attempt=5,
        retry_base_seconds=0.01,
    )
    return registry, builder, evidence_root, trust


def test_expiry_after_startup_atomically_withdraws_listing_http_batch_and_mcp(tmp_path: Path) -> None:
    registry, builder, _, _ = reloadable_registry(tmp_path)
    assert [model.id for model in registry.list(enabled_only=True)] == ["qwen3-8b"]
    assert _protocol_tool_names(registry.get("qwen3-8b")) == {"qwen3_8b_openai_chat"}

    assert not registry.revalidate(validation_time=builder.validation_time + timedelta(hours=1))
    assert registry.list(enabled_only=True) == []
    assert registry.allowed(frozenset({"*"})) == []
    with pytest.raises(RuntimeError, match="not routable"):
        registry.get("qwen3-8b")
    assert _protocol_tool_names(registry.get("qwen3-8b", require_enabled=False)) == set()
    assert registry.validation_health()["healthy"] is False


def test_attestor_rotation_removes_old_routes_and_recovers_only_after_resigning(tmp_path: Path) -> None:
    registry, builder, evidence_root, trust = reloadable_registry(tmp_path)
    old_key_id = next(iter(trust))
    replacement = Ed25519PrivateKey.generate()
    replacement_key_id = public_key_id(replacement.public_key())
    trust[replacement_key_id] = public_key_value(replacement.public_key())
    assert registry.revalidate(validation_time=builder.validation_time)

    del trust[old_key_id]
    assert not registry.revalidate(validation_time=builder.validation_time)
    assert registry.list(enabled_only=True) == []

    for path in sorted((evidence_root / "attestations").rglob("*.json")):
        builder.replace_attestation(path, private_key=replacement)
    assert registry.revalidate(validation_time=builder.validation_time)
    assert [model.id for model in registry.list(enabled_only=True)] == ["qwen3-8b"]
    assert registry.validation_health()["healthy"] is True


def test_wrong_session_and_replayed_nonce_withdraw_the_whole_snapshot(tmp_path: Path) -> None:
    wrong_root = tmp_path / "wrong-session"
    registry, builder, evidence_root, _ = reloadable_registry(wrong_root)
    target = next(iter(sorted((evidence_root / "attestations").rglob("*.json"))))
    builder.replace_attestation(target, session_id=hashlib.sha256(b"wrong-session").hexdigest())
    assert not registry.revalidate(validation_time=builder.validation_time)
    assert registry.list(enabled_only=True) == []

    replay_root = tmp_path / "replayed-nonce"
    registry, builder, evidence_root, _ = reloadable_registry(replay_root)
    paths = sorted((evidence_root / "attestations").rglob("*.json"))
    donor = json.loads(paths[0].read_text(encoding="utf-8"))
    target_path = paths[1]
    target = json.loads(target_path.read_text(encoding="utf-8"))
    subject = target["subject"]
    replay = create_signed_attestation(
        private_key=builder.attestor,
        session_id=target["session_id"],
        nonce=donor["nonce"],
        issued_at=target["issued_at"],
        expires_at=target["expires_at"],
        kind=subject["kind"],
        subject_schema=subject["schema"],
        subject_digest=subject["digest"],
        model_id=subject["model_id"],
        claims=target["claims"],
    )
    target_path.write_text(json.dumps(replay) + "\n", encoding="utf-8")
    assert not registry.revalidate(validation_time=builder.validation_time)
    assert registry.list(enabled_only=True) == []


@pytest.mark.asyncio
async def test_periodic_revalidator_observes_projected_key_removal(tmp_path: Path) -> None:
    registry, _, _, trust = reloadable_registry(tmp_path)
    revalidator = RouteRevalidator(registry, interval_seconds=1)
    await revalidator.start()
    try:
        trust.clear()
        async with asyncio.timeout(2):
            while registry.list(enabled_only=True):
                await asyncio.sleep(0.02)
        assert revalidator.health()["healthy"] is False
    finally:
        await revalidator.close()

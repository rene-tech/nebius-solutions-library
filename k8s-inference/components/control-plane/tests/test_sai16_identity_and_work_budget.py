from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from fs2_serve.access_models import (
    OperatorPrincipalCreate,
    OperatorRole,
    PrincipalKind,
    ReleaseIdentityCapability,
    ReleaseIdentityPurpose,
    SessionExchangeAdmission,
)
from fs2_serve.auth import OperatorSessionService, PasswordWorkCapacityError, PepperRing
from fs2_serve.client_source import ClientSourceError, TrustedClientSource
from fs2_serve.memory_store import MemoryStore
from fs2_serve.release_identity import ReleaseIdentityError
from release_identity_testkit import ReleaseAuthorityFixture

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_release_assertion_is_short_lived_audience_and_capability_bound() -> None:
    authority = ReleaseAuthorityFixture.create()
    list_assertion = authority.bearer(ReleaseIdentityCapability.TOKENS_LIST)
    verified = authority.verifier.verify(
        list_assertion,
        capability=ReleaseIdentityCapability.TOKENS_LIST,
        purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
    )
    assert verified.assertion.subject == authority.subject
    with pytest.raises(ReleaseIdentityError):
        authority.verifier.verify(
            list_assertion,
            capability=ReleaseIdentityCapability.TOKENS_REVOKE,
            purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
        )
    expired = authority.bearer(
        ReleaseIdentityCapability.TOKENS_LIST,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    with pytest.raises(ReleaseIdentityError):
        authority.verifier.verify(
            expired,
            capability=ReleaseIdentityCapability.TOKENS_LIST,
            purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
        )
    with pytest.raises(ReleaseIdentityError):
        authority.verifier.verify(
            authority.bearer(ReleaseIdentityCapability.MODELS_BOOTSTRAP),
            capability=ReleaseIdentityCapability.MODELS_BOOTSTRAP,
            purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
        )
    resource_bound = authority.bearer(
        ReleaseIdentityCapability.MODELS_BOOTSTRAP,
        resource_sha256="2" * 64,
    )
    assert authority.verifier.verify(
        resource_bound,
        capability=ReleaseIdentityCapability.MODELS_BOOTSTRAP,
        purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
    ).assertion.resource_sha256 == "2" * 64
    with pytest.raises(ReleaseIdentityError):
        authority.verifier.verify(
            authority.bearer(
                ReleaseIdentityCapability.OPERATOR_ENROLL,
                operator={
                    "principal_id": str(uuid4()),
                    "subject": "operator:tenant-viewer",
                    "display_name": "Tenant viewer",
                    "role": "viewer",
                    "tenant_id": "tenant-a",
                    "mode": "create",
                },
            ),
            capability=ReleaseIdentityCapability.OPERATOR_ENROLL,
            purpose=ReleaseIdentityPurpose.OPERATOR_ENROLLMENT,
        )


def test_static_bootstrap_is_absent_from_runtime_and_model_bootstrap_uses_one_use_assertion() -> None:
    helpers = (
        REPOSITORY_ROOT
        / "charts/control-plane/fs2-serve-control-plane/templates/_helpers.tpl"
    ).read_text(encoding="utf-8")
    runtime = (REPOSITORY_ROOT / "components/control-plane/src/fs2_serve/api.py").read_text(
        encoding="utf-8"
    )
    bootstrap = (REPOSITORY_ROOT / "stages/workloads/model_controller.tf").read_text(
        encoding="utf-8"
    )
    assert "FS2_ADMIN_TOKEN_FILE" not in helpers
    assert "/var/run/secrets/fs2-serve/admin-token" not in helpers
    assert "admin_token:" not in runtime
    assert "/admin/api/v1/release/model-bootstrap" in bootstrap
    assert "release-assertion" in bootstrap
    assert "backoff_limit           = 0" in bootstrap
    assert "/var/run/fs2-admin" not in bootstrap


def test_trusted_proxy_source_is_canonical_and_untrusted_forwarding_is_ignored() -> None:
    resolver = TrustedClientSource(["10.20.0.0/24"])
    assert resolver.resolve(peer="10.20.0.9", forwarded="203.0.113.7") == "203.0.113.7"
    assert resolver.resolve(peer="192.0.2.9", forwarded="198.51.100.8") == "192.0.2.9"
    with pytest.raises(ClientSourceError):
        resolver.resolve(peer="10.20.0.9", forwarded=None)
    with pytest.raises(ClientSourceError):
        resolver.resolve(peer="10.20.0.9", forwarded="203.0.113.7, 10.20.0.9")


@pytest.mark.asyncio
async def test_source_and_aggregate_limits_are_independent_and_fingerprints_are_keyed(cipher, hasher) -> None:
    store = MemoryStore(cipher, hasher)
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    sessions = OperatorSessionService(store, peppers)
    fingerprint = sessions.source_fingerprint("203.0.113.7")
    assert fingerprint != "203.0.113.7"
    assert fingerprint != sessions.source_fingerprint("203.0.113.8")
    now = datetime.now(UTC)
    for attempt in range(5):
        assert await store.consume_operator_session_exchange(
            fingerprint,
            attempted_at=now,
            window_seconds=60,
            maximum_source_attempts=5,
            maximum_aggregate_attempts=10,
        ) is SessionExchangeAdmission.ADMITTED
    assert await store.consume_operator_session_exchange(
        fingerprint,
        attempted_at=now,
        window_seconds=60,
        maximum_source_attempts=5,
        maximum_aggregate_attempts=10,
    ) is SessionExchangeAdmission.SOURCE_THROTTLED
    for index in range(5):
        assert await store.consume_operator_session_exchange(
            sessions.source_fingerprint(f"198.51.100.{index + 1}"),
            attempted_at=now,
            window_seconds=60,
            maximum_source_attempts=5,
            maximum_aggregate_attempts=10,
        ) is SessionExchangeAdmission.ADMITTED
    assert await store.consume_operator_session_exchange(
        sessions.source_fingerprint("192.0.2.1"),
        attempted_at=now,
        window_seconds=60,
        maximum_source_attempts=5,
        maximum_aggregate_attempts=10,
    ) is SessionExchangeAdmission.AGGREGATE_THROTTLED


@pytest.mark.asyncio
async def test_argon2_verification_uses_one_bounded_worker_without_blocking_event_loop(
    cipher,
    hasher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(cipher, hasher)
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    sessions = OperatorSessionService(store, peppers, credential_work_concurrency=1)
    principal_id = uuid4()
    await store.create_operator_principal(
        principal_id=principal_id,
        request=OperatorPrincipalCreate(
            subject="operator:bounded-work",
            display_name="Bounded work",
            kind=PrincipalKind.HUMAN,
            role=OperatorRole.ADMIN,
            tenant_id=None,
        ),
        actor="test",
    )
    credential = (await sessions.rotate_credential(principal_id, actor="test")).credential
    original_verify = sessions._hasher.verify
    release = threading.Event()
    entered = threading.Event()
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def blocked_verify(digest: str, prehash: str) -> bool:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        entered.set()
        release.wait(timeout=5)
        try:
            return original_verify(digest, prehash)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sessions._hasher, "verify", blocked_verify)
    first = asyncio.create_task(sessions.authenticate_credential(credential))
    assert await asyncio.to_thread(entered.wait, 2)
    with pytest.raises(PasswordWorkCapacityError):
        await sessions.authenticate_credential(credential)
    event_loop_progress = False
    await asyncio.sleep(0)
    event_loop_progress = True
    assert event_loop_progress
    assert maximum_active == 1
    release.set()
    assert (await first).id == principal_id
    assert maximum_active == 1


@pytest.mark.asyncio
async def test_argon2_hashing_runs_off_the_event_loop(cipher, hasher, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(cipher, hasher)
    peppers = PepperRing(active_key_id="pepper-v1", keys={"pepper-v1": b"p" * 32})
    sessions = OperatorSessionService(store, peppers, credential_work_concurrency=1)
    principal_id = uuid4()
    await store.create_operator_principal(
        principal_id=principal_id,
        request=OperatorPrincipalCreate(
            subject="operator:bounded-hash",
            display_name="Bounded hash",
            kind=PrincipalKind.HUMAN,
            role=OperatorRole.ADMIN,
            tenant_id=None,
        ),
        actor="test",
    )
    original_hash = sessions._hasher.hash
    worker_names: list[str] = []

    def observed_hash(prehash: str) -> str:
        worker_names.append(threading.current_thread().name)
        return original_hash(prehash)

    monkeypatch.setattr(sessions._hasher, "hash", observed_hash)
    await sessions.rotate_credential(principal_id, actor="test")
    assert worker_names and all(name.startswith("fs2-operator-argon2") for name in worker_names)

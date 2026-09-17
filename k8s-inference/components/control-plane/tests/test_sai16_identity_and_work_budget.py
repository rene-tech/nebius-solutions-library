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
        resource_generation="release-test-01",
    )
    verified_resource = authority.verifier.verify(
        resource_bound,
        capability=ReleaseIdentityCapability.MODELS_BOOTSTRAP,
        purpose=ReleaseIdentityPurpose.ADMIN_AUTOMATION,
    ).assertion
    assert verified_resource.resource_sha256 == "2" * 64
    assert verified_resource.resource_generation == "release-test-01"
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
    assert "backoff_limit               = 0" in bootstrap
    assert "backoff_limit           = each.value.identity.job_contract.backoff_limit" in bootstrap
    assert "/var/run/fs2-admin" not in bootstrap


def test_model_bootstrap_recovery_is_generation_keyed_and_retains_job_history() -> None:
    bootstrap = (REPOSITORY_ROOT / "stages/workloads/model_controller.tf").read_text(encoding="utf-8")
    inventory = (REPOSITORY_ROOT / "stages/workloads/model_bootstrap_inventory.tf").read_text(
        encoding="utf-8"
    )
    variables = (REPOSITORY_ROOT / "stages/workloads/variables.tf").read_text(encoding="utf-8")
    outputs = (REPOSITORY_ROOT / "stages/workloads/outputs.tf").read_text(encoding="utf-8")
    assert "generation = var.release_identity_model_bootstrap_assertion_generation" in bootstrap
    assert "model_controller_bootstrap_current_spec" in bootstrap
    assert "payload_json" in bootstrap
    assert "bootstrap_script" in bootstrap
    assert "runtime_image" in bootstrap
    assert 'data "kubernetes_resources" "model_controller_bootstrap_configmaps"' in inventory
    assert 'data "kubernetes_resources" "model_controller_bootstrap_jobs"' in inventory
    assert inventory.count("import {") == 2
    assert "model_controller_bootstrap_verified_specs" in inventory
    assert "model_controller_bootstrap_verified_job_specs" in inventory
    assert "model-bootstrap-identity/v2" in bootstrap
    assert "model-bootstrap-job/v1" in bootstrap
    assert "base_labels                 = local.common_labels" in bootstrap
    assert "model_controller_bootstrap_inventory_valid" in bootstrap
    assert "generation_key == substr(sha256(jsonencode(spec.identity)), 0, 32)" in bootstrap
    assert "container.securityContext" in bootstrap
    assert "container.volumeMounts" in bootstrap
    assert "bootstrap_volume.configMap.name" in bootstrap
    assert "assertion_volume.secret.secretName" in bootstrap
    assert bootstrap.count("for_each = local.model_controller_bootstrap_assertions") == 2
    assert bootstrap.count("prevent_destroy = true") >= 2
    assert "ignore_changes  = [data]" not in bootstrap
    assert "ignore_changes  = [spec]" not in bootstrap
    assert "secret_name = each.value.secret_name" in bootstrap
    assert "image   = each.value.runtime_image" in bootstrap
    assert "ValidatingAdmissionPolicy" in bootstrap
    assert "object.immutable == true" in bootstrap
    assert 'name      = "fs2-model-bootstrap-${each.key}"' in bootstrap
    assert "release-signed UID/full-object-bound Kubernetes receipts" in variables
    assert "length(var.release_identity_model_bootstrap_retained_assertions) == 0" in variables
    assert "bootstrap_managed_generations" in outputs
    assert "bootstrap_retained_generations" in outputs
    assert 'bootstrap_inventory_authority   = "release-signed-uid-bound-kubernetes-inventory-v2"' in outputs


def test_postgres_session_exchange_uses_exact_bounded_sliding_state() -> None:
    store = (REPOSITORY_ROOT / "components/control-plane/src/fs2_serve/postgres.py").read_text(encoding="utf-8")
    migration = (
        REPOSITORY_ROOT
        / "components/control-plane/migrations/0033_session_exchange_sliding_window.sql"
    ).read_text(encoding="utf-8")
    normalized_store = " ".join(store.split())
    normalized_migration = " ".join(migration.split())
    assert "fs2_consume_session_exchange_sliding($1,$2,$3,$4)" in store
    assert '"bounded-exact-global-sliding-window-v2"' in store
    assert "FROM fs2_audit_events WHERE action='session.exchange.attempt'" not in normalized_store
    assert "admin-exchange-aggregate" not in store
    assert "_SESSION_EXCHANGE_SOURCE_CACHE_SIZE = 4096" in store
    assert "_SESSION_EXCHANGE_AGGREGATE_CACHE_SIZE = 64" in store
    assert "slot BETWEEN 0 AND 9999" in migration
    assert "source_fingerprint, admitted_at, sequence" in migration
    assert "item.admitted_at > v_cutoff" in migration
    assert "p_maximum_aggregate_attempts" in migration
    assert "session exchange limiter settings differ from bound state" in migration
    assert "aggregate_shard" not in migration
    assert "v_window_start" not in migration
    assert "floor(extract(epoch" not in migration
    assert "rejection_count" not in migration
    assert "Established source saturation is a read-only path" in migration
    assert "Only a request that can still be admitted reaches this cursor lock" in migration
    assert '"rejections_observed": "one_or_more"' in store
    assert '"exact_rejection_count_available": False' in store
    assert "CREATE INDEX fs2_session_exchange_admissions_time_idx" in migration
    assert "CREATE INDEX fs2_session_exchange_admissions_source_time_idx" in migration
    assert "CREATE INDEX fs2_session_exchange_rejection_evidence_forensics_idx" in migration
    assert "SECURITY DEFINER SET search_path = pg_catalog, public" in normalized_migration
    assert "DELETE FROM fs2_session_exchange" not in migration


def test_postgres_session_exchange_cutover_is_shared_fail_closed_and_rollback_compatible() -> None:
    migration = (
        REPOSITORY_ROOT
        / "components/control-plane/migrations/0034_session_exchange_cutover_bridge.sql"
    ).read_text(encoding="utf-8")
    store = (REPOSITORY_ROOT / "components/control-plane/src/fs2_serve/postgres.py").read_text(encoding="utf-8")
    normalized = " ".join(migration.split())
    assert "fs2.preexisting_schema_version" in migration
    assert "<> '__fresh__'" in migration
    assert ">= '0032_session_exchange_buckets.sql'" not in migration
    assert "cutover_required" in migration
    assert "state.migration_started_at + make_interval(secs => p_window_seconds)" in normalized
    assert "v_now < v_state.cutover_not_before" in migration
    assert "cutover_quiescence" in migration
    assert "fs2_consume_session_exchange_bridge($1,$2,$3,$4)" in migration
    assert "CREATE OR REPLACE FUNCTION fs2_consume_session_exchange(" in migration
    assert "CREATE FUNCTION fs2_consume_session_exchange_sliding(" in migration
    assert migration.count("GRANT EXECUTE ON FUNCTION fs2_consume_session_exchange") >= 2
    assert "REVOKE EXECUTE ON FUNCTION fs2_consume_session_exchange(" not in migration
    assert "set_config('fs2.preexisting_schema_version',$1,true)" in store
    assert "legacy signature is intentionally retained" in store


def test_model_bootstrap_history_requires_signed_uid_bound_full_object_receipts() -> None:
    bootstrap = (REPOSITORY_ROOT / "stages/workloads/model_controller.tf").read_text(encoding="utf-8")
    inventory = (REPOSITORY_ROOT / "stages/workloads/model_bootstrap_inventory.tf").read_text(encoding="utf-8")
    verifier = (
        REPOSITORY_ROOT / "stages/workloads/scripts/verify_model_bootstrap_receipt.py"
    ).read_text(encoding="utf-8")
    assert 'data "external" "model_controller_bootstrap_receipt"' in inventory
    assert "model_controller_bootstrap_verified_specs" in inventory
    assert "observed_object_sha256 = sha256(jsonencode" in bootstrap
    assert bootstrap.count("observed_object_sha256 = sha256(jsonencode(item))") == 2
    assert "config_map_uid" in bootstrap
    assert "job_uid" in bootstrap
    assert "release_assertion_fingerprint" in verifier
    assert "public_key.verify" in verifier
    assert "fs2-model-bootstrap-retention+jws" in verifier
    assert "request.operation == 'CREATE'" in bootstrap
    assert "system:serviceaccount:fs2-system:fs2-release-identity" in bootstrap
    assert "model_controller_bootstrap_history_policy_binding" in bootstrap
    assert "model_controller_bootstrap_trust_policy_binding" in bootstrap
    assert "model_controller_bootstrap_receipt_policy_binding" in bootstrap
    assert "fs2-serve-release-identity-trust" in bootstrap
    assert "request.name.startsWith('fs2-model-bootstrap-')" in bootstrap
    assert "for_each = local.model_controller_bootstrap_verified_specs" in inventory


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
            maximum_aggregate_attempts=200,
        ) is SessionExchangeAdmission.ADMITTED
    assert await store.consume_operator_session_exchange(
        fingerprint,
        attempted_at=now,
        window_seconds=60,
        maximum_source_attempts=5,
        maximum_aggregate_attempts=200,
    ) is SessionExchangeAdmission.SOURCE_THROTTLED
    with pytest.raises(RuntimeError, match="settings differ from bound state"):
        await store.consume_operator_session_exchange(
            fingerprint,
            attempted_at=now,
            window_seconds=61,
            maximum_source_attempts=5,
            maximum_aggregate_attempts=200,
        )

    aggregate_store = MemoryStore(cipher, hasher)
    candidates = [f"{index:064x}" for index in range(1, 112)]
    for candidate in candidates[:10]:
        assert await aggregate_store.consume_operator_session_exchange(
            candidate,
            attempted_at=now,
            window_seconds=60,
            maximum_source_attempts=5,
            maximum_aggregate_attempts=10,
        ) is SessionExchangeAdmission.ADMITTED
    assert await aggregate_store.consume_operator_session_exchange(
        candidates[10],
        attempted_at=now,
        window_seconds=60,
        maximum_source_attempts=5,
        maximum_aggregate_attempts=10,
    ) is SessionExchangeAdmission.AGGREGATE_THROTTLED
    aggregate_rejections = [
        event
        for event in aggregate_store.audit
        if event.action == "session.exchange.attempt"
        and event.outcome == str(SessionExchangeAdmission.AGGREGATE_THROTTLED)
    ]
    for candidate in candidates[11:111]:
        assert await aggregate_store.consume_operator_session_exchange(
            candidate,
            attempted_at=now,
            window_seconds=60,
            maximum_source_attempts=5,
            maximum_aggregate_attempts=10,
        ) is SessionExchangeAdmission.AGGREGATE_THROTTLED
    assert [
        event
        for event in aggregate_store.audit
        if event.action == "session.exchange.attempt"
        and event.outcome == str(SessionExchangeAdmission.AGGREGATE_THROTTLED)
    ] == aggregate_rejections


@pytest.mark.asyncio
async def test_exact_sliding_boundary_and_evidence_collisions_never_change_admission(
    cipher, hasher
) -> None:
    store = MemoryStore(cipher, hasher)
    started_at = datetime.fromtimestamp(1_800_000_000, tz=UTC)
    source_a = "00000001000000020000" + "1" * 44
    source_b = "00000001000000020000" + "2" * 44
    unrelated = "00000003000000040000" + "4" * 44
    for source in (source_a, source_b):
        assert await store.consume_operator_session_exchange(
            source,
            attempted_at=started_at,
            window_seconds=60,
            maximum_source_attempts=1,
            maximum_aggregate_attempts=100,
        ) is SessionExchangeAdmission.ADMITTED
    assert await store.consume_operator_session_exchange(
        source_a,
        attempted_at=started_at + timedelta(seconds=1),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.SOURCE_THROTTLED
    assert await store.consume_operator_session_exchange(
        source_b,
        attempted_at=started_at + timedelta(seconds=1),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.SOURCE_THROTTLED
    assert await store.consume_operator_session_exchange(
        unrelated,
        attempted_at=started_at + timedelta(seconds=1),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.ADMITTED

    boundary_store = MemoryStore(cipher, hasher)
    assert await boundary_store.consume_operator_session_exchange(
        source_a,
        attempted_at=started_at + timedelta(seconds=59),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.ADMITTED
    assert await boundary_store.consume_operator_session_exchange(
        source_a,
        attempted_at=started_at + timedelta(seconds=60),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.SOURCE_THROTTLED
    assert await boundary_store.consume_operator_session_exchange(
        source_a,
        attempted_at=started_at + timedelta(seconds=119),
        window_seconds=60,
        maximum_source_attempts=1,
        maximum_aggregate_attempts=100,
    ) is SessionExchangeAdmission.ADMITTED


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

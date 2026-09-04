"""PostgreSQL durable admission queue, encrypted payload store, and ledger."""

from __future__ import annotations

import asyncio
import base64
import copy
import functools
import hashlib
import json
import secrets
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, ParamSpec, TypeVar, cast
from uuid import UUID, uuid4

import asyncpg

from .access_models import (
    AdminApiKeyPolicyPatch,
    AdminKeyUsageRecord,
    OperatorPrincipal,
    OperatorPrincipalCreate,
    OperatorPrincipalPatch,
    OperatorSession,
    OperatorSessionRecord,
)
from .activation_postgres import PostgresActivationStore
from .admin_models import (
    AdminActivationPhase,
    AdminModelActivity,
    AdminOperationQuery,
    AdminOperationRecord,
    AdminUsageRow,
    AdminUsageWindow,
)
from .configuration import (
    TERRAFORM_BOOTSTRAP_ACTOR,
    configuration_etag,
    validate_terraform_apply_correlation,
)
from .configuration_models import (
    ConfigurationPlan,
    ConfigurationRevision,
    PlatformConfiguration,
    ReconciliationPhase,
    ReconciliationStatus,
    TerraformApplyReceipt,
)
from .crypto import Ciphertext, KeyedHasher, PayloadCipher
from .model_deployment import DesiredState, ModelDeploymentSpec, spec_digest
from .model_deployment_records import (
    ModelDeploymentAppendRequest,
    ModelDeploymentAppendResult,
    ModelDeploymentObservedStatus,
    ModelDeploymentRevision,
    ModelDeploymentRevisionAction,
    ModelDeploymentStatusObservation,
    model_deployment_append_payload,
    model_deployment_audit_target,
    model_deployment_status_precedes,
)
from .models import (
    ActivationIntent,
    ActivationLeaderIdentity,
    ActivationTargetState,
    AdmissionRequest,
    AuditEvent,
    ClaimedActivationIntent,
    ClaimedOperation,
    DynamicAdmissionFence,
    ModalityUsage,
    OperationResult,
    OperationStatus,
    OperationView,
    PendingScientificAdmission,
    Principal,
    ReportedUsage,
    RuntimeIdentity,
    TerminalAccounting,
    TokenCreate,
    TokenView,
)
from .postgresql_release import validate_migration_set
from .runtime import sanitize_error_detail
from .store import (
    BudgetExceededError,
    ConcurrencyExceededError,
    ConflictError,
    NotFoundError,
    RateLimitExceededError,
    StaleLeaseError,
)

_TERMINAL = {"succeeded", "failed", "cancelled", "preempted", "expired"}
_CLAIM_BATCH_SIZE = 16
_MAX_AUDIT_DETAIL_CHARS = 64 * 1024
P = ParamSpec("P")
R = TypeVar("R")


def _reject_nonstandard_json_constant(_: str) -> NoReturn:
    raise ValueError


def _decode_audit_detail(value: object) -> dict[str, Any]:
    """Normalize default asyncpg JSONB text without reflecting stored content."""

    if isinstance(value, str):
        if len(value) > _MAX_AUDIT_DETAIL_CHARS:
            raise RuntimeError("stored audit detail is invalid")
        try:
            value = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except (RecursionError, ValueError):
            raise RuntimeError("stored audit detail is invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("stored audit detail is not an object")
    return cast(dict[str, Any], value)


def _decode_modality_usage(value: object) -> list[ModalityUsage]:
    if value is None:
        return []
    if isinstance(value, str):
        if len(value) > 4096:
            raise RuntimeError("stored modality usage is invalid")
        try:
            value = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except (RecursionError, ValueError):
            raise RuntimeError("stored modality usage is invalid") from None
    if not isinstance(value, list) or len(value) > 32:
        raise RuntimeError("stored modality usage is invalid")
    return [ModalityUsage.model_validate(item) for item in value]


def _decode_configuration_json(value: object, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value) > 16 * 1024 * 1024:
            raise RuntimeError(f"stored {label} is invalid")
        try:
            value = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except (RecursionError, ValueError):
            raise RuntimeError(f"stored {label} is invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"stored {label} is not an object")
    return cast(dict[str, Any], value)


def _upgrade_legacy_model_deployment_status(value: dict[str, Any]) -> dict[str, Any]:
    """Retain pre-identity fast-start paths without letting them qualify.

    Status observations written before runtime evidence identities became
    mandatory contain otherwise useful measurements but cannot prove that
    they match the current runtime.  Move those paths to ``retainedPaths`` at
    the persistence boundary.  Unrelated malformed data is deliberately left
    untouched so the strict status decoder still rejects it.
    """

    fast_start = value.get("fast_start", value.get("fastStart"))
    if not isinstance(fast_start, dict):
        return value
    pools = fast_start.get("pools")
    if not isinstance(pools, list):
        return value

    needs_upgrade = False
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        paths = pool.get("paths")
        if not isinstance(paths, list):
            continue
        if any(
            isinstance(path, dict)
            and "identity_digest" not in path
            and "identityDigest" not in path
            for path in paths
        ):
            needs_upgrade = True
            break
    if not needs_upgrade:
        return value

    upgraded = copy.deepcopy(value)
    upgraded_fast_start = upgraded.get("fast_start", upgraded.get("fastStart"))
    assert isinstance(upgraded_fast_start, dict)
    upgraded_pools = upgraded_fast_start.get("pools")
    assert isinstance(upgraded_pools, list)
    changed = False

    for pool in upgraded_pools:
        if not isinstance(pool, dict):
            continue
        paths = pool.get("paths")
        if not isinstance(paths, list):
            continue
        retained_key = "retained_paths" if "retained_paths" in pool else "retainedPaths"
        existing_retained = pool.get(retained_key)
        if existing_retained is not None and not isinstance(existing_retained, list):
            # Do not conceal an unrelated corrupt retained-path value.
            continue

        active_paths: list[object] = []
        legacy_paths: list[dict[str, Any]] = []
        for path in paths:
            if (
                not isinstance(path, dict)
                or "identity_digest" in path
                or "identityDigest" in path
            ):
                active_paths.append(path)
                continue

            retained: dict[str, Any] = {
                "identityState": "LegacyUnbound",
                "reason": "LegacyIdentityUnbound",
                "mismatches": [{"code": "LegacyUnbound", "field": "$.identity"}],
            }
            for snake_name, camel_name in (
                ("mechanism", "mechanism"),
                ("compatibility_tuple_digest", "compatibilityTupleDigest"),
                ("receipt_digests", "receiptDigests"),
                ("model_start", "modelStart"),
                ("capacity_wait", "capacityWait"),
                ("end_to_end", "endToEnd"),
            ):
                if snake_name in path:
                    retained[camel_name] = path[snake_name]
                elif camel_name in path:
                    retained[camel_name] = path[camel_name]
            pool_ref = pool.get("pool_ref", pool.get("poolRef"))
            if pool_ref is not None:
                retained["observedPoolRef"] = pool_ref
            legacy_paths.append(retained)

        if not legacy_paths:
            continue
        changed = True
        pool["paths"] = active_paths
        if isinstance(existing_retained, list):
            pool[retained_key] = [*existing_retained, *legacy_paths]
        else:
            pool[retained_key] = legacy_paths
        if not active_paths:
            for field_name in (
                "selected_mechanism",
                "selectedMechanism",
                "selected_compatibility_tuple_digest",
                "selectedCompatibilityTupleDigest",
                "selected_identity_digest",
                "selectedIdentityDigest",
            ):
                pool.pop(field_name, None)

    return upgraded if changed else value


def retry_serialization(method: Any) -> Any:
    """Retry a whole idempotent transaction on PostgreSQL deadlock/serialization abort."""

    @functools.wraps(method)
    async def wrapped(self: PostgresStore, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                return await method(self, *args, **kwargs)
            except asyncpg.PostgresError as exc:
                if getattr(exc, "sqlstate", None) not in {"40P01", "40001"} or attempt == 2:
                    raise
                await asyncio.sleep(0.01 * (2**attempt))
        raise AssertionError("unreachable")

    return wrapped


class PostgresStore:
    @staticmethod
    async def _token_lock(connection: asyncpg.Connection[Any], token_id: UUID) -> None:
        key = int.from_bytes(hashlib.blake2b(token_id.bytes, digest_size=8).digest(), "big", signed=True)
        await connection.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)

    @staticmethod
    async def _configuration_lock(connection: asyncpg.Connection[Any]) -> None:
        await connection.execute("SELECT pg_advisory_xact_lock(727201920011)")

    @staticmethod
    async def _model_deployment_lock(
        connection: asyncpg.Connection[Any],
        namespace: str,
        name: str,
    ) -> None:
        identity = f"{namespace}\0{name}".encode()
        key = int.from_bytes(hashlib.blake2b(identity, digest_size=8).digest(), "big", signed=True)
        await connection.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)

    @staticmethod
    async def _model_deployment_idempotency_lock(
        connection: asyncpg.Connection[Any],
        actor_id: UUID,
        idempotency_key: str,
    ) -> None:
        identity = f"{actor_id}\0{idempotency_key}".encode()
        key = int.from_bytes(hashlib.blake2b(identity, digest_size=8).digest(), "big", signed=True)
        await connection.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)

    def __init__(
        self,
        pool: asyncpg.Pool[Any],
        migrations_dir: Path,
        cipher: PayloadCipher,
        hasher: KeyedHasher,
        payload_ttl_seconds: int,
    ) -> None:
        self.pool = pool
        self.migrations_dir = migrations_dir
        self.cipher = cipher
        self.hasher = hasher
        self.payload_ttl_seconds = payload_ttl_seconds
        self.activation = PostgresActivationStore(pool, owns_pool=False)

    @classmethod
    async def _connect_pool(
        cls,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        application_name: str = "fs2-serve-control-plane",
    ) -> asyncpg.Pool[Any]:
        dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            server_settings={"application_name": application_name},
        )
        assert pool is not None
        return pool

    @classmethod
    async def connect(
        cls,
        database_url: str,
        migrations_dir: Path,
        cipher: PayloadCipher,
        hasher: KeyedHasher,
        payload_ttl_seconds: int,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> PostgresStore:
        pool = await cls._connect_pool(database_url, min_size=min_size, max_size=max_size)
        return cls(pool, migrations_dir, cipher, hasher, payload_ttl_seconds)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> bool:
        async with self.pool.acquire() as connection:
            return bool(await connection.fetchval("SELECT true"))

    async def activation_controller_ready(self, activation_set_digest: str) -> bool:
        return await self.activation.activation_controller_ready(activation_set_digest)

    @staticmethod
    def _migration_manifest(migrations_dir: Path) -> list[tuple[Path, str]]:
        return validate_migration_set(migrations_dir)

    @classmethod
    async def _apply_migrations(
        cls,
        pool: asyncpg.Pool[Any],
        migrations_dir: Path,
        reporting_role: str = "fs2_serve_reporting",
        runtime_role: str = "fs2_serve_runtime",
        maintenance_role: str = "fs2_serve_maintenance",
        activation_role: str = "fs2_serve_activation",
    ) -> None:
        for label, role in (
            ("reporting", reporting_role),
            ("runtime", runtime_role),
            ("maintenance", maintenance_role),
            ("activation", activation_role),
        ):
            if not role.replace("_", "a").isalnum() or not 1 <= len(role) <= 63:
                raise ValueError(f"{label} database role is invalid")
        if len({reporting_role, runtime_role, maintenance_role, activation_role}) != 4:
            raise ValueError("reporting, runtime, maintenance, and activation database roles must differ")
        manifest = cls._migration_manifest(migrations_dir)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(727201920001)")
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fs2_schema_migrations (
                    version text PRIMARY KEY,
                    sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )
                """
            )
            applied_rows = await connection.fetch(
                "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY applied_at,version"
            )
            applied = [(str(row["version"]), str(row["sha256"])) for row in applied_rows]
            expected = [(path.name, digest) for path, digest in manifest]
            if [version for version, _ in applied] != [version for version, _ in expected[: len(applied)]]:
                raise RuntimeError("applied migration set is missing, extra, or reordered")
            for (version, digest), (_, expected_digest) in zip(applied, expected, strict=False):
                if digest != expected_digest:
                    raise RuntimeError(f"applied migration changed: {version}")
            for path, digest in manifest:
                payload = path.read_bytes()
                existing = await connection.fetchval(
                    "SELECT sha256 FROM fs2_schema_migrations WHERE version=$1", path.name
                )
                if existing is not None:
                    if existing != digest:
                        raise RuntimeError(f"applied migration changed: {path.name}")
                    continue
                await connection.execute(payload.decode("utf-8"))
                await connection.execute(
                    "INSERT INTO fs2_schema_migrations(version,sha256) VALUES($1,$2)",
                    path.name,
                    digest,
                )
            # A role-specific REVOKE does not cancel privileges inherited from
            # PUBLIC on databases created with older PostgreSQL defaults.
            # The service owns this dedicated schema, so close that inherited
            # DDL path before granting the narrowly scoped runtime roles.
            await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            for label, role in (
                ("reporting", reporting_role),
                ("runtime", runtime_role),
                ("maintenance", maintenance_role),
                ("activation", activation_role),
            ):
                can_login = await connection.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname=$1", role)
                if can_login is None:
                    await connection.execute(f'CREATE ROLE "{role}" NOLOGIN')
                elif can_login:
                    raise RuntimeError(f"{label} database group role must be NOLOGIN")
                await connection.execute(f'REVOKE CREATE ON SCHEMA public FROM "{role}"')
                await connection.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            quoted_reporting = f'"{reporting_role}"'
            quoted_runtime = f'"{runtime_role}"'
            quoted_maintenance = f'"{maintenance_role}"'
            quoted_activation = f'"{activation_role}"'
            all_roles = (quoted_reporting, quoted_runtime, quoted_maintenance, quoted_activation)
            for role in all_roles:
                await connection.execute(
                    f"REVOKE ALL ON fs2_schema_migrations,fs2_tokens,fs2_operations,"
                    f"fs2_operation_events,fs2_audit_events,fs2_usage_facts,"
                    f"fs2_operator_principals,fs2_operator_sessions,"
                    f"fs2_configuration_revisions,fs2_configuration_plans,"
                    f"fs2_configuration_reconciliation_events,"
                    f"fs2_model_deployment_revisions,fs2_model_deployments,"
                    f"fs2_model_deployment_idempotency,fs2_model_deployment_status_events,"
                    f"fs2_scientific_stage_attempts,fs2_scientific_artifacts,fs2_scientific_uploads,"
                    f"fs2_scientific_stage_commits,fs2_scientific_stage_commit_attempts,"
                    f"fs2_scientific_run_results,fs2_scientific_artifact_events,"
                    f"fs2_scientific_retention_ledger,fs2_scientific_batches,"
                    f"fs2_scientific_batch_events,fs2_scientific_admission_outbox,"
                    f"fs2_reporting_model_usage,fs2_reporting_principal_usage,"
                    f"fs2_reporting_terminal_totals,fs2_activation_intents,fs2_activation_events,"
                    f"fs2_activation_target_state,fs2_activation_controller_status,"
                    f"fs2_activation_model_fences,fs2_telemetry_subjects,"
                    f"fs2_telemetry_correlations,fs2_lifecycle_signals,fs2_lifecycle_rollups,"
                    f"fs2_reporting_lifecycle_latest,fs2_reporting_gpu_phase_usage,"
                    f"fs2_reporting_lifecycle_workloads FROM {role}"
                )
                await connection.execute(
                    f"REVOKE ALL ON fs2_operation_events_id_seq,fs2_audit_events_id_seq,"
                    f"fs2_activation_events_id_seq,fs2_configuration_revisions_revision_seq,"
                    f"fs2_configuration_reconciliation_events_id_seq,"
                    f"fs2_model_deployment_status_events_id_seq,"
                    f"fs2_scientific_artifact_events_id_seq,"
                    f"fs2_scientific_retention_ledger_id_seq,"
                    f"fs2_scientific_batch_events_sequence_seq,"
                    f"fs2_lifecycle_signals_id_seq FROM {role}"
                )
                await connection.execute(
                    f"REVOKE ALL ON FUNCTION fs2_activation_model_lock_key(text),"
                    f"fs2_runtime_ensure_activation_intent(uuid,integer,text,text,char(64),"
                    f"timestamptz,integer,text,bigint),fs2_record_terminal_usage(),"
                    f"fs2_scientific_assert_writable(),fs2_scientific_assert_live_attempt(),"
                    f"fs2_scientific_validate_attempt_transition(),"
                    f"fs2_scientific_validate_upload_transition(),"
                    f"fs2_scientific_reject_mutation(),"
                    f"fs2_scientific_guard_retention_delete(),"
                    f"fs2_scientific_batch_state_immutable(),"
                    f"fs2_scientific_batch_append_only(),"
                    f"fs2_reject_telemetry_mutation() FROM {role}"
                )
            await connection.execute(
                f"GRANT SELECT ON fs2_reporting_model_usage,fs2_reporting_principal_usage,"
                f"fs2_reporting_terminal_totals,fs2_reporting_gpu_phase_usage,"
                f"fs2_reporting_lifecycle_workloads TO {quoted_reporting}"
            )
            await connection.execute(f"GRANT SELECT,INSERT,UPDATE ON fs2_tokens,fs2_operations TO {quoted_runtime}")
            await connection.execute(
                f"GRANT SELECT,INSERT,UPDATE ON fs2_operator_principals,fs2_operator_sessions TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT,INSERT ON fs2_operation_events,fs2_audit_events TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT,INSERT ON fs2_configuration_revisions,fs2_configuration_plans,"
                f"fs2_configuration_reconciliation_events TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT,INSERT ON fs2_model_deployment_revisions,"
                f"fs2_model_deployment_idempotency,fs2_model_deployment_status_events TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT,INSERT ON fs2_telemetry_subjects,fs2_telemetry_correlations,"
                f"fs2_lifecycle_signals,fs2_lifecycle_rollups TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT ON fs2_reporting_lifecycle_latest,fs2_reporting_gpu_phase_usage,"
                f"fs2_reporting_lifecycle_workloads TO {quoted_runtime}"
            )
            await connection.execute(f"GRANT SELECT,INSERT,UPDATE ON fs2_model_deployments TO {quoted_runtime}")
            # Scientific artifact provenance. Rows are append-only for the
            # runtime role: the only permitted updates are the two documented
            # one-way transitions, and DELETE is additionally gated in SQL by
            # the retention trigger, so the privilege alone cannot erase data.
            await connection.execute(
                f"GRANT SELECT,INSERT ON fs2_scientific_stage_attempts,fs2_scientific_artifacts,"
                f"fs2_scientific_uploads,fs2_scientific_stage_commits,"
                f"fs2_scientific_stage_commit_attempts,fs2_scientific_run_results,"
                f"fs2_scientific_artifact_events,fs2_scientific_retention_ledger TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT UPDATE (status,completed_at,resolved_pool_id,admitted_resource_flavor,"
                f"accelerator_resource_name,accelerator_count,admitted_at,kueue_workload_uid,"
                f"k8s_job_uid,pod_uids,node_uids,gpu_uuids) "
                f"ON fs2_scientific_stage_attempts TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT UPDATE (artifact_id,finalized_at) ON fs2_scientific_uploads TO {quoted_runtime}"
            )
            await connection.execute(f"GRANT SELECT,INSERT ON fs2_scientific_batches TO {quoted_runtime}")
            await connection.execute(
                f"GRANT UPDATE (status,revision,cancel_requested,state,controller_id,"
                f"fencing_token,lease_expires_at,updated_at) ON fs2_scientific_batches TO {quoted_runtime}"
            )
            await connection.execute(f"GRANT SELECT,INSERT ON fs2_scientific_batch_events TO {quoted_runtime}")
            await connection.execute(
                f"GRANT SELECT,INSERT,UPDATE,DELETE ON fs2_scientific_admission_outbox TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT DELETE ON fs2_scientific_stage_attempts,fs2_scientific_artifacts,"
                f"fs2_scientific_uploads,fs2_scientific_stage_commits,"
                f"fs2_scientific_stage_commit_attempts,fs2_scientific_run_results,"
                f"fs2_scientific_artifact_events TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT ON fs2_schema_migrations,fs2_reporting_terminal_totals TO {quoted_runtime}"
            )
            # API-key inventory joins runtime-owned token identities to a
            # narrow, payload-free usage projection. Column grants keep the
            # runtime role unable to inspect tenant/principal/model facts or
            # mutate the append-only accounting ledger.
            await connection.execute(
                f"GRANT SELECT (operation_id,token_id,estimated_gpu_seconds,input_tokens,output_tokens,modality_usage) "
                f"ON fs2_usage_facts TO {quoted_runtime}"
            )
            await connection.execute(f"GRANT SELECT ON fs2_activation_intents TO {quoted_runtime}")
            await connection.execute(f"GRANT SELECT ON fs2_activation_controller_status TO {quoted_runtime}")
            await connection.execute(
                f"GRANT USAGE,SELECT ON fs2_operation_events_id_seq,fs2_audit_events_id_seq TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT USAGE,SELECT ON fs2_configuration_revisions_revision_seq,"
                f"fs2_configuration_reconciliation_events_id_seq,"
                f"fs2_model_deployment_status_events_id_seq,"
                f"fs2_scientific_artifact_events_id_seq,"
                f"fs2_scientific_retention_ledger_id_seq,"
                f"fs2_scientific_batch_events_sequence_seq,"
                f"fs2_lifecycle_signals_id_seq TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT EXECUTE ON FUNCTION fs2_runtime_ensure_activation_intent(uuid,integer,text,text,"
                f"char(64),timestamptz,integer,text,bigint) TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT EXECUTE ON FUNCTION fs2_activation_model_lock_key(text) TO {quoted_runtime}"
            )
            await connection.execute(
                f"GRANT SELECT (id,revoked_at,expires_at,gpu_seconds_reserved),"
                f"UPDATE (gpu_seconds_reserved),DELETE ON fs2_tokens TO {quoted_maintenance}"
            )
            await connection.execute(
                f"GRANT SELECT (id,token_id,status,reserved_gpu_seconds,payload_expires_at,"
                f"payload_purged_at,completed_at,outcome,error_code,fencing_token),"
                f"UPDATE (request_key_id,request_nonce,request_ciphertext,response_key_id,response_nonce,"
                f"response_ciphertext,payload_purged_at,status,completed_at,outcome,error_code,error_detail,"
                f"worker_id,heartbeat_at,lease_expires_at,fencing_token,reserved_gpu_seconds),"
                f"DELETE ON fs2_operations TO {quoted_maintenance}"
            )
            await connection.execute(
                f"GRANT SELECT (id,occurred_at),DELETE ON fs2_audit_events TO {quoted_maintenance}"
            )
            await connection.execute(
                f"GRANT SELECT (operation_id,occurred_at),DELETE ON fs2_usage_facts TO {quoted_maintenance}"
            )
            await connection.execute(
                f"GRANT SELECT (id,model_id,model_revision,status,attempt,lease_expires_at,deadline_at) "
                f"ON fs2_operations TO {quoted_activation}"
            )
            await connection.execute(
                f"GRANT SELECT,INSERT,UPDATE ON fs2_activation_intents,fs2_activation_target_state,"
                f"fs2_activation_controller_status,fs2_activation_model_fences TO {quoted_activation}"
            )
            await connection.execute(f"GRANT INSERT ON fs2_activation_events TO {quoted_activation}")
            await connection.execute(f"GRANT USAGE ON fs2_activation_events_id_seq TO {quoted_activation}")
            await connection.execute(
                f"GRANT EXECUTE ON FUNCTION fs2_activation_model_lock_key(text) TO {quoted_activation}"
            )

    async def migrate(self) -> None:
        await self._apply_migrations(self.pool, self.migrations_dir)

    @classmethod
    async def migrate_database(
        cls,
        database_url: str,
        migrations_dir: Path,
        reporting_role: str = "fs2_serve_reporting",
        runtime_role: str = "fs2_serve_runtime",
        maintenance_role: str = "fs2_serve_maintenance",
        activation_role: str = "fs2_serve_activation",
    ) -> None:
        """Apply serialized DDL without loading any runtime cryptographic material."""

        pool = await cls._connect_pool(
            database_url,
            min_size=1,
            max_size=1,
            application_name="fs2-serve-migration",
        )
        try:
            await cls._apply_migrations(
                pool,
                migrations_dir,
                reporting_role,
                runtime_role,
                maintenance_role,
                activation_role,
            )
        finally:
            await pool.close()

    @classmethod
    async def wait_for_schema(cls, database_url: str, migrations_dir: Path, timeout_seconds: float) -> None:
        """Wait for the exact packaged migration set using only the runtime DML credential."""

        expected = [(path.name, digest) for path, digest in cls._migration_manifest(migrations_dir)]
        pool = await cls._connect_pool(
            database_url,
            min_size=1,
            max_size=1,
            application_name="fs2-serve-schema-wait",
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            while True:
                async with pool.acquire() as connection:
                    table_exists = await connection.fetchval("SELECT to_regclass('public.fs2_schema_migrations')")
                    rows = (
                        await connection.fetch(
                            "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY applied_at,version"
                        )
                        if table_exists is not None
                        else []
                    )
                applied = [(str(row["version"]), str(row["sha256"])) for row in rows]
                if [version for version, _ in applied] != [version for version, _ in expected[: len(applied)]]:
                    raise RuntimeError("applied migration set is missing, extra, or reordered")
                changed = [
                    version
                    for (version, digest), (_, expected_digest) in zip(applied, expected, strict=False)
                    if digest != expected_digest
                ]
                if changed:
                    raise RuntimeError("an applied migration does not match this immutable image")
                if applied == expected:
                    async with pool.acquire() as connection:
                        runtime_privileges_ready = await connection.fetchval(
                            "SELECT "
                            "has_table_privilege('fs2_serve_runtime',"
                            "'public.fs2_scientific_admission_outbox','SELECT') AND "
                            "has_table_privilege('fs2_serve_runtime',"
                            "'public.fs2_scientific_admission_outbox','INSERT') AND "
                            "has_table_privilege('fs2_serve_runtime',"
                            "'public.fs2_scientific_admission_outbox','UPDATE') AND "
                            "has_table_privilege('fs2_serve_runtime',"
                            "'public.fs2_scientific_admission_outbox','DELETE') AND "
                            "has_table_privilege(current_user,"
                            "'public.fs2_scientific_admission_outbox','SELECT') AND "
                            "has_table_privilege(current_user,"
                            "'public.fs2_scientific_admission_outbox','INSERT') AND "
                            "has_table_privilege(current_user,"
                            "'public.fs2_scientific_admission_outbox','UPDATE') AND "
                            "has_table_privilege(current_user,"
                            "'public.fs2_scientific_admission_outbox','DELETE')"
                        )
                    if not runtime_privileges_ready:
                        raise RuntimeError("database schema runtime privileges are incomplete")
                    return
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise RuntimeError("database schema did not become ready before the bounded deadline")
                await asyncio.sleep(min(1.0, remaining))
        finally:
            await pool.close()

    @staticmethod
    def _token(row: asyncpg.Record) -> TokenView:
        return TokenView(
            id=row["id"],
            prefix=row["prefix"],
            pepper_key_id=row["pepper_key_id"],
            principal_id=row["principal_id"],
            tenant_id=row["tenant_id"],
            scopes=list(row["scopes"]),
            models=list(row["models"]),
            expires_at=row["expires_at"],
            request_budget=row["request_budget"],
            requests_used=row["requests_used"],
            gpu_seconds_budget=row["gpu_seconds_budget"],
            gpu_seconds_used=row["gpu_seconds_used"],
            gpu_seconds_reserved=row["gpu_seconds_reserved"],
            max_concurrency=row["max_concurrency"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            revoked_at=row["revoked_at"],
            name=row["name"],
            fingerprint=row["fingerprint"],
            last_used_at=row["last_used_at"],
            rotation_parent_id=row["rotation_parent_id"],
            rotated_at=row["rotated_at"],
            rate_limit_requests=row["rate_limit_requests"],
            rate_window_seconds=row["rate_window_seconds"],
            rate_window_started_at=row["rate_window_started_at"],
            rate_window_requests=row["rate_window_requests"],
        )

    @staticmethod
    def _operator_principal(row: asyncpg.Record) -> OperatorPrincipal:
        keys = set(row.keys())
        return OperatorPrincipal(
            id=row["principal_id"] if "principal_id" in keys else row["id"],
            subject=row["subject"],
            display_name=row["display_name"],
            kind=row["kind"],
            role=row["role"],
            tenant_id=row["tenant_id"],
            enabled=row["enabled"],
            created_at=row["principal_created_at"] if "principal_created_at" in keys else row["created_at"],
            created_by=row["principal_created_by"] if "principal_created_by" in keys else row["created_by"],
            updated_at=row["updated_at"],
            disabled_at=row["disabled_at"],
        )

    @classmethod
    def _operator_session(cls, row: asyncpg.Record) -> OperatorSessionRecord:
        principal = cls._operator_principal(row)
        return OperatorSessionRecord(
            session=OperatorSession(
                id=row["session_id"],
                principal=principal,
                created_at=row["session_created_at"],
                expires_at=row["expires_at"],
                last_seen_at=row["last_seen_at"],
                revoked_at=row["revoked_at"],
            ),
            pepper_key_id=row["pepper_key_id"],
            digest=row["digest"],
        )

    @staticmethod
    def _configuration_revision(row: asyncpg.Record) -> ConfigurationRevision:
        return ConfigurationRevision(
            revision=row["revision"],
            etag=row["etag"],
            desired=PlatformConfiguration.model_validate(
                _decode_configuration_json(row["desired"], "configuration desired state")
            ),
            effective=PlatformConfiguration.model_validate(
                _decode_configuration_json(row["effective"], "configuration effective state")
            ),
            created_at=row["created_at"],
            created_by=row["created_by"],
            previous_revision=row["previous_revision"],
            reconciliation_id=row["reconciliation_id"],
        )

    @staticmethod
    def _model_deployment_revision(row: asyncpg.Record) -> ModelDeploymentRevision:
        return ModelDeploymentRevision(
            namespace=row["namespace"],
            name=row["name"],
            tenant_id=row["tenant_id"],
            revision=row["revision"],
            etag=row["etag"],
            spec=ModelDeploymentSpec.model_validate(
                _decode_configuration_json(row["spec"], "model deployment desired state")
            ),
            action=row["action"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            previous_revision=row["previous_revision"],
        )

    @staticmethod
    def _model_deployment_status(row: asyncpg.Record) -> ModelDeploymentStatusObservation:
        raw_status = _decode_configuration_json(row["status"], "model deployment status")
        return ModelDeploymentStatusObservation(
            observation_id=row["observation_id"],
            source_uid=row["source_uid"],
            source_resource_version=row["source_resource_version"],
            namespace=row["namespace"],
            name=row["name"],
            tenant_id=row["tenant_id"],
            revision=row["revision"],
            status=ModelDeploymentObservedStatus.model_validate(
                _upgrade_legacy_model_deployment_status(raw_status)
            ),
            observed_at=row["observed_at"],
        )

    @staticmethod
    def _operation(row: asyncpg.Record, *, reused: bool = False) -> OperationView:
        expires = row["payload_expires_at"]
        result_available = row["response_ciphertext"] is not None and expires > datetime.now(UTC)
        return OperationView(
            id=row["id"],
            tenant_id=row["tenant_id"],
            principal_id=row["principal_id"],
            token_id=row["token_id"],
            model_id=row["model_id"],
            model_revision=row["model_revision"],
            protocol=row["protocol"],
            operation=row["operation"],
            idempotency_key=row["idempotency_key"],
            status=OperationStatus(row["status"]),
            accepted_at=row["accepted_at"],
            available_at=row["available_at"],
            deadline_at=row["deadline_at"],
            activation_started_at=row["activation_started_at"],
            ready_at=row["ready_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            outcome=row["outcome"],
            semantic_outcome=row["semantic_outcome"],
            http_status=row["http_status"],
            response_content_type=row["response_content_type"],
            result_available=result_available,
            payload_expires_at=expires,
            error_code=row["error_code"],
            error_detail=row["error_detail"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            fencing_token=row["fencing_token"],
            runtime=RuntimeIdentity(
                pod_uid=row["pod_uid"],
                node_uid=row["node_uid"],
                gpu_uuids=list(row["gpu_uuids"]),
                gpu_count=row["gpu_count"],
                preemptible=row["preemptible"],
            ),
            estimated_gpu_seconds=row["estimated_gpu_seconds"],
            reserved_gpu_seconds=row["reserved_gpu_seconds"],
            cold_start_seconds=row["cold_start_seconds"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            modality_usage=_decode_modality_usage(row["modality_usage"]),
            modality_usage_reported=row["modality_usage"] is not None,
            reused=reused,
        )

    @staticmethod
    async def _event(
        connection: asyncpg.Connection[Any],
        operation_id: UUID,
        event: str,
        status: OperationStatus,
        attempt: int,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            "INSERT INTO fs2_operation_events(operation_id,event,status,attempt,detail) VALUES($1,$2,$3,$4,$5)",
            operation_id,
            event,
            str(status),
            attempt,
            json.dumps(detail or {}),
        )

    @staticmethod
    async def _audit(
        connection: asyncpg.Connection[Any],
        *,
        actor: str,
        tenant_id: str | None,
        token_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO fs2_audit_events
                (actor,tenant_id,token_id,action,target_type,target_id,outcome,detail)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            actor,
            tenant_id,
            token_id,
            action,
            target_type,
            target_id,
            outcome,
            json.dumps(detail or {}),
        )

    @retry_serialization
    async def issue_token(
        self,
        *,
        token_id: UUID,
        prefix: str,
        pepper_key_id: str,
        digest: str,
        request: TokenCreate,
        created_by: str,
        fingerprint: str | None = None,
    ) -> TokenView:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, token_id)
            try:
                row = await connection.fetchrow(
                    """
                    INSERT INTO fs2_tokens
                        (id,prefix,pepper_key_id,digest,principal_id,tenant_id,scopes,models,expires_at,
                         request_budget,gpu_seconds_budget,max_concurrency,created_by,name,fingerprint,
                         rate_limit_requests,rate_window_seconds)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) RETURNING *
                    """,
                    token_id,
                    prefix,
                    pepper_key_id,
                    digest,
                    request.principal_id,
                    request.tenant_id,
                    sorted(str(scope) for scope in request.scopes),
                    sorted(request.models),
                    request.expires_at,
                    request.request_budget,
                    request.gpu_seconds_budget,
                    request.max_concurrency,
                    created_by,
                    request.name,
                    fingerprint,
                    request.rate_limit_requests,
                    request.rate_window_seconds,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("token already exists") from exc
            assert row is not None
            await self._audit(
                connection,
                actor=created_by,
                tenant_id=request.tenant_id,
                token_id=token_id,
                action="token.issue",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={
                    "prefix": prefix,
                    "scopes": sorted(str(scope) for scope in request.scopes),
                    "models": sorted(request.models),
                },
            )
            return self._token(row)

    async def token_for_verification(self, token_id: UUID) -> tuple[TokenView, str] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1", token_id)
            return (self._token(row), cast(str, row["digest"])) if row is not None else None

    async def get_token(self, token_id: UUID) -> TokenView:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1", token_id)
        if row is None:
            raise NotFoundError("token not found")
        return self._token(row)

    @retry_serialization
    async def rehash_token(self, token_id: UUID, *, pepper_key_id: str, digest: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, token_id)
            await connection.execute(
                """
                UPDATE fs2_tokens SET pepper_key_id=$2,digest=$3
                WHERE id=$1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp())
                """,
                token_id,
                pepper_key_id,
                digest,
            )

    async def list_tokens(self, *, tenant_id: str | None = None, limit: int = 200) -> list[TokenView]:
        if not 1 <= limit <= 1000:
            raise ValueError("token list limit is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM fs2_tokens WHERE ($1::text IS NULL OR tenant_id=$1)
                ORDER BY created_at DESC,id DESC LIMIT $2
                """,
                tenant_id,
                limit,
            )
            return [self._token(row) for row in rows]

    @retry_serialization
    async def record_token_expired(self, token_id: UUID, *, actor: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, token_id)
            row = await connection.fetchrow(
                """
                UPDATE fs2_tokens SET expiration_recorded_at=clock_timestamp()
                WHERE id=$1 AND expires_at IS NOT NULL AND expires_at<=clock_timestamp()
                  AND expiration_recorded_at IS NULL
                RETURNING tenant_id
                """,
                token_id,
            )
            if row is None:
                return
            await self._audit(
                connection,
                actor=actor,
                tenant_id=row["tenant_id"],
                token_id=token_id,
                action="token.expire",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
            )

    @retry_serialization
    async def rotate_token(
        self,
        predecessor_id: UUID,
        *,
        token_id: UUID,
        prefix: str,
        pepper_key_id: str,
        digest: str,
        fingerprint: str,
        name: str | None,
        expires_at: datetime | None,
        actor: str,
    ) -> TokenView:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, predecessor_id)
            await self._token_lock(connection, token_id)
            predecessor = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1 FOR UPDATE", predecessor_id)
            now = await connection.fetchval("SELECT clock_timestamp()")
            if predecessor is None:
                raise NotFoundError("token not found")
            if predecessor["revoked_at"] is not None or (
                predecessor["expires_at"] is not None and predecessor["expires_at"] <= now
            ):
                raise ConflictError("token is already inactive")
            if expires_at is not None and expires_at <= now:
                raise ValueError("expires_at must be in the future")
            released = await connection.fetchval(
                """
                SELECT COALESCE(sum(reserved_gpu_seconds),0) FROM fs2_operations
                WHERE token_id=$1 AND status IN ('queued','activating','running')
                """,
                predecessor_id,
            )
            try:
                successor = await connection.fetchrow(
                    """
                    INSERT INTO fs2_tokens(
                        id,prefix,pepper_key_id,digest,principal_id,tenant_id,scopes,models,expires_at,
                        request_budget,requests_used,gpu_seconds_budget,gpu_seconds_used,gpu_seconds_reserved,
                        max_concurrency,created_by,name,fingerprint,rotation_parent_id,rate_limit_requests,
                        rate_window_seconds,rate_window_started_at,rate_window_requests
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,COALESCE($9::timestamptz,$10),$11,$12,$13,$14,0,$15,$16,
                        COALESCE($17,$18),$19,$20,$21,$22,$23,$24
                    ) RETURNING *
                    """,
                    token_id,
                    prefix,
                    pepper_key_id,
                    digest,
                    predecessor["principal_id"],
                    predecessor["tenant_id"],
                    predecessor["scopes"],
                    predecessor["models"],
                    expires_at,
                    predecessor["expires_at"],
                    predecessor["request_budget"],
                    predecessor["requests_used"],
                    predecessor["gpu_seconds_budget"],
                    predecessor["gpu_seconds_used"],
                    predecessor["max_concurrency"],
                    actor,
                    name,
                    predecessor["name"],
                    fingerprint,
                    predecessor_id,
                    predecessor["rate_limit_requests"],
                    predecessor["rate_window_seconds"],
                    predecessor["rate_window_started_at"],
                    predecessor["rate_window_requests"],
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("token already exists") from exc
            assert successor is not None
            await connection.execute(
                """
                UPDATE fs2_tokens SET revoked_at=$2,rotated_at=$2,
                    gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$3) WHERE id=$1
                """,
                predecessor_id,
                now,
                released,
            )
            await connection.execute(
                """
                UPDATE fs2_operations SET status='cancelled',completed_at=$2,
                    outcome='token_rotated',error_code='token_rotated',error_detail=NULL,
                    worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    fencing_token=fencing_token+1,reserved_gpu_seconds=0
                WHERE token_id=$1 AND status IN ('queued','activating','running')
                """,
                predecessor_id,
                now,
            )
            await self._audit(
                connection,
                actor=actor,
                tenant_id=successor["tenant_id"],
                token_id=token_id,
                action="token.rotate",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={"predecessor_id": str(predecessor_id), "prefix": prefix},
            )
            return self._token(successor)

    @retry_serialization
    async def revoke_token(self, token_id: UUID, *, actor: str) -> TokenView:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, token_id)
            row = await connection.fetchrow(
                "UPDATE fs2_tokens SET revoked_at=COALESCE(revoked_at,clock_timestamp()) WHERE id=$1 RETURNING *",
                token_id,
            )
            if row is None:
                raise NotFoundError("token not found")
            released = await connection.fetchval(
                """
                SELECT COALESCE(sum(reserved_gpu_seconds),0) FROM fs2_operations
                WHERE token_id=$1 AND status IN ('queued','activating','running')
                """,
                token_id,
            )
            await connection.execute(
                """
                UPDATE fs2_operations SET status='cancelled',completed_at=clock_timestamp(),
                    outcome='token_revoked',error_code='token_revoked',error_detail=NULL,
                    worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                    fencing_token=fencing_token+1,reserved_gpu_seconds=0
                WHERE token_id=$1 AND status IN ('queued','activating','running')
                """,
                token_id,
            )
            row = await connection.fetchrow(
                """
                UPDATE fs2_tokens SET gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2)
                WHERE id=$1 RETURNING *
                """,
                token_id,
                released,
            )
            assert row is not None
            await self._audit(
                connection,
                actor=actor,
                tenant_id=row["tenant_id"],
                token_id=token_id,
                action="token.revoke",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
            )
            return self._token(row)

    @retry_serialization
    async def update_token_policy(
        self,
        token_id: UUID,
        *,
        request: AdminApiKeyPolicyPatch,
        actor: str,
    ) -> TokenView:
        fields = request.model_fields_set
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, token_id)
            current = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1 FOR UPDATE", token_id)
            if current is None:
                raise NotFoundError("token not found")
            now = await connection.fetchval("SELECT clock_timestamp()")
            if current["revoked_at"] is not None or (
                current["expires_at"] is not None and current["expires_at"] <= now
            ):
                raise ConflictError("token is already inactive")
            if "expires_at" in fields and request.expires_at is not None and request.expires_at <= now:
                raise ValueError("expires_at must be in the future")
            if (
                "request_budget" in fields
                and request.request_budget is not None
                and request.request_budget < current["requests_used"]
            ):
                raise ConflictError("request budget is below durable usage")
            if (
                "gpu_seconds_budget" in fields
                and request.gpu_seconds_budget is not None
                and request.gpu_seconds_budget < current["gpu_seconds_used"] + current["gpu_seconds_reserved"]
            ):
                raise ConflictError("GPU budget is below durable usage and reservations")
            row = await connection.fetchrow(
                """
                UPDATE fs2_tokens SET
                    name=CASE WHEN $2::boolean THEN $3::text ELSE name END,
                    scopes=CASE WHEN $4::boolean THEN $5::text[] ELSE scopes END,
                    models=CASE WHEN $6::boolean THEN $7::text[] ELSE models END,
                    expires_at=CASE WHEN $8::boolean THEN $9::timestamptz ELSE expires_at END,
                    request_budget=CASE WHEN $10::boolean THEN $11::bigint ELSE request_budget END,
                    gpu_seconds_budget=CASE WHEN $12::boolean THEN $13::double precision ELSE gpu_seconds_budget END,
                    max_concurrency=CASE WHEN $14::boolean THEN $15::integer ELSE max_concurrency END,
                    rate_limit_requests=CASE WHEN $16::boolean THEN $17::integer ELSE rate_limit_requests END,
                    rate_window_seconds=CASE WHEN $16::boolean THEN $18::integer ELSE rate_window_seconds END,
                    rate_window_started_at=CASE WHEN $16::boolean THEN NULL ELSE rate_window_started_at END,
                    rate_window_requests=CASE WHEN $16::boolean THEN 0 ELSE rate_window_requests END
                WHERE id=$1 RETURNING *
                """,
                token_id,
                "name" in fields,
                request.name,
                "scopes" in fields,
                sorted(str(scope) for scope in request.scopes) if request.scopes is not None else None,
                "models" in fields,
                sorted(request.models) if request.models is not None else None,
                "expires_at" in fields,
                request.expires_at,
                "request_budget" in fields,
                request.request_budget,
                "gpu_seconds_budget" in fields,
                request.gpu_seconds_budget,
                "max_concurrency" in fields,
                request.max_concurrency,
                "rate_limit_requests" in fields,
                request.rate_limit_requests,
                request.rate_window_seconds,
            )
            assert row is not None
            await self._audit(
                connection,
                actor=actor,
                tenant_id=row["tenant_id"],
                token_id=token_id,
                action="token.policy.update",
                target_type="token",
                target_id=str(token_id),
                outcome="succeeded",
                detail={f"{name}_changed": True for name in sorted(fields)},
            )
            return self._token(row)

    async def list_operator_principals(
        self, *, tenant_id: str | None, include_global: bool, limit: int
    ) -> list[OperatorPrincipal]:
        if not 1 <= limit <= 1000:
            raise ValueError("principal list limit is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM fs2_operator_principals
                WHERE ($1::text IS NULL AND $2::boolean)
                   OR tenant_id IS NOT DISTINCT FROM $1
                   OR ($2::boolean AND tenant_id IS NULL)
                ORDER BY created_at DESC,id DESC LIMIT $3
                """,
                tenant_id,
                include_global,
                limit,
            )
        return [self._operator_principal(row) for row in rows]

    async def get_operator_principal(self, principal_id: UUID) -> OperatorPrincipal:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_operator_principals WHERE id=$1", principal_id)
        if row is None:
            raise NotFoundError("operator principal not found")
        return self._operator_principal(row)

    @retry_serialization
    async def create_operator_principal(
        self, *, principal_id: UUID, request: OperatorPrincipalCreate, actor: str
    ) -> OperatorPrincipal:
        async with self.pool.acquire() as connection, connection.transaction():
            try:
                row = await connection.fetchrow(
                    """
                    INSERT INTO fs2_operator_principals(
                        id,subject,display_name,kind,role,tenant_id,enabled,created_by
                    ) VALUES($1,$2,$3,$4,$5,$6,true,$7) RETURNING *
                    """,
                    principal_id,
                    request.subject,
                    request.display_name,
                    str(request.kind),
                    str(request.role),
                    request.tenant_id,
                    actor,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("operator principal already exists") from exc
            assert row is not None
            await self._audit(
                connection,
                actor=actor,
                tenant_id=request.tenant_id,
                token_id=None,
                action="principal.create",
                target_type="operator_principal",
                target_id=str(principal_id),
                outcome="succeeded",
                detail={"kind": str(request.kind), "role": str(request.role)},
            )
            return self._operator_principal(row)

    @retry_serialization
    async def update_operator_principal(
        self, principal_id: UUID, *, request: OperatorPrincipalPatch, actor: str
    ) -> OperatorPrincipal:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE fs2_operator_principals
                SET display_name=COALESCE($2,display_name),role=COALESCE($3,role),
                    enabled=COALESCE($4,enabled),
                    disabled_at=CASE
                        WHEN $4::boolean IS TRUE THEN NULL
                        WHEN $4::boolean IS FALSE THEN clock_timestamp()
                        ELSE disabled_at
                    END,
                    updated_at=clock_timestamp()
                WHERE id=$1 RETURNING *
                """,
                principal_id,
                request.display_name,
                str(request.role) if request.role is not None else None,
                request.enabled,
            )
            if row is None:
                raise NotFoundError("operator principal not found")
            if not row["enabled"]:
                await connection.execute(
                    """
                    UPDATE fs2_operator_sessions SET revoked_at=COALESCE(revoked_at,clock_timestamp())
                    WHERE principal_id=$1 AND revoked_at IS NULL
                    """,
                    principal_id,
                )
            detail: dict[str, str | bool] = {}
            if request.display_name is not None:
                detail["display_name_changed"] = True
            if request.role is not None:
                detail["role"] = str(request.role)
            if request.enabled is not None:
                detail["enabled"] = request.enabled
            await self._audit(
                connection,
                actor=actor,
                tenant_id=row["tenant_id"],
                token_id=None,
                action="principal.update",
                target_type="operator_principal",
                target_id=str(principal_id),
                outcome="succeeded",
                detail=detail,
            )
            return self._operator_principal(row)

    @retry_serialization
    async def create_operator_session(
        self,
        *,
        session_id: UUID,
        principal_id: UUID,
        pepper_key_id: str,
        digest: str,
        expires_at: datetime,
        actor: str,
    ) -> OperatorSession:
        async with self.pool.acquire() as connection, connection.transaction():
            principal = await connection.fetchrow(
                "SELECT * FROM fs2_operator_principals WHERE id=$1 AND enabled FOR UPDATE", principal_id
            )
            if principal is None:
                raise NotFoundError("operator principal not found")
            try:
                row = await connection.fetchrow(
                    """
                    INSERT INTO fs2_operator_sessions(
                        id,principal_id,pepper_key_id,digest,expires_at,created_by
                    ) VALUES($1,$2,$3,$4,$5,$6)
                    RETURNING id AS session_id,created_at AS session_created_at,expires_at,last_seen_at,revoked_at
                    """,
                    session_id,
                    principal_id,
                    pepper_key_id,
                    digest,
                    expires_at,
                    actor,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("operator session already exists") from exc
            assert row is not None
            session = OperatorSession(
                id=row["session_id"],
                principal=self._operator_principal(principal),
                created_at=row["session_created_at"],
                expires_at=row["expires_at"],
                last_seen_at=row["last_seen_at"],
                revoked_at=row["revoked_at"],
            )
            await self._audit(
                connection,
                actor=actor,
                tenant_id=principal["tenant_id"],
                token_id=None,
                action="session.issue",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
                detail={"principal_id": str(principal_id)},
            )
            return session

    @retry_serialization
    async def replace_operator_session(
        self,
        *,
        prior_session_id: UUID | None,
        prior_digest: str | None,
        session_id: UUID,
        principal_id: UUID,
        pepper_key_id: str,
        digest: str,
        expires_at: datetime,
        actor: str,
    ) -> OperatorSession:
        if (prior_session_id is None) != (prior_digest is None):
            raise ValueError("prior operator session verifier is incomplete")
        async with self.pool.acquire() as connection, connection.transaction():
            principal = await connection.fetchrow(
                "SELECT * FROM fs2_operator_principals WHERE id=$1 AND enabled FOR UPDATE", principal_id
            )
            if principal is None:
                raise NotFoundError("operator principal not found")
            prior = None
            if prior_session_id is not None and prior_digest is not None:
                prior = await connection.fetchrow(
                    "SELECT * FROM fs2_operator_sessions WHERE id=$1 AND digest=$2 FOR UPDATE",
                    prior_session_id,
                    prior_digest,
                )
            try:
                row = await connection.fetchrow(
                    """
                    INSERT INTO fs2_operator_sessions(
                        id,principal_id,pepper_key_id,digest,expires_at,created_by
                    ) VALUES($1,$2,$3,$4,$5,$6)
                    RETURNING id AS session_id,created_at AS session_created_at,expires_at,last_seen_at,revoked_at
                    """,
                    session_id,
                    principal_id,
                    pepper_key_id,
                    digest,
                    expires_at,
                    actor,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("operator session already exists") from exc
            assert row is not None
            if prior is not None and prior["revoked_at"] is None:
                await connection.execute(
                    "UPDATE fs2_operator_sessions SET revoked_at=clock_timestamp() WHERE id=$1",
                    prior_session_id,
                )
                await self._audit(
                    connection,
                    actor=actor,
                    tenant_id=(
                        await connection.fetchval(
                            "SELECT tenant_id FROM fs2_operator_principals WHERE id=$1",
                            prior["principal_id"],
                        )
                    ),
                    token_id=None,
                    action="session.revoke",
                    target_type="operator_session",
                    target_id=str(prior_session_id),
                    outcome="succeeded",
                    detail={"reason": "replacement"},
                )
            session = OperatorSession(
                id=row["session_id"],
                principal=self._operator_principal(principal),
                created_at=row["session_created_at"],
                expires_at=row["expires_at"],
                last_seen_at=row["last_seen_at"],
                revoked_at=row["revoked_at"],
            )
            await self._audit(
                connection,
                actor=actor,
                tenant_id=principal["tenant_id"],
                token_id=None,
                action="session.issue",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
                detail={"principal_id": str(principal_id), "replacement": prior is not None},
            )
            return session

    async def operator_session_for_verification(self, session_id: UUID) -> OperatorSessionRecord | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT session.id AS session_id,session.pepper_key_id,session.digest,
                       session.created_at AS session_created_at,session.expires_at,
                       session.last_seen_at,session.revoked_at,
                       principal.id AS principal_id,principal.subject,principal.display_name,
                       principal.kind,principal.role,principal.tenant_id,principal.enabled,
                       principal.created_at AS principal_created_at,
                       principal.created_by AS principal_created_by,principal.updated_at,principal.disabled_at
                FROM fs2_operator_sessions session
                JOIN fs2_operator_principals principal ON principal.id=session.principal_id
                WHERE session.id=$1
                """,
                session_id,
            )
        return self._operator_session(row) if row is not None else None

    async def touch_operator_session(self, session_id: UUID, *, seen_at: datetime) -> None:
        if seen_at.tzinfo is None:
            raise ValueError("session use timestamp must be timezone-aware")
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE fs2_operator_sessions SET last_seen_at=GREATEST(last_seen_at,$2)
                WHERE id=$1 AND revoked_at IS NULL AND expires_at>$2
                """,
                session_id,
                seen_at,
            )

    @retry_serialization
    async def revoke_operator_session(self, session_id: UUID, *, actor: str) -> OperatorSession:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE fs2_operator_sessions session
                SET revoked_at=COALESCE(session.revoked_at,clock_timestamp())
                FROM fs2_operator_principals principal
                WHERE session.id=$1 AND principal.id=session.principal_id
                RETURNING session.id AS session_id,session.pepper_key_id,session.digest,
                          session.created_at AS session_created_at,session.expires_at,
                          session.last_seen_at,session.revoked_at,
                          principal.id AS principal_id,principal.subject,principal.display_name,
                          principal.kind,principal.role,principal.tenant_id,principal.enabled,
                          principal.created_at AS principal_created_at,
                          principal.created_by AS principal_created_by,principal.updated_at,principal.disabled_at
                """,
                session_id,
            )
            if row is None:
                raise NotFoundError("operator session not found")
            record = self._operator_session(row)
            await self._audit(
                connection,
                actor=actor,
                tenant_id=record.session.principal.tenant_id,
                token_id=None,
                action="session.revoke",
                target_type="operator_session",
                target_id=str(session_id),
                outcome="succeeded",
            )
            return record.session

    async def append_audit_event(
        self,
        *,
        actor: str,
        tenant_id: str | None,
        token_id: UUID | None,
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        detail: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        if not all(1 <= len(value) <= 200 for value in (actor, action, target_type, target_id, outcome)):
            raise ValueError("audit identity is outside the bound")
        encoded = json.dumps(detail or {}, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 4096:
            raise ValueError("audit detail is outside the bound")
        async with self.pool.acquire() as connection, connection.transaction():
            await self._audit(
                connection,
                actor=actor,
                tenant_id=tenant_id,
                token_id=token_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome=outcome,
                detail=dict(detail or {}),
            )

    async def configuration_current(self) -> ConfigurationRevision | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow("SELECT * FROM fs2_configuration_revisions ORDER BY revision DESC LIMIT 1")
        return self._configuration_revision(row) if row is not None else None

    async def configuration_get_revision(self, revision: int) -> ConfigurationRevision | None:
        if revision < 1:
            return None
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fs2_configuration_revisions WHERE revision=$1",
                revision,
            )
        return self._configuration_revision(row) if row is not None else None

    @retry_serialization
    async def configuration_ensure_initial(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        if actor != TERRAFORM_BOOTSTRAP_ACTOR:
            raise ValueError("initial configuration requires the Terraform-rendered baseline actor")
        desired = configuration.model_dump(mode="json")
        etag = configuration_etag(configuration)
        async with self.pool.acquire() as connection, connection.transaction():
            await self._configuration_lock(connection)
            row = await connection.fetchrow("SELECT * FROM fs2_configuration_revisions ORDER BY revision DESC LIMIT 1")
            if row is not None:
                current = self._configuration_revision(row)
                if current.etag == etag:
                    return current
                raise ConflictError("changed configuration requires a correlated Terraform apply receipt")
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_configuration_revisions(etag,desired,effective,created_by)
                VALUES($1,$2,$2,$3) RETURNING *
                """,
                etag,
                json.dumps(desired, sort_keys=True, separators=(",", ":")),
                actor,
            )
            assert row is not None
            await self._audit(
                connection,
                actor=actor,
                tenant_id=None,
                token_id=None,
                action="configuration.bootstrap",
                target_type="platform_configuration",
                target_id=etag,
                outcome="succeeded",
                detail={"revision": row["revision"]},
            )
            return self._configuration_revision(row)

    @retry_serialization
    async def configuration_adopt_terraform_baseline(
        self,
        configuration: PlatformConfiguration,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        """Append the mounted Terraform baseline when its desired state changed."""

        if not 1 <= len(actor) <= 200:
            raise ValueError("configuration actor is outside the bound")
        desired = configuration.model_dump(mode="json")
        etag = configuration_etag(configuration)
        async with self.pool.acquire() as connection, connection.transaction():
            await self._configuration_lock(connection)
            current_row = await connection.fetchrow(
                "SELECT * FROM fs2_configuration_revisions ORDER BY revision DESC LIMIT 1"
            )
            current = self._configuration_revision(current_row) if current_row is not None else None
            if current is not None and current.etag == etag:
                return current
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_configuration_revisions(
                    etag,desired,effective,created_by,previous_revision
                ) VALUES($1,$2,$2,$3,$4) RETURNING *
                """,
                etag,
                json.dumps(desired, sort_keys=True, separators=(",", ":")),
                actor,
                current.revision if current is not None else None,
            )
            assert row is not None
            return self._configuration_revision(row)

    @retry_serialization
    async def configuration_accept_terraform_applied(
        self,
        configuration: PlatformConfiguration,
        receipt: TerraformApplyReceipt,
        *,
        actor: str,
    ) -> ConfigurationRevision:
        if not 1 <= len(actor) <= 200:
            raise ValueError("configuration actor is outside the bound")
        async with self.pool.acquire() as connection, connection.transaction():
            await self._configuration_lock(connection)
            current_row = await connection.fetchrow(
                "SELECT * FROM fs2_configuration_revisions ORDER BY revision DESC LIMIT 1"
            )
            if current_row is None:
                raise ConflictError("configuration is not initialized")
            current = self._configuration_revision(current_row)
            plan_payload = await connection.fetchval(
                "SELECT payload FROM fs2_configuration_plans WHERE id=$1",
                receipt.plan_id,
            )
            status_payload = await connection.fetchval(
                """
                SELECT payload FROM fs2_configuration_reconciliation_events
                WHERE reconciliation_id=$1 ORDER BY id DESC LIMIT 1
                """,
                receipt.reconciliation_id,
            )
            if plan_payload is None or status_payload is None:
                raise ConflictError("Terraform apply receipt has no durable plan and awaiting event")
            plan = ConfigurationPlan.model_validate(_decode_configuration_json(plan_payload, "configuration plan"))
            status = ReconciliationStatus.model_validate(
                _decode_configuration_json(status_payload, "configuration status")
            )
            try:
                action = validate_terraform_apply_correlation(
                    current=current,
                    plan=plan,
                    status=status,
                    configuration=configuration,
                    receipt=receipt,
                )
            except ValueError as exc:
                raise ConflictError(str(exc)) from None
            if action == "replay":
                return current

            row = await connection.fetchrow(
                """
                INSERT INTO fs2_configuration_revisions(
                    etag,desired,effective,created_by,previous_revision,reconciliation_id
                ) VALUES($1,$2,$2,$3,$4,$5) RETURNING *
                """,
                configuration_etag(configuration),
                json.dumps(configuration.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                actor,
                current.revision,
                receipt.reconciliation_id,
            )
            assert row is not None
            revision = self._configuration_revision(row)
            completed_at = datetime.now(UTC)
            succeeded = status.model_copy(
                update={
                    "phase": ReconciliationPhase.SUCCEEDED,
                    "applied_revision": revision.revision,
                    "completed_at": completed_at,
                }
            )
            await connection.execute(
                """
                INSERT INTO fs2_configuration_reconciliation_events(
                    reconciliation_id,plan_id,phase,payload,occurred_at
                ) VALUES($1,$2,$3,$4,$5)
                """,
                succeeded.reconciliation_id,
                succeeded.plan_id,
                succeeded.phase.value,
                json.dumps(succeeded.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                completed_at,
            )
            await self._audit(
                connection,
                actor=actor,
                tenant_id=None,
                token_id=None,
                action="configuration.terraform-applied",
                target_type="platform_configuration",
                target_id=revision.etag,
                outcome="succeeded",
                detail={
                    "revision": revision.revision,
                    "previous_revision": current.revision,
                    "plan_id": str(receipt.plan_id),
                    "reconciliation_id": str(receipt.reconciliation_id),
                },
            )
            return revision

    async def configuration_save_plan(self, plan: ConfigurationPlan) -> None:
        payload = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        async with self.pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                """
                INSERT INTO fs2_configuration_plans(
                    id,base_revision,base_etag,proposed_etag,state,payload,
                    created_at,expires_at,created_by
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (id) DO NOTHING RETURNING id
                """,
                plan.plan_id,
                plan.base_revision,
                plan.base_etag,
                plan.proposed_etag,
                plan.state.value,
                payload,
                plan.created_at,
                plan.expires_at,
                plan.created_by,
            )
            if inserted is None:
                existing = await connection.fetchval(
                    "SELECT payload FROM fs2_configuration_plans WHERE id=$1",
                    plan.plan_id,
                )
                if _decode_configuration_json(existing, "configuration plan") != plan.model_dump(mode="json"):
                    raise ConflictError("configuration plan identity was reused") from None
                return
            await self._audit(
                connection,
                actor=plan.created_by,
                tenant_id=None,
                token_id=None,
                action="configuration.plan.persist",
                target_type="configuration_plan",
                target_id=str(plan.plan_id),
                outcome="succeeded",
                detail={
                    "base_revision": plan.base_revision,
                    "proposed_etag": plan.proposed_etag,
                    "terraform_required": plan.terraform.required,
                },
            )

    async def configuration_get_plan(self, plan_id: UUID) -> ConfigurationPlan | None:
        async with self.pool.acquire() as connection:
            payload = await connection.fetchval(
                "SELECT payload FROM fs2_configuration_plans WHERE id=$1",
                plan_id,
            )
        if payload is None:
            return None
        return ConfigurationPlan.model_validate(_decode_configuration_json(payload, "configuration plan"))

    async def configuration_save_status(self, status: ReconciliationStatus) -> None:
        payload_value = status.model_dump(mode="json")
        payload = json.dumps(payload_value, sort_keys=True, separators=(",", ":"))
        async with self.pool.acquire() as connection, connection.transaction():
            row_id = await connection.fetchval(
                """
                INSERT INTO fs2_configuration_reconciliation_events(
                    reconciliation_id,plan_id,phase,payload
                ) VALUES($1,$2,$3,$4)
                ON CONFLICT (reconciliation_id,phase) DO NOTHING RETURNING id
                """,
                status.reconciliation_id,
                status.plan_id,
                str(status.phase),
                payload,
            )
            if row_id is None:
                existing = await connection.fetchval(
                    """
                    SELECT payload FROM fs2_configuration_reconciliation_events
                    WHERE reconciliation_id=$1 AND phase=$2
                    """,
                    status.reconciliation_id,
                    str(status.phase),
                )
                if _decode_configuration_json(existing, "configuration status") != payload_value:
                    raise ConflictError("configuration reconciliation phase was rewritten")

    async def configuration_get_status(self, reconciliation_id: UUID) -> ReconciliationStatus | None:
        async with self.pool.acquire() as connection:
            payload = await connection.fetchval(
                """
                SELECT payload FROM fs2_configuration_reconciliation_events
                WHERE reconciliation_id=$1 ORDER BY id DESC LIMIT 1
                """,
                reconciliation_id,
            )
        if payload is None:
            return None
        return ReconciliationStatus.model_validate(
            _decode_configuration_json(payload, "configuration reconciliation status")
        )

    @retry_serialization
    async def model_deployment_append_revision(
        self,
        request: ModelDeploymentAppendRequest,
    ) -> ModelDeploymentAppendResult:
        key_value = request.idempotency_key.encode("utf-8")
        request_value = model_deployment_append_payload(request)
        candidates = self.hasher.candidate_digests(
            key_value,
            context="fs2-serve.model-deployment-idempotency/v1",
        )
        async with self.pool.acquire() as connection, connection.transaction():
            await self._model_deployment_idempotency_lock(
                connection,
                request.actor_id,
                request.idempotency_key,
            )
            for key_id, key_hmac in candidates:
                receipt = await connection.fetchrow(
                    """
                    SELECT request_hmac,namespace,name,revision
                    FROM fs2_model_deployment_idempotency
                    WHERE actor_id=$1 AND hmac_key_id=$2 AND key_hmac=$3
                    """,
                    request.actor_id,
                    key_id,
                    key_hmac,
                )
                if receipt is None:
                    continue
                replay_hmac = self.hasher.digest_for(
                    key_id,
                    request_value,
                    context="fs2-serve.model-deployment-request/v1",
                )
                if not secrets.compare_digest(replay_hmac, receipt["request_hmac"]):
                    raise ConflictError("model deployment idempotency key is bound to another request")
                row = await connection.fetchrow(
                    """
                    SELECT * FROM fs2_model_deployment_revisions
                    WHERE namespace=$1 AND name=$2 AND revision=$3
                    """,
                    receipt["namespace"],
                    receipt["name"],
                    receipt["revision"],
                )
                if row is None:
                    raise ConflictError("model deployment idempotency receipt is incomplete")
                return ModelDeploymentAppendResult(
                    value=self._model_deployment_revision(row),
                    reused=True,
                )

            await self._model_deployment_lock(connection, request.namespace, request.name)
            current_row = await connection.fetchrow(
                """
                SELECT revision.*
                FROM fs2_model_deployments deployment
                JOIN fs2_model_deployment_revisions revision
                  ON revision.namespace=deployment.namespace
                 AND revision.name=deployment.name
                 AND revision.revision=deployment.current_revision
                WHERE deployment.namespace=$1 AND deployment.name=$2
                FOR UPDATE OF deployment
                """,
                request.namespace,
                request.name,
            )
            current = self._model_deployment_revision(current_row) if current_row is not None else None
            if current is None:
                if request.action is not ModelDeploymentRevisionAction.CREATE or request.expected_etag is not None:
                    raise ConflictError("model deployment create does not match current state")
                revision_number = 1
                previous_revision = None
            else:
                if request.action is ModelDeploymentRevisionAction.CREATE:
                    raise ConflictError("model deployment already exists")
                if request.expected_etag != current.etag:
                    raise ConflictError("model deployment ETag is stale")
                if (
                    request.spec.tenant_id != current.tenant_id
                    or request.spec.model_ref != current.spec.model_ref
                    or request.spec.runtime.profile != current.spec.runtime.profile
                ):
                    raise ConflictError("model deployment immutable identity changed")
                if spec_digest(request.spec) == current.etag:
                    raise ConflictError("model deployment revision contains no desired-state change")
                revision_number = current.revision + 1
                previous_revision = current.revision

            etag = spec_digest(request.spec)
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_model_deployment_revisions(
                    namespace,name,tenant_id,revision,etag,spec,action,created_by,previous_revision
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *
                """,
                request.namespace,
                request.name,
                request.spec.tenant_id,
                revision_number,
                etag,
                json.dumps(
                    request.spec.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                request.action.value,
                request.actor,
                previous_revision,
            )
            assert row is not None
            if current is None:
                await connection.execute(
                    """
                    INSERT INTO fs2_model_deployments(
                        namespace,name,tenant_id,current_revision,current_etag,updated_by
                    ) VALUES($1,$2,$3,$4,$5,$6)
                    """,
                    request.namespace,
                    request.name,
                    request.spec.tenant_id,
                    revision_number,
                    etag,
                    request.actor,
                )
            else:
                await connection.execute(
                    """
                    UPDATE fs2_model_deployments
                    SET current_revision=$3,current_etag=$4,updated_at=clock_timestamp(),updated_by=$5
                    WHERE namespace=$1 AND name=$2
                    """,
                    request.namespace,
                    request.name,
                    revision_number,
                    etag,
                    request.actor,
                )
            active_key_id, key_hmac = self.hasher.digest(
                key_value,
                context="fs2-serve.model-deployment-idempotency/v1",
            )
            request_hmac = self.hasher.digest_for(
                active_key_id,
                request_value,
                context="fs2-serve.model-deployment-request/v1",
            )
            await connection.execute(
                """
                INSERT INTO fs2_model_deployment_idempotency(
                    actor_id,hmac_key_id,key_hmac,request_hmac,namespace,name,revision
                ) VALUES($1,$2,$3,$4,$5,$6,$7)
                """,
                request.actor_id,
                active_key_id,
                key_hmac,
                request_hmac,
                request.namespace,
                request.name,
                revision_number,
            )
            await self._audit(
                connection,
                actor=request.actor,
                tenant_id=request.spec.tenant_id,
                token_id=None,
                action=f"model_deployment.revision.{request.action.value}",
                target_type="model_deployment",
                target_id=model_deployment_audit_target(request.namespace, request.name),
                outcome="succeeded",
                detail={
                    "namespace": request.namespace,
                    "name": request.name,
                    "revision": revision_number,
                    "previous_revision": previous_revision,
                    "etag": etag,
                    "idempotent_replay": False,
                },
            )
            return ModelDeploymentAppendResult(value=self._model_deployment_revision(row))

    @staticmethod
    def _validate_model_deployment_read(
        *,
        namespace: str,
        tenant_id: str | None,
        limit: int,
    ) -> None:
        if not 1 <= len(namespace) <= 63 or not 1 <= limit <= 201:
            raise ValueError("model deployment read is outside the bound")
        if tenant_id is not None and not 1 <= len(tenant_id) <= 120:
            raise ValueError("model deployment tenant is outside the bound")

    async def model_deployment_list(
        self,
        *,
        namespace: str,
        tenant_id: str | None,
        after_name: str | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=limit)
        if after_name is not None and not 1 <= len(after_name) <= 253:
            raise ValueError("model deployment cursor is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT revision.*
                FROM fs2_model_deployments deployment
                JOIN fs2_model_deployment_revisions revision
                  ON revision.namespace=deployment.namespace
                 AND revision.name=deployment.name
                 AND revision.revision=deployment.current_revision
                WHERE deployment.namespace=$1
                  AND ($2::text IS NULL OR deployment.tenant_id=$2)
                  AND ($3::text IS NULL OR deployment.name>$3)
                ORDER BY deployment.name
                LIMIT $4
                """,
                namespace,
                tenant_id,
                after_name,
                limit,
            )
        return [self._model_deployment_revision(row) for row in rows]

    async def model_deployment_current(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentRevision | None:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=1)
        if not 1 <= len(name) <= 253:
            raise ValueError("model deployment name is outside the bound")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT revision.*
                FROM fs2_model_deployments deployment
                JOIN fs2_model_deployment_revisions revision
                  ON revision.namespace=deployment.namespace
                 AND revision.name=deployment.name
                 AND revision.revision=deployment.current_revision
                WHERE deployment.namespace=$1 AND deployment.name=$2
                  AND ($3::text IS NULL OR deployment.tenant_id=$3)
                """,
                namespace,
                name,
                tenant_id,
            )
        return self._model_deployment_revision(row) if row is not None else None

    async def model_deployment_history(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
        before_revision: int | None,
        limit: int,
    ) -> list[ModelDeploymentRevision]:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=limit)
        if not 1 <= len(name) <= 253 or (before_revision is not None and before_revision < 2):
            raise ValueError("model deployment history query is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT revision.*
                FROM fs2_model_deployment_revisions revision
                JOIN fs2_model_deployments deployment
                  ON deployment.namespace=revision.namespace AND deployment.name=revision.name
                WHERE revision.namespace=$1 AND revision.name=$2
                  AND ($3::text IS NULL OR deployment.tenant_id=$3)
                  AND ($4::bigint IS NULL OR revision.revision<$4)
                ORDER BY revision.revision DESC
                LIMIT $5
                """,
                namespace,
                name,
                tenant_id,
                before_revision,
                limit,
            )
        return [self._model_deployment_revision(row) for row in rows]

    async def model_deployment_append_status(
        self,
        observation: ModelDeploymentStatusObservation,
    ) -> ModelDeploymentStatusObservation:
        payload_value = observation.status.model_dump(mode="json")
        payload = json.dumps(payload_value, sort_keys=True, separators=(",", ":"))
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, observation.observation_id)
            existing_row = await connection.fetchrow(
                "SELECT * FROM fs2_model_deployment_status_events WHERE observation_id=$1",
                observation.observation_id,
            )
            if existing_row is not None:
                existing = self._model_deployment_status(existing_row)
                if existing != observation:
                    raise ConflictError("model deployment observation identity was reused")
                return existing
            await self._model_deployment_lock(connection, observation.namespace, observation.name)
            revision = await connection.fetchrow(
                """
                SELECT tenant_id,etag FROM fs2_model_deployment_revisions
                WHERE namespace=$1 AND name=$2 AND revision=$3
                """,
                observation.namespace,
                observation.name,
                observation.revision,
            )
            if (
                revision is None
                or revision["tenant_id"] != observation.tenant_id
                or revision["etag"] != observation.status.spec_digest
            ):
                raise ConflictError("model deployment observation has no matching desired revision")
            latest_row = await connection.fetchrow(
                """
                SELECT * FROM fs2_model_deployment_status_events
                WHERE namespace=$1 AND name=$2
                ORDER BY id DESC
                LIMIT 1
                """,
                observation.namespace,
                observation.name,
            )
            if latest_row is not None and model_deployment_status_precedes(
                observation,
                self._model_deployment_status(latest_row),
            ):
                raise ConflictError("model deployment observation is older than current status")
            row = await connection.fetchrow(
                """
                INSERT INTO fs2_model_deployment_status_events(
                    observation_id,source_uid,source_resource_version,namespace,name,tenant_id,
                    revision,spec_etag,status,observed_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *
                """,
                observation.observation_id,
                observation.source_uid,
                observation.source_resource_version,
                observation.namespace,
                observation.name,
                observation.tenant_id,
                observation.revision,
                observation.status.spec_digest,
                payload,
                observation.observed_at,
            )
            assert row is not None
            return self._model_deployment_status(row)

    async def model_deployment_status(
        self,
        *,
        namespace: str,
        name: str,
        tenant_id: str | None,
    ) -> ModelDeploymentStatusObservation | None:
        self._validate_model_deployment_read(namespace=namespace, tenant_id=tenant_id, limit=1)
        if not 1 <= len(name) <= 253:
            raise ValueError("model deployment name is outside the bound")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT event.*
                FROM fs2_model_deployment_status_events event
                JOIN fs2_model_deployments deployment
                  ON deployment.namespace=event.namespace AND deployment.name=event.name
                WHERE event.namespace=$1 AND event.name=$2
                  AND ($3::text IS NULL OR deployment.tenant_id=$3)
                ORDER BY event.id DESC
                LIMIT 1
                """,
                namespace,
                name,
                tenant_id,
            )
        return self._model_deployment_status(row) if row is not None else None

    async def admin_key_usage(self, token_ids: tuple[UUID, ...], *, tenant_id: str | None) -> list[AdminKeyUsageRecord]:
        if len(token_ids) > 1000 or len(set(token_ids)) != len(token_ids):
            raise ValueError("key usage request is outside the bound")
        if not token_ids:
            return []
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH requested(token_id) AS (SELECT unnest($1::uuid[]))
                SELECT requested.token_id,count(fact.operation_id)::bigint AS terminal_operations,
                       COALESCE(sum(fact.estimated_gpu_seconds),0)::double precision AS estimated_gpu_seconds,
                       sum(fact.input_tokens)::bigint AS input_tokens,
                       sum(fact.output_tokens)::bigint AS output_tokens,
                       count(*) FILTER (
                           WHERE fact.input_tokens IS NOT NULL AND fact.output_tokens IS NOT NULL
                       )::bigint AS token_reported_operations,
                       count(*) FILTER (WHERE fact.modality_usage IS NOT NULL)::bigint
                           AS modality_reported_operations
                FROM requested
                JOIN fs2_tokens token ON token.id=requested.token_id
                    AND ($2::text IS NULL OR token.tenant_id=$2)
                LEFT JOIN fs2_usage_facts fact ON fact.token_id=requested.token_id
                GROUP BY requested.token_id ORDER BY requested.token_id
                """,
                list(token_ids),
                tenant_id,
            )
            modality_rows = await connection.fetch(
                """
                WITH totals AS (
                    SELECT fact.token_id,item.value->>'modality' AS modality,
                           item.value->>'direction' AS direction,item.value->>'unit' AS unit,
                           sum((item.value->>'amount')::double precision)::double precision AS amount
                    FROM fs2_usage_facts fact
                    JOIN fs2_tokens token ON token.id=fact.token_id
                    CROSS JOIN LATERAL jsonb_array_elements(fact.modality_usage) item(value)
                    WHERE fact.token_id=ANY($1::uuid[]) AND ($2::text IS NULL OR token.tenant_id=$2)
                    GROUP BY fact.token_id,2,3,4
                ), bounded AS (
                    SELECT totals.*,row_number() OVER (
                        PARTITION BY token_id ORDER BY modality,direction,unit
                    ) AS ordinal
                    FROM totals
                )
                SELECT token_id,modality,direction,unit,amount FROM bounded
                WHERE ordinal<=33 ORDER BY token_id,ordinal
                """,
                list(token_ids),
                tenant_id,
            )
        modalities: dict[UUID, list[ModalityUsage]] = {}
        for row in modality_rows:
            values = modalities.setdefault(row["token_id"], [])
            values.append(
                ModalityUsage(
                    modality=row["modality"],
                    direction=row["direction"],
                    unit=row["unit"],
                    amount=row["amount"],
                )
            )
        if any(len(values) > 32 for values in modalities.values()):
            raise RuntimeError("stored modality usage exceeds the reporting bound")
        return [
            AdminKeyUsageRecord(
                token_id=row["token_id"],
                terminal_operations=row["terminal_operations"],
                estimated_gpu_seconds=row["estimated_gpu_seconds"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                token_reported_operations=row["token_reported_operations"],
                modality_reported_operations=row["modality_reported_operations"],
                modality_units=modalities.get(row["token_id"], []),
            )
            for row in rows
        ]

    @retry_serialization
    async def append_operation(
        self,
        *,
        principal: Principal,
        admission: AdmissionRequest,
        model_revision: str,
        reserved_gpu_seconds: float,
        max_attempts: int,
        dispatch_snapshot: str | None = None,
        dynamic_fence: DynamicAdmissionFence | None = None,
        scientific_admission_factory: Callable[[OperationView], dict[str, object]] | None = None,
    ) -> OperationView:
        async with self.pool.acquire() as connection, connection.transaction():
            await self._token_lock(connection, principal.token_id)
            await connection.execute(
                "SELECT pg_advisory_xact_lock(fs2_activation_model_lock_key($1))", admission.model_id
            )
            token = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1 FOR UPDATE", principal.token_id)
            now = await connection.fetchval("SELECT clock_timestamp()")
            if (
                token is None
                or token["revoked_at"] is not None
                or (token["expires_at"] is not None and token["expires_at"] <= now)
            ):
                raise PermissionError("token is no longer active")
            existing = await connection.fetchrow(
                """
                SELECT * FROM fs2_operations
                WHERE tenant_id=$1 AND principal_id=$2 AND token_id=$3 AND idempotency_key=$4 FOR UPDATE
                """,
                principal.tenant_id,
                principal.principal_id,
                principal.token_id,
                admission.idempotency_key,
            )
            if existing is not None:
                try:
                    request_hmac = self.hasher.digest_for(
                        existing["request_hmac_key_id"],
                        admission.request_body,
                        context="fs2-serve.request/v1",
                    )
                except ValueError as exc:
                    raise ConflictError("idempotency replay key is unavailable") from exc
                comparable = (
                    existing["model_id"],
                    existing["model_revision"],
                    existing["protocol"],
                    existing["operation"],
                    existing["request_content_type"],
                    existing["request_hmac"],
                )
                incoming = (
                    admission.model_id,
                    model_revision,
                    admission.protocol,
                    admission.operation,
                    admission.request_content_type,
                    request_hmac,
                )
                if comparable != incoming:
                    raise ConflictError("idempotency key is already bound to a different request")
                operation = self._operation(existing, reused=True)
                await self._stage_scientific_admission(
                    connection,
                    operation,
                    scientific_admission_factory,
                )
                return operation
            if (dynamic_fence is None) != (dispatch_snapshot is None):
                raise ConflictError("dynamic admission fence and dispatch snapshot must be supplied together")
            if dynamic_fence is not None:
                await self._model_deployment_lock(
                    connection,
                    dynamic_fence.namespace,
                    dynamic_fence.name,
                )
                desired_row = await connection.fetchrow(
                    """
                    SELECT deployment.tenant_id,deployment.current_etag,revision.spec
                    FROM fs2_model_deployments deployment
                    JOIN fs2_model_deployment_revisions revision
                      ON revision.namespace=deployment.namespace
                     AND revision.name=deployment.name
                     AND revision.revision=deployment.current_revision
                    WHERE deployment.namespace=$1 AND deployment.name=$2
                    FOR UPDATE OF deployment
                    """,
                    dynamic_fence.namespace,
                    dynamic_fence.name,
                )
                desired_spec = (
                    None
                    if desired_row is None
                    else ModelDeploymentSpec.model_validate(
                        _decode_configuration_json(desired_row["spec"], "model deployment desired state")
                    )
                )
                if (
                    desired_row is None
                    or desired_spec is None
                    or desired_row["current_etag"] != dynamic_fence.etag
                    or desired_row["tenant_id"] != principal.tenant_id
                    or desired_spec.model_ref != admission.model_id
                    or desired_spec.lifecycle.desired_state is not DesiredState.ENABLED
                ):
                    raise ConflictError("dynamic model no longer accepts admissions")
            hmac_key_id, request_hmac = self.hasher.digest(admission.request_body, context="fs2-serve.request/v1")
            rate_started = token["rate_window_started_at"]
            rate_requests = token["rate_window_requests"]
            if token["rate_limit_requests"] is not None and token["rate_window_seconds"] is not None:
                if rate_started is None or (now - rate_started).total_seconds() >= token["rate_window_seconds"]:
                    rate_started = now
                    rate_requests = 0
                if rate_requests >= token["rate_limit_requests"]:
                    raise RateLimitExceededError("token rate window is exhausted")
            if token["request_budget"] is not None and token["requests_used"] >= token["request_budget"]:
                raise BudgetExceededError("request budget exhausted")
            if token["gpu_seconds_budget"] is not None and (
                token["gpu_seconds_used"] + token["gpu_seconds_reserved"] + reserved_gpu_seconds
                > token["gpu_seconds_budget"]
            ):
                raise BudgetExceededError("GPU-seconds reservation exceeds token budget")
            active = await connection.fetchval(
                "SELECT count(*) FROM fs2_operations WHERE token_id=$1 AND status IN ('queued','activating','running')",
                principal.token_id,
            )
            if int(active) >= token["max_concurrency"]:
                raise ConcurrencyExceededError("token concurrency limit reached")
            operation_id = uuid4()
            encrypted = self.cipher.encrypt(
                admission.request_body,
                aad=self.cipher.aad(operation_id, principal.tenant_id, admission.model_id, "request"),
            )
            try:
                row = await connection.fetchrow(
                    """
                    INSERT INTO fs2_operations
                        (id,tenant_id,principal_id,token_id,model_id,model_revision,protocol,operation,
                         idempotency_key,request_hmac_key_id,request_hmac,request_key_id,request_nonce,
                         request_ciphertext,request_content_type,traceparent,deadline_at,payload_expires_at,
                         max_attempts,reserved_gpu_seconds,dispatch_snapshot)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                           clock_timestamp()+make_interval(secs=>$18::double precision),$19,$20,$21::jsonb)
                    RETURNING *
                    """,
                    operation_id,
                    principal.tenant_id,
                    principal.principal_id,
                    principal.token_id,
                    admission.model_id,
                    model_revision,
                    admission.protocol,
                    admission.operation,
                    admission.idempotency_key,
                    hmac_key_id,
                    request_hmac,
                    encrypted.key_id,
                    encrypted.nonce,
                    encrypted.value,
                    admission.request_content_type,
                    admission.traceparent,
                    admission.deadline_at,
                    self.payload_ttl_seconds,
                    max_attempts,
                    reserved_gpu_seconds,
                    dispatch_snapshot,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ConflictError("idempotency key raced with another request") from exc
            assert row is not None
            operation = self._operation(row)
            await self._stage_scientific_admission(
                connection,
                operation,
                scientific_admission_factory,
            )
            await connection.execute(
                """
                UPDATE fs2_tokens
                SET requests_used=requests_used+1,last_used_at=$5,
                    gpu_seconds_reserved=gpu_seconds_reserved+$2,
                    rate_window_started_at=$3,
                    rate_window_requests=CASE WHEN rate_limit_requests IS NULL THEN 0 ELSE $4 END
                WHERE id=$1
                """,
                principal.token_id,
                reserved_gpu_seconds,
                rate_started,
                rate_requests + 1 if token["rate_limit_requests"] is not None else 0,
                now,
            )
            await self._event(connection, operation_id, "accepted", OperationStatus.QUEUED, 0)
            await self._audit(
                connection,
                actor=principal.principal_id,
                tenant_id=principal.tenant_id,
                token_id=principal.token_id,
                action="operation.admit",
                target_type="operation",
                target_id=str(operation_id),
                outcome="queued",
                detail={"model_id": admission.model_id, "protocol": admission.protocol},
            )
            return operation

    @staticmethod
    async def _stage_scientific_admission(
        connection: asyncpg.Connection[Any],
        operation: OperationView,
        factory: Callable[[OperationView], dict[str, object]] | None,
    ) -> None:
        if factory is None:
            return
        if operation.protocol != "scientific-batch-v1":
            raise ConflictError("scientific admission outbox requires a scientific batch Operation")
        if await connection.fetchval(
            "SELECT true FROM fs2_scientific_batches WHERE operation_id=$1",
            operation.id,
        ):
            # An idempotent client replay must not recreate an already
            # consumed outbox from today's policy or execution bindings.
            return
        payload = factory(operation)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload_json.encode("utf-8")) > 4 * 1024 * 1024:
            raise ConflictError("scientific admission outbox exceeds the durable bound")
        await connection.execute(
            """
            INSERT INTO fs2_scientific_admission_outbox(operation_id,payload)
            VALUES($1,$2::jsonb)
            ON CONFLICT (operation_id) DO NOTHING
            """,
            operation.id,
            payload_json,
        )
        stored = await connection.fetchval(
            "SELECT payload FROM fs2_scientific_admission_outbox WHERE operation_id=$1 FOR SHARE",
            operation.id,
        )
        if stored is None or _decode_configuration_json(stored, "scientific admission outbox") != payload:
            raise ConflictError("scientific admission outbox already contains another frozen request")

    async def get_scientific_admission(self, operation_id: UUID) -> PendingScientificAdmission | None:
        async with self.pool.acquire() as connection:
            record = await connection.fetchrow(
                "SELECT operation_id,payload,created_at FROM fs2_scientific_admission_outbox WHERE operation_id=$1",
                operation_id,
            )
        if record is None:
            return None
        return PendingScientificAdmission(
            operation_id=record["operation_id"],
            payload=_decode_configuration_json(record["payload"], "scientific admission outbox"),
            created_at=record["created_at"],
        )

    async def list_scientific_admissions(self, *, limit: int = 100) -> list[PendingScientificAdmission]:
        if not 1 <= limit <= 1000:
            raise ValueError("scientific admission page is outside the bound")
        async with self.pool.acquire() as connection:
            records = await connection.fetch(
                """
                SELECT operation_id,payload,created_at
                FROM fs2_scientific_admission_outbox
                ORDER BY created_at,operation_id
                LIMIT $1
                """,
                limit,
            )
        return [
            PendingScientificAdmission(
                operation_id=record["operation_id"],
                payload=_decode_configuration_json(record["payload"], "scientific admission outbox"),
                created_at=record["created_at"],
            )
            for record in records
        ]

    async def complete_scientific_admission(self, operation_id: UUID) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM fs2_scientific_admission_outbox WHERE operation_id=$1",
                operation_id,
            )

    async def get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> OperationView:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fs2_operations WHERE id=$1 AND ($2::text IS NULL OR tenant_id=$2)",
                operation_id,
                tenant_id,
            )
            if row is None:
                raise NotFoundError("operation not found")
            return self._operation(row)

    async def get_operation_result(self, operation_id: UUID, *, tenant_id: str) -> OperationResult:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM fs2_operations WHERE id=$1 AND tenant_id=$2", operation_id, tenant_id
            )
            if row is None:
                raise NotFoundError("operation not found")
            metadata = self._operation(row)
            if metadata.status != OperationStatus.SUCCEEDED:
                raise ConflictError("operation has no successful result")
            if not metadata.result_available:
                raise ConflictError("operation result is unavailable")
            envelope = Ciphertext(
                row["response_key_id"], bytes(row["response_nonce"]), bytes(row["response_ciphertext"])
            )
            raw = self.cipher.decrypt(
                envelope, aad=self.cipher.aad(operation_id, row["tenant_id"], row["model_id"], "response")
            )
            try:
                result: Any = json.loads(raw)
            except json.JSONDecodeError:
                result = {"base64": base64.b64encode(raw).decode()}
            return OperationResult(operation=metadata, result=result)

    async def _expire_inactive_queued_batch(self) -> int:
        """Boundedly terminalize queued work whose PAT can no longer authorize execution."""

        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT o.id,o.token_id FROM fs2_operations o
                JOIN fs2_tokens t ON t.id=o.token_id
                WHERE o.status='queued'
                  AND (t.revoked_at IS NOT NULL
                       OR (t.expires_at IS NOT NULL AND t.expires_at<=clock_timestamp()))
                ORDER BY o.available_at,o.accepted_at,o.id LIMIT $1
                """,
                _CLAIM_BATCH_SIZE,
            )
        expired = 0
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["token_id"])
                token = await connection.fetchrow(
                    """
                    SELECT *, revoked_at IS NULL
                        AND (expires_at IS NULL OR expires_at>clock_timestamp()) AS active
                    FROM fs2_tokens WHERE id=$1 FOR UPDATE
                    """,
                    candidate["token_id"],
                )
                if token is None or token["active"]:
                    continue
                operation = await connection.fetchrow(
                    """
                    SELECT id,token_id,reserved_gpu_seconds,attempt FROM fs2_operations
                    WHERE id=$1 AND token_id=$2 AND status='queued'
                    FOR UPDATE SKIP LOCKED
                    """,
                    candidate["id"],
                    candidate["token_id"],
                )
                if operation is None:
                    continue
                await self._expire_locked_inactive_operation(connection, operation)
                expired += 1
        return expired

    async def _expire_locked_inactive_operation(
        self, connection: asyncpg.Connection[Any], operation: asyncpg.Record
    ) -> None:
        """Expire one locked queued operation and release its reservation atomically."""

        row = await connection.fetchrow(
            """
            UPDATE fs2_operations SET status='expired',completed_at=clock_timestamp(),
                outcome='expired',error_code='token_inactive',error_detail=NULL,
                worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                fencing_token=fencing_token+1,reserved_gpu_seconds=0
            WHERE id=$1 AND token_id=$2 AND status='queued' RETURNING *
            """,
            operation["id"],
            operation["token_id"],
        )
        if row is None:
            return
        await connection.execute(
            """
            UPDATE fs2_tokens SET gpu_seconds_reserved=
                GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1
            """,
            operation["token_id"],
            operation["reserved_gpu_seconds"],
        )
        await self._event(
            connection,
            row["id"],
            "token_inactive",
            OperationStatus.EXPIRED,
            row["attempt"],
        )

    async def ensure_activation_intent(
        self,
        operation: ClaimedOperation,
        *,
        binding_digest: str,
        worker_id: str,
        fencing_token: int,
    ) -> ActivationIntent:
        return await self.activation.ensure_activation_intent(
            operation,
            binding_digest=binding_digest,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )

    async def get_activation_intent(self, operation_id: UUID) -> ActivationIntent:
        return await self.activation.get_activation_intent(operation_id)

    async def claim_activation_intent(
        self,
        identity: ActivationLeaderIdentity,
        *,
        leadership_fencing_token: int,
        lease_seconds: float,
    ) -> ClaimedActivationIntent | None:
        return await self.activation.claim_activation_intent(
            identity,
            leadership_fencing_token=leadership_fencing_token,
            lease_seconds=lease_seconds,
        )

    async def heartbeat_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        lease_seconds: float,
    ) -> None:
        await self.activation.heartbeat_activation_intent(
            intent_id,
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
            controller_id=controller_id,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        )

    async def activation_wait_budget(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        maximum_seconds: float,
    ) -> float:
        return await self.activation.activation_wait_budget(
            intent_id,
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
            controller_id=controller_id,
            fencing_token=fencing_token,
            maximum_seconds=maximum_seconds,
        )

    async def complete_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        scale_contract_digest: str,
        target: ActivationTargetState,
    ) -> ActivationIntent:
        return await self.activation.complete_activation_intent(
            intent_id,
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
            controller_id=controller_id,
            fencing_token=fencing_token,
            scale_contract_digest=scale_contract_digest,
            target=target,
        )

    async def retry_activation_intent(
        self,
        intent_id: UUID,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        controller_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
    ) -> ActivationIntent:
        return await self.activation.retry_activation_intent(
            intent_id,
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
            controller_id=controller_id,
            fencing_token=fencing_token,
            available_at=available_at,
            error_code=error_code,
        )

    async def request_scale_down(
        self,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
        model_id: str,
        model_revision: str,
        binding_digest: str,
        idle_before: datetime,
        max_attempts: int,
    ) -> ActivationIntent | None:
        return await self.activation.request_scale_down(
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
            model_id=model_id,
            model_revision=model_revision,
            binding_digest=binding_digest,
            idle_before=idle_before,
            max_attempts=max_attempts,
        )

    async def get_activation_target_state(self, model_id: str) -> ActivationTargetState | None:
        return await self.activation.get_activation_target_state(model_id)

    def activation_mutation_guard(
        self,
        intent: ClaimedActivationIntent,
        *,
        identity: ActivationLeaderIdentity,
        leadership_fencing_token: int,
    ) -> AbstractAsyncContextManager[None]:
        return self.activation.activation_mutation_guard(
            intent,
            identity=identity,
            leadership_fencing_token=leadership_fencing_token,
        )

    @retry_serialization
    async def claim_operation(self, worker_id: str, *, lease_seconds: float) -> ClaimedOperation | None:
        # Cleanup is deliberately bounded, while the active-token join below
        # prevents any remaining inactive rows from becoming a queue head.
        await self._expire_inactive_queued_batch()
        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT o.id,o.token_id FROM fs2_operations o
                JOIN fs2_tokens t ON t.id=o.token_id
                    AND t.revoked_at IS NULL
                    AND (t.expires_at IS NULL OR t.expires_at>clock_timestamp())
                WHERE o.status='queued' AND o.protocol<>'scientific-batch-v1'
                  AND o.protocol<>'scientific-artifact-upload-v1'
                  AND o.available_at<=clock_timestamp()
                  AND o.payload_expires_at>clock_timestamp()
                  AND (o.deadline_at IS NULL OR o.deadline_at>clock_timestamp())
                  AND o.attempt<o.max_attempts
                ORDER BY o.available_at,o.accepted_at,o.id LIMIT $1
                """,
                _CLAIM_BATCH_SIZE,
            )
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["token_id"])
                token = await connection.fetchrow(
                    "SELECT * FROM fs2_tokens WHERE id=$1 FOR UPDATE", candidate["token_id"]
                )
                if (
                    token is None
                    or token["revoked_at"] is not None
                    or (
                        token["expires_at"] is not None
                        and not await connection.fetchval(
                            "SELECT $1::timestamptz>clock_timestamp()", token["expires_at"]
                        )
                    )
                ):
                    inactive = await connection.fetchrow(
                        """
                        SELECT * FROM fs2_operations
                        WHERE id=$1 AND token_id=$2 AND status='queued'
                        FOR UPDATE SKIP LOCKED
                        """,
                        candidate["id"],
                        candidate["token_id"],
                    )
                    if inactive is not None:
                        await self._expire_locked_inactive_operation(connection, inactive)
                    continue
                row = await connection.fetchrow(
                    """
                    WITH charge AS (
                        SELECT id,reserved_gpu_seconds/GREATEST(1,max_attempts-attempt) AS amount
                        FROM fs2_operations WHERE id=$1 AND token_id=$4 AND status='queued'
                          AND protocol<>'scientific-batch-v1'
                          AND protocol<>'scientific-artifact-upload-v1'
                          AND available_at<=clock_timestamp() AND payload_expires_at>clock_timestamp()
                          AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                          AND attempt<max_attempts FOR UPDATE
                    )
                    UPDATE fs2_operations o SET status='activating',
                        activation_started_at=COALESCE(activation_started_at,clock_timestamp()),
                        attempt=attempt+1,worker_id=$2,fencing_token=fencing_token+1,
                        heartbeat_at=clock_timestamp(),
                        lease_expires_at=LEAST(clock_timestamp()+make_interval(secs=>$3::double precision),
                            COALESCE(deadline_at,'infinity'::timestamptz)),
                        estimated_gpu_seconds=estimated_gpu_seconds+charge.amount,
                        reserved_gpu_seconds=GREATEST(0,reserved_gpu_seconds-charge.amount)
                    FROM charge WHERE o.id=charge.id AND o.attempt<o.max_attempts
                    RETURNING o.*,charge.amount AS claim_charge
                    """,
                    candidate["id"],
                    worker_id,
                    lease_seconds,
                    candidate["token_id"],
                )
                if row is None:
                    continue
                per_attempt = row["claim_charge"]
                await connection.execute(
                    """
                    UPDATE fs2_tokens SET gpu_seconds_used=gpu_seconds_used+$2,
                        gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1
                    """,
                    row["token_id"],
                    per_attempt,
                )
                await self._event(
                    connection, row["id"], "activation_started", OperationStatus.ACTIVATING, row["attempt"]
                )
                metadata = self._operation(row)
                return ClaimedOperation(
                    **metadata.model_dump(),
                    request_content_type=row["request_content_type"],
                    traceparent=row["traceparent"],
                    dispatch_snapshot=(
                        None
                        if row["dispatch_snapshot"] is None
                        else json.dumps(
                            _decode_configuration_json(row["dispatch_snapshot"], "dynamic dispatch snapshot"),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    ),
                    worker_id=worker_id,
                )
        return None

    @retry_serialization
    async def complete_scientific_artifact_upload(
        self,
        operation_id: UUID,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> OperationView:
        """Atomically terminalize a verified upload operation without a worker lease."""

        async with self.pool.acquire() as connection, connection.transaction():
            token_id = await connection.fetchval(
                "SELECT token_id FROM fs2_operations WHERE id=$1",
                operation_id,
            )
            if token_id is None:
                raise NotFoundError("scientific artifact upload operation not found")
            # Match the global token -> operation mutation lock order.
            await self._token_lock(connection, token_id)
            current = await connection.fetchrow(
                "SELECT * FROM fs2_operations WHERE id=$1 FOR UPDATE",
                operation_id,
            )
            if (
                current is None
                or current["tenant_id"] != tenant_id
                or current["principal_id"] != principal_id
                or current["protocol"] != "scientific-artifact-upload-v1"
            ):
                raise NotFoundError("scientific artifact upload operation not found")
            if current["status"] == "succeeded":
                return self._operation(current, reused=True)
            if current["status"] != "queued":
                raise ConflictError("scientific artifact upload operation is not writable")
            row = await connection.fetchrow(
                """
                UPDATE fs2_operations
                SET status='succeeded',completed_at=clock_timestamp(),outcome='artifact_uploaded',
                    semantic_outcome='verified',http_status=201,reserved_gpu_seconds=0,
                    worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL
                WHERE id=$1 AND status='queued' AND protocol='scientific-artifact-upload-v1'
                RETURNING *
                """,
                operation_id,
            )
            if row is None:
                raise ConflictError("scientific artifact upload operation changed")
            await self._event(connection, operation_id, "artifact_uploaded", OperationStatus.SUCCEEDED, row["attempt"])
            await self._audit(
                connection,
                actor=principal_id,
                tenant_id=tenant_id,
                token_id=row["token_id"],
                action="scientific_artifact.upload.complete",
                target_type="operation",
                target_id=str(operation_id),
                outcome="succeeded",
            )
            return self._operation(row)

    async def read_request_payload(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> bytes:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM fs2_operations
                WHERE id=$1 AND worker_id=$2 AND fencing_token=$3
                  AND status IN ('activating','running') AND lease_expires_at>clock_timestamp()
                  AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                """,
                operation_id,
                worker_id,
                fencing_token,
            )
            if row is None or row["request_ciphertext"] is None:
                raise StaleLeaseError("operation lease or payload is stale")
            envelope = Ciphertext(row["request_key_id"], bytes(row["request_nonce"]), bytes(row["request_ciphertext"]))
            return self.cipher.decrypt(
                envelope, aad=self.cipher.aad(operation_id, row["tenant_id"], row["model_id"], "request")
            )

    @retry_serialization
    async def heartbeat(self, operation_id: UUID, *, worker_id: str, fencing_token: int, lease_seconds: float) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                """
                UPDATE fs2_operations SET heartbeat_at=clock_timestamp(),
                    lease_expires_at=LEAST(clock_timestamp()+make_interval(secs=>$4::double precision),
                        COALESCE(deadline_at,'infinity'::timestamptz))
                WHERE id=$1 AND worker_id=$2 AND fencing_token=$3
                  AND lease_expires_at>clock_timestamp() AND status IN ('activating','running')
                  AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                """,
                operation_id,
                worker_id,
                fencing_token,
                lease_seconds,
            )
            if result == "UPDATE 0":
                raise StaleLeaseError("operation lease or deadline is stale")

    @retry_serialization
    async def mark_ready(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> None:
        await self._lease_update(
            operation_id,
            worker_id,
            fencing_token,
            "SET ready_at=clock_timestamp()",
            "runtime_ready",
            OperationStatus.ACTIVATING,
        )

    @retry_serialization
    async def mark_running(
        self, operation_id: UUID, runtime: RuntimeIdentity, *, worker_id: str, fencing_token: int
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE fs2_operations SET status='running',started_at=clock_timestamp(),pod_uid=$4,node_uid=$5,
                    gpu_uuids=$6,gpu_count=$7,preemptible=$8
                WHERE id=$1 AND worker_id=$2 AND fencing_token=$3 AND status='activating'
                  AND lease_expires_at>clock_timestamp() AND (deadline_at IS NULL OR deadline_at>clock_timestamp())
                RETURNING attempt
                """,
                operation_id,
                worker_id,
                fencing_token,
                runtime.pod_uid,
                runtime.node_uid,
                runtime.gpu_uuids,
                runtime.gpu_count,
                runtime.preemptible,
            )
            if row is None:
                raise StaleLeaseError("operation lease is stale")
            await self._event(connection, operation_id, "inference_started", OperationStatus.RUNNING, row["attempt"])

    async def _lease_update(
        self,
        operation_id: UUID,
        worker_id: str,
        fencing_token: int,
        set_clause: str,
        event: str,
        status: OperationStatus,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""
                UPDATE fs2_operations {set_clause}
                WHERE id=$1 AND worker_id=$2 AND fencing_token=$3
                  AND lease_expires_at>clock_timestamp() AND status IN ('activating','running')
                  AND (deadline_at IS NULL OR deadline_at>clock_timestamp()) RETURNING attempt
                """,  # noqa: S608 - set_clause is an internal constant only
                operation_id,
                worker_id,
                fencing_token,
            )
            if row is None:
                raise StaleLeaseError("operation lease is stale")
            await self._event(connection, operation_id, event, status, row["attempt"])

    @retry_serialization
    async def complete_operation(
        self,
        operation_id: UUID,
        *,
        status: OperationStatus,
        outcome: str,
        semantic_outcome: str,
        http_status: int,
        response_body: bytes | None,
        response_content_type: str | None,
        error_code: str | None,
        error_detail: str | None,
        runtime: RuntimeIdentity,
        worker_id: str,
        fencing_token: int,
        usage: ReportedUsage | None = None,
    ) -> OperationView:
        if not status.terminal:
            raise ValueError("completion status must be terminal")
        async with self.pool.acquire() as connection:
            token_id = await connection.fetchval("SELECT token_id FROM fs2_operations WHERE id=$1", operation_id)
            if token_id is None:
                raise StaleLeaseError("operation lease is stale")
            async with connection.transaction():
                await self._token_lock(connection, token_id)
                await connection.fetchrow("SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", token_id)
                existing = await connection.fetchrow(
                    "SELECT * FROM fs2_operations WHERE id=$1 FOR UPDATE", operation_id
                )
                if existing is None:
                    raise StaleLeaseError("operation lease is stale")
                encrypted = None
                response_hmac_key_id = None
                response_hmac = None
                if response_body is not None:
                    response_hmac_key_id, response_hmac = self.hasher.digest(
                        response_body, context="fs2-serve.response/v1"
                    )
                    encrypted = self.cipher.encrypt(
                        response_body,
                        aad=self.cipher.aad(operation_id, existing["tenant_id"], existing["model_id"], "response"),
                    )
                row = await connection.fetchrow(
                    """
                    UPDATE fs2_operations
                    SET status=$2::fs2_operation_status,completed_at=clock_timestamp(),outcome=$3,
                        semantic_outcome=$4,http_status=$5,response_hmac_key_id=$6,response_hmac=$7,
                        response_key_id=$8,response_nonce=$9,response_ciphertext=$10,
                        response_content_type=$11,error_code=$12,error_detail=$13,pod_uid=$14,
                        node_uid=$15,gpu_uuids=$16,gpu_count=$17,preemptible=$18,reserved_gpu_seconds=0,
                        worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                        cold_start_seconds=CASE WHEN ready_at IS NULL THEN NULL
                            ELSE extract(epoch FROM ready_at-accepted_at) END,
                        input_tokens=$21,output_tokens=$22,modality_usage=$23::jsonb
                    WHERE id=$1 AND worker_id=$19 AND fencing_token=$20
                      AND status IN ('activating','running') AND lease_expires_at>clock_timestamp()
                      AND (deadline_at IS NULL OR deadline_at>clock_timestamp()) RETURNING *
                    """,
                    operation_id,
                    str(status),
                    outcome,
                    semantic_outcome,
                    http_status,
                    response_hmac_key_id,
                    response_hmac,
                    encrypted.key_id if encrypted else None,
                    encrypted.nonce if encrypted else None,
                    encrypted.value if encrypted else None,
                    response_content_type,
                    error_code,
                    sanitize_error_detail(error_detail or "") or None,
                    runtime.pod_uid,
                    runtime.node_uid,
                    runtime.gpu_uuids,
                    runtime.gpu_count,
                    runtime.preemptible,
                    worker_id,
                    fencing_token,
                    usage.input_tokens if usage is not None else None,
                    usage.output_tokens if usage is not None else None,
                    (
                        json.dumps([item.model_dump(mode="json") for item in usage.modalities])
                        if usage is not None and usage.modalities is not None
                        else None
                    ),
                )
                if row is None:
                    raise StaleLeaseError("operation lease or deadline is stale")
                await connection.execute(
                    "UPDATE fs2_tokens SET gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1",
                    row["token_id"],
                    existing["reserved_gpu_seconds"],
                )
                await self._event(
                    connection,
                    operation_id,
                    outcome,
                    status,
                    row["attempt"],
                    {"semantic_outcome": semantic_outcome},
                )
                await self._audit(
                    connection,
                    actor="worker",
                    tenant_id=row["tenant_id"],
                    token_id=row["token_id"],
                    action="operation.complete",
                    target_type="operation",
                    target_id=str(operation_id),
                    outcome=outcome,
                    detail={"model_id": row["model_id"], "semantic_outcome": semantic_outcome},
                )
                return self._operation(row)

    @retry_serialization
    async def retry_operation(
        self,
        operation_id: UUID,
        *,
        worker_id: str,
        fencing_token: int,
        available_at: datetime,
        error_code: str,
        error_detail: str,
    ) -> OperationView:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                UPDATE fs2_operations
                SET status='queued',available_at=$4,error_code=$5,error_detail=$6,
                    worker_id=NULL,heartbeat_at=NULL,
                    lease_expires_at=NULL,fencing_token=fencing_token+1
                WHERE id=$1 AND worker_id=$2 AND fencing_token=$3 AND attempt<max_attempts
                  AND lease_expires_at>clock_timestamp() AND (deadline_at IS NULL OR $4<deadline_at)
                RETURNING *
                """,
                operation_id,
                worker_id,
                fencing_token,
                available_at,
                error_code,
                sanitize_error_detail(error_detail),
            )
            if row is None:
                raise ConflictError("operation retry is no longer admissible")
            await self._event(connection, operation_id, "retry_scheduled", OperationStatus.QUEUED, row["attempt"])
            return self._operation(row)

    @retry_serialization
    async def release_operation(self, operation_id: UUID, *, worker_id: str, fencing_token: int) -> OperationView:
        async with self.pool.acquire() as connection:
            token_id = await connection.fetchval("SELECT token_id FROM fs2_operations WHERE id=$1", operation_id)
            if token_id is None:
                raise StaleLeaseError("operation lease is stale")
            async with connection.transaction():
                await self._token_lock(connection, token_id)
                token = await connection.fetchrow("SELECT * FROM fs2_tokens WHERE id=$1 FOR UPDATE", token_id)
                existing = await connection.fetchrow(
                    "SELECT * FROM fs2_operations WHERE id=$1 FOR UPDATE", operation_id
                )
                if (
                    existing is None
                    or existing["worker_id"] != worker_id
                    or existing["fencing_token"] != fencing_token
                    or existing["status"] not in {"activating", "running"}
                ):
                    raise StaleLeaseError("operation lease is stale")
                can_requeue = bool(
                    token is not None
                    and token["revoked_at"] is None
                    and await connection.fetchval(
                        """
                        SELECT ($1::timestamptz IS NULL OR $1>clock_timestamp())
                           AND $2::timestamptz>clock_timestamp()
                           AND ($3::timestamptz IS NULL OR $3>clock_timestamp())
                           AND $4::integer<$5::integer
                        """,
                        token["expires_at"],
                        existing["payload_expires_at"],
                        existing["deadline_at"],
                        existing["attempt"],
                        existing["max_attempts"],
                    )
                )
                if can_requeue:
                    row = await connection.fetchrow(
                        """
                        UPDATE fs2_operations SET status='queued',available_at=clock_timestamp(),
                            worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                            fencing_token=fencing_token+1,error_code='worker_released',error_detail=NULL
                        WHERE id=$1 RETURNING *
                        """,
                        operation_id,
                    )
                else:
                    exhausted = existing["attempt"] >= existing["max_attempts"]
                    terminal_error = "attempts_exhausted" if exhausted else "release_not_admissible"
                    row = await connection.fetchrow(
                        """
                        UPDATE fs2_operations SET status='expired',completed_at=clock_timestamp(),
                            outcome='expired',error_code=$2,error_detail=NULL,
                            worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                            fencing_token=fencing_token+1,reserved_gpu_seconds=0
                        WHERE id=$1 RETURNING *
                        """,
                        operation_id,
                        terminal_error,
                    )
                    if token is not None:
                        await connection.execute(
                            """
                            UPDATE fs2_tokens SET gpu_seconds_reserved=
                                GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1
                            """,
                            token_id,
                            existing["reserved_gpu_seconds"],
                        )
                assert row is not None
                await self._event(
                    connection,
                    operation_id,
                    "worker_released" if can_requeue else terminal_error,
                    OperationStatus(row["status"]),
                    row["attempt"],
                )
                return self._operation(row)

    @retry_serialization
    async def cancel_operation(self, operation_id: UUID, *, tenant_id: str, actor: str) -> OperationView:
        async with self.pool.acquire() as connection:
            token_id = await connection.fetchval(
                "SELECT token_id FROM fs2_operations WHERE id=$1 AND tenant_id=$2", operation_id, tenant_id
            )
            if token_id is None:
                raise NotFoundError("operation not found")
            async with connection.transaction():
                await self._token_lock(connection, token_id)
                await connection.fetchrow("SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", token_id)
                existing = await connection.fetchrow(
                    "SELECT * FROM fs2_operations WHERE id=$1 AND tenant_id=$2 FOR UPDATE",
                    operation_id,
                    tenant_id,
                )
                if existing is None:
                    raise NotFoundError("operation not found")
                if existing["status"] in _TERMINAL:
                    return self._operation(existing)
                row = await connection.fetchrow(
                    """
                    UPDATE fs2_operations SET status='cancelled',completed_at=clock_timestamp(),outcome='cancelled',
                        error_code='cancelled_by_caller',error_detail=NULL,worker_id=NULL,heartbeat_at=NULL,
                        lease_expires_at=NULL,fencing_token=fencing_token+1,reserved_gpu_seconds=0
                    WHERE id=$1 RETURNING *
                    """,
                    operation_id,
                )
                assert row is not None
                await connection.execute(
                    "UPDATE fs2_tokens SET gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1",
                    row["token_id"],
                    existing["reserved_gpu_seconds"],
                )
                await self._event(connection, operation_id, "cancelled", OperationStatus.CANCELLED, row["attempt"])
                await self._audit(
                    connection,
                    actor=actor,
                    tenant_id=tenant_id,
                    token_id=row["token_id"],
                    action="operation.cancel",
                    target_type="operation",
                    target_id=str(operation_id),
                    outcome="cancelled",
                )
                return self._operation(row)

    @retry_serialization
    async def purge_operation_payload(self, operation_id: UUID, *, tenant_id: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                """
                UPDATE fs2_operations SET request_key_id=NULL,request_nonce=NULL,request_ciphertext=NULL,
                    response_key_id=NULL,response_nonce=NULL,response_ciphertext=NULL,
                    payload_purged_at=clock_timestamp()
                WHERE id=$1 AND tenant_id=$2 AND status IN ('succeeded','failed','cancelled','preempted','expired')
                """,
                operation_id,
                tenant_id,
            )
            if result == "UPDATE 0":
                raise ConflictError("operation is absent or nonterminal")

    @retry_serialization
    async def purge_expired_payloads(self) -> int:
        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT id,token_id FROM fs2_operations
                WHERE payload_expires_at<=clock_timestamp()
                  AND payload_purged_at IS NULL
                ORDER BY payload_expires_at,id LIMIT 100
                """
            )
        count = 0
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["token_id"])
                await connection.fetchrow("SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", candidate["token_id"])
                row = await connection.fetchrow(
                    """
                    SELECT id,token_id,status,reserved_gpu_seconds FROM fs2_operations
                    WHERE id=$1 AND payload_expires_at<=clock_timestamp() AND payload_purged_at IS NULL
                    FOR UPDATE SKIP LOCKED
                    """,
                    candidate["id"],
                )
                if row is None:
                    continue
                if row["status"] not in _TERMINAL and row["reserved_gpu_seconds"]:
                    await connection.execute(
                        "UPDATE fs2_tokens SET gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1",
                        row["token_id"],
                        row["reserved_gpu_seconds"],
                    )
                await connection.execute(
                    """
                    UPDATE fs2_operations SET request_key_id=NULL,request_nonce=NULL,request_ciphertext=NULL,
                        response_key_id=NULL,response_nonce=NULL,response_ciphertext=NULL,
                        payload_purged_at=clock_timestamp(),
                        status=CASE WHEN status IN ('queued','activating','running')
                            THEN 'expired'::fs2_operation_status ELSE status END,
                        completed_at=CASE WHEN status IN ('queued','activating','running')
                            THEN clock_timestamp() ELSE completed_at END,
                        outcome=CASE WHEN status IN ('queued','activating','running') THEN 'expired' ELSE outcome END,
                        error_code=CASE WHEN status IN ('queued','activating','running')
                            THEN 'payload_expired' ELSE error_code END,
                        error_detail=NULL,worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                        fencing_token=fencing_token+1,reserved_gpu_seconds=0 WHERE id=$1
                    """,
                    row["id"],
                )
                count += 1
        return count

    @retry_serialization
    async def expire_deadline_operations(self) -> int:
        """Boundedly terminalize queued deadlines using token-before-operation locking."""

        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT id,token_id FROM fs2_operations
                WHERE status='queued' AND deadline_at IS NOT NULL
                  AND deadline_at<=clock_timestamp()
                ORDER BY deadline_at,id LIMIT 100
                """
            )
        count = 0
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["token_id"])
                token = await connection.fetchrow(
                    "SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", candidate["token_id"]
                )
                if token is None:
                    continue
                operation = await connection.fetchrow(
                    """
                    SELECT * FROM fs2_operations
                    WHERE id=$1 AND token_id=$2 AND status='queued'
                      AND deadline_at IS NOT NULL AND deadline_at<=clock_timestamp()
                    FOR UPDATE SKIP LOCKED
                    """,
                    candidate["id"],
                    candidate["token_id"],
                )
                if operation is None:
                    continue
                row = await connection.fetchrow(
                    """
                    WITH expired AS (
                        UPDATE fs2_operations SET status='expired',completed_at=clock_timestamp(),
                            outcome='expired',error_code='deadline_exceeded',error_detail=NULL,
                            worker_id=NULL,heartbeat_at=NULL,lease_expires_at=NULL,
                            fencing_token=fencing_token+1,reserved_gpu_seconds=0
                        WHERE id=$1 AND token_id=$2 AND status='queued'
                          AND deadline_at IS NOT NULL AND deadline_at<=clock_timestamp()
                        RETURNING *
                    ), released AS (
                        UPDATE fs2_tokens SET gpu_seconds_reserved=
                            GREATEST(0,gpu_seconds_reserved-$3::double precision)
                        WHERE id=$2 AND EXISTS (SELECT 1 FROM expired)
                        RETURNING id
                    )
                    SELECT expired.id,expired.attempt
                    FROM expired JOIN released ON released.id=expired.token_id
                    """,
                    operation["id"],
                    operation["token_id"],
                    operation["reserved_gpu_seconds"],
                )
                if row is None:
                    continue
                await self._event(
                    connection,
                    row["id"],
                    "deadline_expired",
                    OperationStatus.EXPIRED,
                    row["attempt"],
                )
                count += 1
        return count

    @retry_serialization
    async def reap_stale_operations(self) -> int:
        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT id,token_id FROM fs2_operations WHERE status IN ('activating','running')
                  AND lease_expires_at<=clock_timestamp() ORDER BY lease_expires_at,id LIMIT 100
                """
            )
        count = 0
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["token_id"])
                await connection.fetchrow("SELECT id FROM fs2_tokens WHERE id=$1 FOR UPDATE", candidate["token_id"])
                row = await connection.fetchrow(
                    """
                    SELECT id,token_id,reserved_gpu_seconds,attempt FROM fs2_operations
                    WHERE id=$1 AND status IN ('activating','running')
                      AND lease_expires_at<=clock_timestamp() FOR UPDATE SKIP LOCKED
                    """,
                    candidate["id"],
                )
                if row is None:
                    continue
                requeued = await connection.fetchrow(
                    """
                    UPDATE fs2_operations SET status='queued',available_at=clock_timestamp(),worker_id=NULL,
                        heartbeat_at=NULL,lease_expires_at=NULL,fencing_token=fencing_token+1,
                        error_code='stale_worker_reaped',error_detail=NULL
                    WHERE id=$1 AND request_ciphertext IS NOT NULL AND payload_expires_at>clock_timestamp()
                      AND (deadline_at IS NULL OR deadline_at>clock_timestamp()) AND attempt<max_attempts
                    RETURNING id
                    """,
                    row["id"],
                )
                if requeued is None:
                    await connection.execute(
                        "UPDATE fs2_tokens SET gpu_seconds_reserved=GREATEST(0,gpu_seconds_reserved-$2) WHERE id=$1",
                        row["token_id"],
                        row["reserved_gpu_seconds"],
                    )
                    await connection.execute(
                        """
                        UPDATE fs2_operations SET status='expired',completed_at=clock_timestamp(),outcome='expired',
                            error_code='lease_recovery_exhausted',error_detail=NULL,worker_id=NULL,heartbeat_at=NULL,
                            lease_expires_at=NULL,fencing_token=fencing_token+1,reserved_gpu_seconds=0 WHERE id=$1
                        """,
                        row["id"],
                    )
                count += 1
        return count

    @retry_serialization
    async def delete_expired_rows(
        self,
        *,
        operation_retention_seconds: int,
        token_retention_seconds: int,
        audit_retention_seconds: int = 2592000,
        usage_retention_seconds: int = 7776000,
    ) -> dict[str, int]:
        # Keep operation deletion and token deletion in separate transactions.
        # No transaction may lock an operation and then a token: all state
        # transitions that need both use token -> operation ordering.
        async with self.pool.acquire() as connection, connection.transaction():
            operations = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT id FROM fs2_operations
                    WHERE status IN ('succeeded','failed','cancelled','preempted','expired')
                      AND completed_at < clock_timestamp()-make_interval(secs=>$1::double precision)
                    ORDER BY completed_at,id FOR UPDATE SKIP LOCKED LIMIT 100
                )
                DELETE FROM fs2_operations o USING candidates c WHERE o.id=c.id RETURNING o.id
                """,
                operation_retention_seconds,
            )
        async with self.pool.acquire() as connection:
            candidates = await connection.fetch(
                """
                SELECT t.id FROM fs2_tokens t
                WHERE ((t.revoked_at IS NOT NULL AND
                          t.revoked_at < clock_timestamp()-make_interval(secs=>$1::double precision))
                       OR (t.expires_at IS NOT NULL AND
                          t.expires_at < clock_timestamp()-make_interval(secs=>$1::double precision)))
                  AND NOT EXISTS (SELECT 1 FROM fs2_operations o WHERE o.token_id=t.id)
                ORDER BY COALESCE(t.revoked_at,t.expires_at),t.id LIMIT 100
                """,
                token_retention_seconds,
            )
        deleted_tokens = 0
        for candidate in candidates:
            async with self.pool.acquire() as connection, connection.transaction():
                await self._token_lock(connection, candidate["id"])
                row = await connection.fetchrow(
                    """
                    SELECT id FROM fs2_tokens WHERE id=$1
                      AND ((revoked_at IS NOT NULL AND
                            revoked_at < clock_timestamp()-make_interval(secs=>$2::double precision))
                           OR (expires_at IS NOT NULL AND
                            expires_at < clock_timestamp()-make_interval(secs=>$2::double precision)))
                    FOR UPDATE SKIP LOCKED
                    """,
                    candidate["id"],
                    token_retention_seconds,
                )
                if row is None:
                    continue
                result = await connection.execute(
                    """
                    DELETE FROM fs2_tokens t WHERE t.id=$1
                      AND NOT EXISTS (SELECT 1 FROM fs2_operations o WHERE o.token_id=t.id)
                    """,
                    candidate["id"],
                )
                deleted_tokens += result == "DELETE 1"
        async with self.pool.acquire() as connection, connection.transaction():
            audit = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT id FROM fs2_audit_events
                    WHERE occurred_at < clock_timestamp()-make_interval(secs=>$1::double precision)
                    ORDER BY occurred_at,id LIMIT 100
                )
                DELETE FROM fs2_audit_events a USING candidates c WHERE a.id=c.id RETURNING a.id
                """,
                audit_retention_seconds,
            )
            usage = await connection.fetch(
                """
                WITH candidates AS (
                    SELECT operation_id FROM fs2_usage_facts
                    WHERE occurred_at < clock_timestamp()-make_interval(secs=>$1::double precision)
                    ORDER BY occurred_at,operation_id LIMIT 100
                )
                DELETE FROM fs2_usage_facts f USING candidates c
                WHERE f.operation_id=c.operation_id RETURNING f.operation_id
                """,
                usage_retention_seconds,
            )
        return {
            "operations": len(operations),
            "tokens": deleted_tokens,
            "audit": len(audit),
            "usage": len(usage),
        }

    async def list_audit(self, *, tenant_id: str | None = None, limit: int = 100) -> list[AuditEvent]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM fs2_audit_events WHERE ($1::text IS NULL OR tenant_id=$1)
                ORDER BY occurred_at DESC,id DESC LIMIT $2
                """,
                tenant_id,
                limit,
            )
            result: list[AuditEvent] = []
            for row in rows:
                value = dict(row)
                value["detail"] = _decode_audit_detail(value["detail"])
                result.append(AuditEvent.model_validate(value))
            return result

    async def queue_counts(self) -> dict[tuple[str, str], int]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("SELECT model_id,status,count(*) AS count FROM fs2_operations GROUP BY 1,2")
            return {(row["model_id"], str(row["status"])): row["count"] for row in rows}

    async def oldest_queue_age(self) -> dict[str, float]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT model_id,
                    max(GREATEST(0,extract(epoch FROM clock_timestamp()-accepted_at))) AS age
                FROM fs2_operations WHERE status='queued' GROUP BY model_id
                """
            )
            return {row["model_id"]: float(row["age"]) for row in rows}

    async def terminal_accounting(self) -> list[TerminalAccounting]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT model_id,protocol,outcome,operations,estimated_gpu_seconds,
                       duration_seconds,cold_start_seconds
                FROM fs2_reporting_terminal_totals ORDER BY model_id,protocol,outcome
                """
            )
        return [TerminalAccounting.model_validate(dict(row)) for row in rows]

    async def admin_model_activity(self, model_ids: tuple[str, ...]) -> list[AdminModelActivity]:
        if (
            len(model_ids) > 256
            or len(set(model_ids)) != len(model_ids)
            or any(not 1 <= len(model_id) <= 128 for model_id in model_ids)
        ):
            raise ValueError("admin model activity request is outside the bound")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH requested(model_id) AS (SELECT unnest($1::text[]))
                SELECT requested.model_id,
                       count(operation.id) FILTER (WHERE operation.status='queued')::bigint AS queued_operations,
                       COALESCE((
                           SELECT intent.status::text
                           FROM fs2_activation_intents intent
                           WHERE intent.model_id=requested.model_id
                           ORDER BY intent.requested_at DESC,intent.id DESC LIMIT 1
                       ),'none') AS activation_phase,
                       clock_timestamp() AS observed_at
                FROM requested
                LEFT JOIN fs2_operations operation ON operation.model_id=requested.model_id
                GROUP BY requested.model_id
                ORDER BY requested.model_id
                """,
                list(model_ids),
            )
        return [
            AdminModelActivity(
                model_id=row["model_id"],
                queued_operations=row["queued_operations"],
                activation_phase=AdminActivationPhase(row["activation_phase"]),
                observed_at=row["observed_at"],
            )
            for row in rows
        ]

    async def admin_usage_window(self, *, from_at: datetime, to_at: datetime) -> AdminUsageWindow:
        if (
            from_at.tzinfo is None
            or to_at.tzinfo is None
            or not from_at < to_at
            or (to_at - from_at).total_seconds() > 31 * 24 * 60 * 60
        ):
            raise ValueError("admin usage window is invalid")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH terminal AS (
                    SELECT model_id,status,estimated_gpu_seconds,cold_start_seconds,
                           input_tokens,output_tokens,
                           GREATEST(0,extract(epoch FROM completed_at-accepted_at))
                               ::double precision AS latency_seconds
                    FROM fs2_operations
                    WHERE status IN ('succeeded','failed','cancelled','preempted','expired')
                      AND completed_at >= $1 AND completed_at < $2
                )
                SELECT model_id,
                       count(*)::bigint AS terminal_operations,
                       count(*) FILTER (WHERE status<>'succeeded')::bigint AS error_operations,
                       COALESCE(sum(estimated_gpu_seconds),0)::double precision AS estimated_gpu_seconds,
                       COALESCE(sum(latency_seconds),0)::double precision AS duration_seconds,
                       COALESCE(sum(COALESCE(cold_start_seconds,0)),0)::double precision AS cold_start_seconds,
                       COALESCE(sum(input_tokens),0)::bigint AS input_tokens,
                       COALESCE(sum(output_tokens),0)::bigint AS output_tokens,
                       count(*) FILTER (
                           WHERE input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                       )::bigint AS token_reported_operations,
                       percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_seconds)
                           ::double precision AS latency_p50_seconds,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_seconds)
                           ::double precision AS latency_p95_seconds,
                       percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_seconds)
                           ::double precision AS latency_p99_seconds
                FROM terminal
                GROUP BY model_id ORDER BY model_id
                """,
                from_at,
                to_at,
            )
            latency = await connection.fetchrow(
                """
                SELECT percentile_cont(0.50) WITHIN GROUP (
                           ORDER BY GREATEST(0,extract(epoch FROM completed_at-accepted_at))
                       )::double precision AS latency_p50_seconds,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY GREATEST(0,extract(epoch FROM completed_at-accepted_at))
                       )::double precision AS latency_p95_seconds,
                       percentile_cont(0.99) WITHIN GROUP (
                           ORDER BY GREATEST(0,extract(epoch FROM completed_at-accepted_at))
                       )::double precision AS latency_p99_seconds
                FROM fs2_operations
                WHERE status IN ('succeeded','failed','cancelled','preempted','expired')
                  AND completed_at >= $1 AND completed_at < $2
                """,
                from_at,
                to_at,
            )
        assert latency is not None
        return AdminUsageWindow(
            from_at=from_at,
            to_at=to_at,
            rows=[AdminUsageRow.model_validate(dict(row)) for row in rows],
            latency_p50_seconds=latency["latency_p50_seconds"],
            latency_p95_seconds=latency["latency_p95_seconds"],
            latency_p99_seconds=latency["latency_p99_seconds"],
        )

    @staticmethod
    def _admin_operation(row: asyncpg.Record) -> AdminOperationRecord:
        return AdminOperationRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            principal_id=row["principal_id"],
            api_key_prefix=row["api_key_prefix"],
            model_id=row["model_id"],
            model_revision=row["model_revision"],
            protocol=row["protocol"],
            operation=row["operation"],
            status=OperationStatus(row["status"]),
            accepted_at=row["accepted_at"],
            activation_started_at=row["activation_started_at"],
            ready_at=row["ready_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            outcome=row["outcome"],
            semantic_outcome=row["semantic_outcome"],
            http_status=row["http_status"],
            error_code=row["error_code"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            gpu_count=row["gpu_count"],
            preemptible=row["preemptible"],
            estimated_gpu_seconds=row["estimated_gpu_seconds"],
            cold_start_seconds=row["cold_start_seconds"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
        )

    async def admin_list_operations(self, query: AdminOperationQuery) -> list[AdminOperationRecord]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT operation.id,operation.tenant_id,operation.principal_id,token.prefix AS api_key_prefix,
                       operation.model_id,operation.model_revision,operation.protocol,operation.operation,
                       operation.status::text AS status,operation.accepted_at,operation.activation_started_at,
                       operation.ready_at,operation.started_at,operation.completed_at,operation.outcome,
                       operation.semantic_outcome,operation.http_status,operation.error_code,operation.attempt,
                       operation.max_attempts,operation.gpu_count,operation.preemptible,
                       operation.estimated_gpu_seconds,operation.cold_start_seconds,
                       operation.input_tokens,operation.output_tokens
                FROM fs2_operations operation
                JOIN fs2_tokens token ON token.id=operation.token_id
                WHERE operation.accepted_at >= $1 AND operation.accepted_at < $2
                  AND ($3::text IS NULL OR operation.tenant_id=$3)
                  AND ($4::timestamptz IS NULL OR
                       (operation.accepted_at,operation.id)<($4::timestamptz,$5::uuid))
                  AND ($6::text IS NULL OR operation.model_id=$6)
                  AND ($7::text IS NULL OR operation.principal_id=$7)
                  AND ($8::text IS NULL OR token.prefix=$8)
                  AND ($9::text IS NULL OR operation.status::text=$9)
                  AND ($10::text IS NULL OR operation.error_code=$10)
                ORDER BY operation.accepted_at DESC,operation.id DESC LIMIT $11
                """,
                query.from_at,
                query.to_at,
                query.tenant_id,
                query.after_at,
                query.after_id,
                query.model_id,
                query.principal_id,
                query.api_key_prefix,
                str(query.status) if query.status is not None else None,
                query.error_code,
                query.limit,
            )
        return [self._admin_operation(row) for row in rows]

    async def admin_get_operation(self, operation_id: UUID, *, tenant_id: str | None = None) -> AdminOperationRecord:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT operation.id,operation.tenant_id,operation.principal_id,token.prefix AS api_key_prefix,
                       operation.model_id,operation.model_revision,operation.protocol,operation.operation,
                       operation.status::text AS status,operation.accepted_at,operation.activation_started_at,
                       operation.ready_at,operation.started_at,operation.completed_at,operation.outcome,
                       operation.semantic_outcome,operation.http_status,operation.error_code,operation.attempt,
                       operation.max_attempts,operation.gpu_count,operation.preemptible,
                       operation.estimated_gpu_seconds,operation.cold_start_seconds,
                       operation.input_tokens,operation.output_tokens
                FROM fs2_operations operation
                JOIN fs2_tokens token ON token.id=operation.token_id
                WHERE operation.id=$1 AND ($2::text IS NULL OR operation.tenant_id=$2)
                """,
                operation_id,
                tenant_id,
            )
        if row is None:
            raise NotFoundError("operation not found")
        return self._admin_operation(row)


class PostgresMaintenanceStore:
    """Credential-minimal retention surface without payload or ledger keys."""

    _token_lock = staticmethod(PostgresStore._token_lock)

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> PostgresMaintenanceStore:
        pool = await PostgresStore._connect_pool(
            database_url,
            min_size=1,
            max_size=2,
            application_name="fs2-serve-maintenance",
        )
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def purge_expired_payloads(self) -> int:
        return cast(int, await PostgresStore.purge_expired_payloads(cast(PostgresStore, self)))

    async def delete_expired_rows(
        self,
        *,
        operation_retention_seconds: int,
        token_retention_seconds: int,
        audit_retention_seconds: int,
        usage_retention_seconds: int,
    ) -> dict[str, int]:
        return cast(
            dict[str, int],
            await PostgresStore.delete_expired_rows(
                cast(PostgresStore, self),
                operation_retention_seconds=operation_retention_seconds,
                token_retention_seconds=token_retention_seconds,
                audit_retention_seconds=audit_retention_seconds,
                usage_retention_seconds=usage_retention_seconds,
            ),
        )

from __future__ import annotations

import ast
import copy
import inspect
import json
import re
import shutil
import textwrap
from pathlib import Path

import pytest
from conftest import CONTROL_ROOT

import fs2_serve.scientific_artifacts as scientific_artifacts
import fs2_serve.scientific_batch.postgres_accounting as scientific_postgres_accounting
from fs2_serve.postgres import SCIENTIFIC_RUNTIME_UPDATE_COLUMNS, PostgresStore
from fs2_serve.postgresql_release import (
    EXPECTED_MIGRATIONS,
    build_postgresql_release_contract,
    build_schema_rollout_prepare_receipt,
    render_postgresql_release_contract,
    validate_migration_set,
    validate_postgresql_release_contract,
    validate_schema_rollout_prepare_receipt,
)
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

MIGRATIONS = CONTROL_ROOT / "migrations"
CONTRACT = CONTROL_ROOT / "contracts" / "postgresql-release-contract.json"


def test_committed_postgresql_contract_is_exact_emitted_release_receipt_input() -> None:
    committed_bytes = CONTRACT.read_bytes()
    assert committed_bytes == render_postgresql_release_contract(MIGRATIONS)
    committed = json.loads(committed_bytes)
    assert validate_postgresql_release_contract(committed, MIGRATIONS) == committed
    assert PostgresStore._migration_manifest(MIGRATIONS) == validate_migration_set(MIGRATIONS)

    receipt = committed["required_release_receipt_inputs"]
    assert receipt == {
        "first_migration_version": "0001_initial.sql",
        "last_migration_version": "0031_scientific_quota_fencing.sql",
        "migration_count": 31,
        "migration_set_sha256": "1bc495f57002004a6032b8a9b209c4563b2e9e788698f21c60550cded947a7bd",
        "namespace_role_ownership_sha256": "cb7c4b131acfc613c49fc0504dbd5ae9cfe3c3904aec55d1b5ff61ceb35d7580",
    }
    migrations = committed["migration_set"]["ordered_migrations"]
    assert len(migrations) == receipt["migration_count"]
    assert migrations[0]["version"] == receipt["first_migration_version"]
    assert migrations[-1]["version"] == receipt["last_migration_version"]
    assert [migration["ordinal"] for migration in migrations] == list(range(1, receipt["migration_count"] + 1))


def test_schema_rollout_receipt_binds_the_prepared_image_and_one_way_contract_phase() -> None:
    image = "registry.nebius.cloud/unit/control-plane@sha256:" + "7" * 64
    receipt = build_schema_rollout_prepare_receipt(MIGRATIONS, image)
    assert validate_schema_rollout_prepare_receipt(receipt, MIGRATIONS, image) == receipt
    assert receipt["new_multipart_sessions_gate_supported"] is True
    assert receipt["migration_set_sha256"] == build_postgresql_release_contract(MIGRATIONS)["migration_set"][
        "sha256"
    ]

    changed = copy.deepcopy(receipt)
    changed["image_ref"] = "registry.nebius.cloud/unit/control-plane@sha256:" + "8" * 64
    with pytest.raises(RuntimeError, match="prepare receipt"):
        validate_schema_rollout_prepare_receipt(changed, MIGRATIONS, image)

    migration_source = inspect.getsource(PostgresStore._apply_migrations)
    contract_preflight_source = inspect.getsource(PostgresStore._assert_contract_schema_preapplied)
    assert "preserve_predecessor_artifact_authority" in migration_source
    assert "the PostgreSQL contract phase cannot return to expanded" in migration_source
    assert "GRANT UPDATE (artifact_id,finalized_at)" in migration_source
    assert "SET phase='contracted'" in migration_source
    assert migration_source.index("await cls._assert_contract_schema_preapplied") < migration_source.index(
        "CREATE TABLE IF NOT EXISTS fs2_schema_migrations"
    )
    assert "applied != expected" in contract_preflight_source
    assert "recorded_steps" in contract_preflight_source
    assert "every expand migration step" in contract_preflight_source
    assert "missing_unfinished_upload_sessions=0" in contract_preflight_source


def test_scientific_runtime_grant_repairs_are_additive_and_readiness_checked() -> None:
    base_sql = (MIGRATIONS / "0021_scientific_admission_outbox_runtime_grant.sql").read_text(encoding="utf-8")
    base_normalized = " ".join(base_sql.split())
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime')" in base_normalized
    assert (
        "GRANT SELECT, INSERT, DELETE ON TABLE fs2_scientific_admission_outbox TO fs2_serve_runtime" in base_normalized
    )
    lock_sql = (MIGRATIONS / "0022_scientific_admission_outbox_lock_privilege.sql").read_text(encoding="utf-8")
    lock_normalized = " ".join(lock_sql.split())
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime')" in lock_normalized
    assert "GRANT UPDATE ON TABLE fs2_scientific_admission_outbox TO fs2_serve_runtime" in lock_normalized
    batch_sql = (MIGRATIONS / "0023_scientific_batch_scheduling_digest_privilege.sql").read_text(encoding="utf-8")
    batch_normalized = " ".join(batch_sql.split())
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fs2_serve_runtime')" in batch_normalized
    assert "GRANT UPDATE (scheduling_digest) ON TABLE fs2_scientific_batches TO fs2_serve_runtime" in batch_normalized

    wait_source = inspect.getsource(PostgresStore.wait_for_schema)
    assert "has_table_privilege('fs2_serve_runtime'" in wait_source
    assert "has_table_privilege(current_user" in wait_source
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert wait_source.count(f"fs2_scientific_admission_outbox','{privilege}'") == 2
    assert wait_source.count("fs2_scientific_batches','scheduling_digest','UPDATE'") == 2
    assert wait_source.count("fs2_scientific_artifact_quota_reservations','SELECT'") == 2
    assert wait_source.count("fs2_scientific_artifact_quota_reservations','INSERT'") == 2
    assert wait_source.count("fs2_scientific_artifact_quota_reservations','expires_at','UPDATE'") == 2
    assert wait_source.count("fs2_scientific_artifact_quota_reservations','state','UPDATE'") == 2
    assert wait_source.count("fs2_scientific_artifact_quota_events','INSERT'") == 2
    assert wait_source.count("fs2_scientific_artifact_removal_evidence','SELECT'") == 2
    assert wait_source.count("fs2_scientific_artifact_removal_evidence','INSERT'") == 2
    assert wait_source.count("fs2_scientific_gpu_settlements','SELECT'") == 2
    assert wait_source.count("fs2_scientific_gpu_settlements','INSERT'") == 2
    assert "GRANT SELECT,INSERT ON fs2_scientific_gpu_settlements" not in wait_source
    assert "fs2_serve_artifact_verifier" in wait_source
    assert "fs2_scientific_claim_artifact_verifications_v2(integer)" in wait_source
    assert "fs2_scientific_claim_artifact_removals_v2(integer,uuid,text)" in wait_source
    assert "fs2_scientific_record_upload_capability_v2" in wait_source
    assert "fs2_scientific_record_artifact_removal_v2" in wait_source
    assert "fs2_scientific_record_legacy_artifact_version_scan_v2" in wait_source
    assert "fs2_scientific_record_upload_session_aborted_v2" in wait_source
    assert "NOT has_function_privilege('fs2_serve_runtime'" in wait_source
    assert "database schema runtime privileges are incomplete" in wait_source


def test_scientific_quota_migration_retains_provenance_and_bounds_settlement() -> None:
    quota_sql = (MIGRATIONS / "0030_scientific_quota_settlement.sql").read_text(encoding="utf-8")
    normalized = " ".join(quota_sql.split())
    assert "fs2_scientific_artifact_quota_reservations" in normalized
    assert "fs2_scientific_artifact_quota_events" in normalized
    assert "fs2_scientific_artifact_removal_evidence" in normalized
    assert "reserved_objects integer NOT NULL DEFAULT 1 CHECK (reserved_objects = 1)" in normalized
    assert (
        "event_type text NOT NULL CHECK "
        "(event_type IN ('reserved','retention_extended','removal_claimed','released'))"
    ) in normalized
    assert "CREATE TRIGGER fs2_scientific_artifact_quota_events_immutable" in normalized
    assert "CREATE TRIGGER fs2_scientific_artifact_removal_evidence_immutable" in normalized
    assert "CREATE TRIGGER fs2_scientific_gpu_settlements_immutable" in normalized
    assert "CREATE TRIGGER fs2_scientific_operations_settlement_guard" in normalized
    assert "charged_gpu_seconds <= reserved_gpu_seconds" in normalized
    assert "'active',reserved_at,expires_at,NULL,NULL" in normalized
    assert "SET state='released',released_at=p_observed_at,release_reason='provider_removed'" in normalized
    assert "p_evidence_kind<>'absence_confirmed'" in normalized
    assert "p_removed_version_count<>0" in normalized
    assert "p_observed_at<reservation.verification_claimed_at" in normalized
    assert "NEW.release_reason<>'provider_removed'" in normalized
    assert "artifact quota release lacks exact provider evidence" in normalized
    assert normalized.count("row_number() OVER ( PARTITION BY reservation.tenant_id") == 2
    assert "ORDER BY tenant_rank,expires_at,tenant_id,upload_id LIMIT p_limit" in normalized
    assert normalized.count("LEAST(3600.0,30.0*power(2.0") == 2
    assert "evidence_kind text NOT NULL CHECK (evidence_kind='absence_confirmed')" in normalized
    assert "current_setting('fs2.scientific_settlement_operation',true)" in normalized
    assert "terminal scientific operation is immutable" in normalized
    assert "GRANT UPDATE ON fs2_tokens" not in normalized
    assert "GRANT DELETE ON fs2_operations" not in normalized
    assert "operation_id uuid PRIMARY KEY REFERENCES" not in normalized
    assert "token_id uuid NOT NULL REFERENCES" not in normalized

    fencing_sql = (MIGRATIONS / "0031_scientific_quota_fencing.sql").read_text(encoding="utf-8")
    fencing = " ".join(fencing_sql.split())
    assert "latest_upload_capability_expires_at" in fencing
    assert "fs2_scientific_artifact_upload_capabilities" in fencing
    assert "fs2_scientific_artifact_deletion_evidence_v2" in fencing
    assert "fs2_scientific_artifact_removal_evidence_v2" in fencing
    assert "fs2_scientific_publish_artifact_v2" in fencing
    assert "fs2_scientific_record_legacy_artifact_version_scan_v2" in fencing
    assert "fs2_scientific_artifact_legacy_version_scan_events" in fencing
    assert fencing.count("FOR UPDATE OF janitor_cursor SKIP LOCKED") == 2
    assert fencing.count("FOR UPDATE OF reservation SKIP LOCKED") == 2
    assert fencing.count("WHILE claimed<p_limit LOOP") == 2
    assert "pg_advisory_xact_lock" not in fencing
    assert "REFERENCES fs2_scientific_uploads" not in fencing
    assert "REFERENCES fs2_scientific_artifacts" not in fencing
    assert "DEFAULT (CURRENT_TIMESTAMP+interval '15 minutes')" in fencing
    assert "SET DEFAULT (statement_timestamp()+interval '15 minutes')" in fencing
    assert "UPDATE fs2_scientific_artifact_quota_reservations SET latest_upload" not in fencing
    assert "p_second_version_set_digest<>p_first_version_set_digest" in fencing
    assert "reservation.latest_upload_capability_expires_at<>p_latest_upload_capability_expires_at" in fencing


def test_schema_bridge_receipt_has_authoritative_drain_catchups_and_retry_contract() -> None:
    sql = (MIGRATIONS / "0031_scientific_quota_fencing.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.split())
    postgres_source = inspect.getsource(PostgresStore._apply_migrations)

    assert "missing_unfinished_upload_sessions bigint" in sql
    assert sql.count(
        "INSERT INTO fs2_scientific_artifact_upload_sessions("
    ) >= 2
    assert sql.count(
        "INSERT INTO fs2_scientific_artifact_legacy_version_claims(artifact_id)"
    ) >= 2
    assert sql.index("CREATE TRIGGER fs2_scientific_quota_queue_legacy_upload_session") < sql.index(
        "-- fs2-migration-transaction-boundary",
        sql.index("CREATE TRIGGER fs2_scientific_quota_queue_legacy_upload_session"),
    ) < sql.index(
        "-- CREATE TRIGGER waits for predecessor writers",
    )
    assert sql.index("CREATE TRIGGER fs2_scientific_artifacts_queue_legacy_version") < sql.index(
        "-- fs2-migration-transaction-boundary",
        sql.index("CREATE TRIGGER fs2_scientific_artifacts_queue_legacy_version"),
    ) < sql.index("-- Close the analogous predecessor INSERT window")
    assert "fs2_schema_bridge_rollout_attempts" in sql
    assert "deployment_uid text NOT NULL" in sql
    assert "runtime_pod_set_digest char(64) NOT NULL" in sql
    assert "kubernetes_audit_id text NOT NULL" in sql
    assert "missing_unfinished_upload_sessions bigint NOT NULL CHECK" in normalized
    assert "p_deployment_observed_generation<>p_deployment_generation" in normalized
    assert "p_runtime_pod_count<>p_deployment_desired_replicas" in normalized
    assert "rollout.predecessor_image_ref IS NOT NULL" in normalized
    assert "rollout.phase<>'contracted'" in normalized
    assert "rollout.phase IN ('expanded','contracted')" in postgres_source
    assert "JOIN fs2_schema_bridge_rollout_attempts attempt" in postgres_source
    assert "bridge_release_revision=COALESCE(bridge_release_revision,$2)" in postgres_source
    assert "GREATEST(COALESCE(bridge_release_revision" not in postgres_source


def _updated_columns(source: str, table: str) -> set[str]:
    statements = re.findall(
        rf"\bUPDATE\s+{re.escape(table)}(?:\s+[a-z_][a-z0-9_]*)?\s+SET\s+(.*?)"
        rf"(?=\s+(?:FROM|WHERE|RETURNING)\b)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return {
        column.lower()
        for statement in statements
        for column in re.findall(r"(?:^|,)\s*([a-z_][a-z0-9_]*)\s*=", statement, flags=re.IGNORECASE)
    }


def _sql_literals(source: str) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_scientific_runtime_update_grants_cover_every_repository_statement() -> None:
    """Fail closed when repository SQL grows beyond its restricted-role ACL."""

    batch_source = inspect.getsource(PostgresScientificBatchRepository)
    settlement_source = inspect.getsource(scientific_postgres_accounting)
    artifact_source = inspect.getsource(scientific_artifacts)
    # Provider version publication is confined to the checked SECURITY
    # DEFINER CAS; the runtime repository must never regain a direct upload
    # row update merely to make finalization work.
    assert _updated_columns(artifact_source, "fs2_scientific_uploads") == set()
    assert _updated_columns(artifact_source, "fs2_scientific_artifact_quota_reservations") == set()
    actual = {
        "fs2_scientific_stage_attempts": _updated_columns(artifact_source, "fs2_scientific_stage_attempts"),
        "fs2_scientific_batches": (
            _updated_columns(batch_source, "fs2_scientific_batches")
            | _updated_columns(settlement_source, "fs2_scientific_batches")
        ),
    }
    assert actual == {table: set(columns) for table, columns in SCIENTIFIC_RUNTIME_UPDATE_COLUMNS.items()}

    # PostgreSQL row-locking reads require UPDATE privilege. These are all the
    # restricted scientific tables read with FOR UPDATE/FOR SHARE; each is in
    # the audited column-grant map. The outbox has a dedicated table-level
    # grant because it is also deleted after materialization.
    locked_scientific_tables = {
        match.lower()
        for source in (batch_source, settlement_source, artifact_source)
        for literal in _sql_literals(source)
        for match in re.findall(
            r"\bFROM\s+(fs2_scientific_[a-z0-9_]+)[^;]*?\bFOR\s+(?:UPDATE|SHARE)\b",
            literal,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    assert locked_scientific_tables - {"fs2_scientific_gpu_settlements"} == set(
        SCIENTIFIC_RUNTIME_UPDATE_COLUMNS
    )
    assert "FOR SHARE" in inspect.getsource(PostgresStore._stage_scientific_admission)


@pytest.mark.parametrize("mutation", ["missing", "extra", "renamed", "changed", "symlink"])
def test_migration_set_rejects_missing_extra_reordered_or_changed_files(tmp_path: Path, mutation: str) -> None:
    candidate = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, candidate)
    if mutation == "missing":
        (candidate / EXPECTED_MIGRATIONS[3][0]).unlink()
    elif mutation == "extra":
        (candidate / "0009_unreviewed.sql").write_text("SELECT 1;\n", encoding="utf-8")
    elif mutation == "renamed":
        (candidate / EXPECTED_MIGRATIONS[5][0]).rename(candidate / "0009_activation_controller.sql")
    elif mutation == "changed":
        (candidate / EXPECTED_MIGRATIONS[-1][0]).write_bytes(
            (candidate / EXPECTED_MIGRATIONS[-1][0]).read_bytes() + b"\n"
        )
    else:
        target = candidate / EXPECTED_MIGRATIONS[-1][0]
        payload = tmp_path / "migration.sql"
        payload.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(payload)
    with pytest.raises(RuntimeError, match="migration"):
        validate_migration_set(candidate)


def test_contract_rejects_reordered_missing_extra_and_namespace_substitution() -> None:
    exact = build_postgresql_release_contract(MIGRATIONS)
    candidates = []

    reordered = copy.deepcopy(exact)
    ordered = reordered["migration_set"]["ordered_migrations"]
    ordered[6], ordered[7] = ordered[7], ordered[6]
    candidates.append(reordered)

    missing = copy.deepcopy(exact)
    missing["migration_set"]["ordered_migrations"].pop()
    candidates.append(missing)

    extra = copy.deepcopy(exact)
    extra["migration_set"]["ordered_migrations"].append(
        {"ordinal": 11, "version": "0011_unreviewed.sql", "sha256": "0" * 64}
    )
    candidates.append(extra)

    wrong_namespace = copy.deepcopy(exact)
    wrong_namespace["namespace_role_ownership"]["database"]["namespace"] = "fs2-system"
    candidates.append(wrong_namespace)

    wrong_secret_namespace = copy.deepcopy(exact)
    wrong_secret_namespace["namespace_role_ownership"]["credential_secrets"][0]["namespace"] = "fs2-data"
    candidates.append(wrong_secret_namespace)

    for candidate in candidates:
        with pytest.raises(RuntimeError, match="missing, extra, reordered, or hash-mismatched"):
            validate_postgresql_release_contract(candidate, MIGRATIONS)


def test_namespace_secret_and_role_ownership_is_one_closed_cross_lane_contract() -> None:
    ownership = build_postgresql_release_contract(MIGRATIONS)["namespace_role_ownership"]
    assert ownership["database"] == {
        "namespace": "fs2-data",
        "cluster_name": "fs2-control-db",
        "read_write_service_name": "fs2-control-db-rw",
        "port": 5432,
        "database_name": "fs2serve",
        "database_owner_role": "fs2serve",
        "resource_owner": "postgresql-platform-release",
    }
    secrets = {secret["purpose"]: secret for secret in ownership["credential_secrets"]}
    assert {purpose: (value["namespace"], value["name"], value["key"]) for purpose, value in secrets.items()} == {
        "activation": ("fs2-system", "fs2-serve-database-activation", "url"),
        "artifact-removal": ("fs2-system", "fs2-serve-database-artifact-remover", "url"),
        "artifact-verification": ("fs2-system", "fs2-serve-database-artifact-verifier", "url"),
        "maintenance": ("fs2-system", "fs2-serve-database-maintenance", "url"),
        "migrations": ("fs2-system", "fs2-serve-database-migrations", "url"),
        "reporting": ("fs2-observability", "fs2-serve-database-reporting", "url"),
        "runtime": ("fs2-system", "fs2-serve-database", "url"),
    }
    assert {role["name"] for role in ownership["database_group_roles"]} == {
        "fs2_serve_activation",
        "fs2_serve_artifact_remover",
        "fs2_serve_artifact_verifier",
        "fs2_serve_maintenance",
        "fs2_serve_reporting",
        "fs2_serve_runtime",
    }
    assert all(not role["login"] for role in ownership["database_group_roles"])
    assert secrets["runtime"]["consumer_owners"] == ["fs2-serve-control-plane-gateway"]
    assert secrets["maintenance"]["consumer_owners"] == ["fs2-serve-control-plane-maintenance"]
    assert ownership["schema_migration_owner"]["ownership"] == "sole-ddl-and-grant-owner"

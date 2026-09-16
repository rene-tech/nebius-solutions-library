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
from fs2_serve.postgres import (
    RETENTION_ELIGIBLE_CANDIDATES_SQL,
    RETENTION_EXPIRY_SCAN_SQL,
    RETENTION_REVOKED_SCAN_SQL,
    SCIENTIFIC_RUNTIME_UPDATE_COLUMNS,
    PostgresStore,
)
from fs2_serve.postgresql_release import (
    EXPECTED_MIGRATIONS,
    build_postgresql_release_contract,
    render_postgresql_release_contract,
    validate_migration_set,
    validate_postgresql_release_contract,
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
        "last_migration_version": "0036_scientific_admission_complete_binding.sql",
        "migration_count": 36,
        "migration_set_sha256": "8ff1bba38cb5a3ec388c94d0f00c3d2017bc39e7532fec41a338702892659cf7",
        "namespace_role_ownership_sha256": "47397ccc7c42612a11c568101f67ccd7a3446899b2ede5af3bf3bd926aa111ca",
    }
    migrations = committed["migration_set"]["ordered_migrations"]
    assert len(migrations) == receipt["migration_count"]
    assert migrations[0]["version"] == receipt["first_migration_version"]
    assert migrations[-1]["version"] == receipt["last_migration_version"]
    assert [migration["ordinal"] for migration in migrations] == list(range(1, receipt["migration_count"] + 1))


def test_scientific_runtime_grants_converge_to_trigger_bound_completion() -> None:
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
    completion_sql = (MIGRATIONS / "0032_scientific_admission_completion.sql").read_text(encoding="utf-8")
    completion_normalized = " ".join(completion_sql.split())
    assert "SECURITY DEFINER SET search_path = pg_catalog, public" in completion_normalized
    assert "AFTER INSERT ON fs2_scientific_batches" in completion_normalized
    assert (
        "REVOKE UPDATE,DELETE ON TABLE fs2_scientific_admission_outbox FROM fs2_serve_runtime" in completion_normalized
    )
    assert "REVOKE ALL ON FUNCTION fs2_scientific_consume_admission_outbox() FROM PUBLIC" in completion_normalized

    digest_sql = (MIGRATIONS / "0035_scientific_admission_digest.sql").read_text(encoding="utf-8")
    digest_normalized = " ".join(digest_sql.split())
    assert "ADD COLUMN scheduling_digest char(71)" in digest_normalized
    assert "scheduling_digest IS NULL OR scheduling_digest ~ '^sha256:[0-9a-f]{64}$'" in digest_normalized

    binding_sql = (MIGRATIONS / "0036_scientific_admission_complete_binding.sql").read_text(encoding="utf-8")
    binding_normalized = " ".join(binding_sql.split())
    assert "ALTER COLUMN scheduling_digest SET NOT NULL" in binding_normalized
    assert "NEW.state IS DISTINCT FROM frozen_payload" in binding_normalized
    assert "NEW.scheduling_digest IS DISTINCT FROM frozen_scheduling_digest" in binding_normalized
    assert "NEW.status <> 'queued'" in binding_normalized
    assert "NEW.revision <> 0" in binding_normalized
    assert "NEW.cancel_requested" in binding_normalized
    assert "NEW.controller_id IS NOT NULL" in binding_normalized
    assert "NEW.fencing_token <> 0" in binding_normalized
    assert "NEW.lease_expires_at IS NOT NULL" in binding_normalized
    assert "RAISE EXCEPTION USING ERRCODE='FS204'" in binding_normalized

    wait_source = inspect.getsource(PostgresStore.wait_for_schema)
    assert "has_table_privilege('fs2_serve_runtime'" in wait_source
    assert "has_table_privilege(current_user" in wait_source
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert wait_source.count(f"fs2_scientific_admission_outbox','{privilege}'") == 2
    assert wait_source.count("NOT has_table_privilege") >= 4
    assert wait_source.count("NOT has_function_privilege") == 2
    assert "fs2_scientific_consume_admission_outbox_trigger" in wait_source
    assert "attname='scheduling_digest' AND attnotnull" in wait_source
    assert wait_source.count("fs2_scientific_batches','scheduling_digest','UPDATE'") == 2
    assert "SELECT,INSERT" not in wait_source
    assert "database schema runtime privileges are incomplete" in wait_source


def test_retention_scan_hardening_is_versioned_and_future_functions_fail_closed() -> None:
    source = (MIGRATIONS / "0031_retention_scan_hardening.sql").read_text(encoding="utf-8")
    normalized = " ".join(source.split())
    for index in (
        "fs2_operations_retention_idx",
        "fs2_tokens_revoked_retention_idx",
        "fs2_tokens_expiry_retention_idx",
        "fs2_audit_retention_idx",
        "fs2_request_telemetry_retention_idx",
        "fs2_scientific_stage_attempts_retention_idx",
        "fs2_scientific_artifacts_retention_idx",
    ):
        assert f"CREATE INDEX {index}" in normalized
    assert "ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC" in normalized

    successor = (MIGRATIONS / "0033_retention_privilege_and_token_scan.sql").read_text(encoding="utf-8")
    successor_normalized = " ".join(successor.split())
    assert "CREATE INDEX fs2_operations_token_retention_idx ON fs2_operations (token_id)" in successor_normalized
    for object_class in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        assert f"ALTER DEFAULT PRIVILEGES REVOKE ALL ON {object_class} FROM PUBLIC" in successor_normalized
        assert f"ALTER DEFAULT PRIVILEGES REVOKE ALL ON {object_class} FROM %I" in successor_normalized
        assert (
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {object_class} FROM PUBLIC"
            in successor_normalized
        )
        assert f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON {object_class} FROM %I" in successor_normalized


def test_token_retention_progress_is_persistent_bounded_and_maintenance_only() -> None:
    migration = (MIGRATIONS / "0034_token_retention_scan_progress.sql").read_text(encoding="utf-8")
    normalized = " ".join(migration.split())
    assert "CREATE TABLE fs2_retention_scan_cursors" in normalized
    assert "stream IN ('tokens_revoked','tokens_expiry')" in normalized
    assert "REVOKE ALL ON TABLE fs2_retention_scan_cursors FROM PUBLIC" in normalized
    assert "REVOKE ALL ON TABLE fs2_retention_scan_cursors FROM %I" in normalized

    store_source = inspect.getsource(PostgresStore)
    for query_name in (
        "RETENTION_REVOKED_SCAN_SQL",
        "RETENTION_EXPIRY_SCAN_SQL",
        "RETENTION_ELIGIBLE_CANDIDATES_SQL",
    ):
        assert query_name in store_source
    assert "FOR UPDATE" in inspect.getsource(PostgresStore._advance_token_retention_stream)
    assert "position_at=NULL,position_id=NULL" in inspect.getsource(PostgresStore._advance_token_retention_stream)
    assert "LIMIT $2" in RETENTION_REVOKED_SCAN_SQL
    assert "LIMIT $2" in RETENTION_EXPIRY_SCAN_SQL
    assert "LIMIT $2" in RETENTION_ELIGIBLE_CANDIDATES_SQL


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
    artifact_source = inspect.getsource(scientific_artifacts)
    actual = {
        "fs2_scientific_stage_attempts": _updated_columns(artifact_source, "fs2_scientific_stage_attempts"),
        "fs2_scientific_uploads": _updated_columns(artifact_source, "fs2_scientific_uploads"),
        "fs2_scientific_batches": _updated_columns(batch_source, "fs2_scientific_batches"),
    }
    assert actual == {table: set(columns) for table, columns in SCIENTIFIC_RUNTIME_UPDATE_COLUMNS.items()}

    # PostgreSQL row-locking reads require UPDATE privilege. These are all the
    # restricted scientific tables read with FOR UPDATE/FOR SHARE; each is in
    # the audited column-grant map. Admission outbox recovery uses plain reads
    # and therefore needs neither UPDATE nor DELETE.
    locked_scientific_tables = {
        match.lower()
        for source in (batch_source, artifact_source)
        for literal in _sql_literals(source)
        for match in re.findall(
            r"\bFROM\s+(fs2_scientific_[a-z0-9_]+)[^;]*?\bFOR\s+(?:UPDATE|SHARE)\b",
            literal,
            flags=re.IGNORECASE | re.DOTALL,
        )
    }
    assert locked_scientific_tables == {
        *SCIENTIFIC_RUNTIME_UPDATE_COLUMNS,
        # Retention uses the same module but a distinct maintenance-only pool.
        "fs2_scientific_retention_claims",
    }
    assert "FOR SHARE" not in inspect.getsource(PostgresStore._stage_scientific_admission)


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
        "maintenance": ("fs2-system", "fs2-serve-database-maintenance", "url"),
        "migrations": ("fs2-system", "fs2-serve-database-migrations", "url"),
        "reporting": ("fs2-observability", "fs2-serve-database-reporting", "url"),
        "runtime": ("fs2-system", "fs2-serve-database", "url"),
    }
    assert {role["name"] for role in ownership["database_group_roles"]} == {
        "fs2_serve_activation",
        "fs2_serve_maintenance",
        "fs2_serve_reporting",
        "fs2_serve_runtime",
    }
    assert all(not role["login"] for role in ownership["database_group_roles"])
    assert secrets["runtime"]["consumer_owners"] == ["fs2-serve-control-plane-gateway"]
    assert secrets["maintenance"]["consumer_owners"] == ["fs2-serve-control-plane-maintenance"]
    assert ownership["schema_migration_owner"]["ownership"] == "sole-ddl-and-grant-owner"

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
from fs2_serve.postgres import SCIENTIFIC_RUNTIME_UPDATE_COLUMNS, PostgresStore
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
        "last_migration_version": "0023_scientific_batch_scheduling_digest_privilege.sql",
        "migration_count": 23,
        "migration_set_sha256": "1459b5ce45c9de22301c5d0ada0cfed8527e7f802a58d607e4078afcb62fda66",
        "namespace_role_ownership_sha256": "47397ccc7c42612a11c568101f67ccd7a3446899b2ede5af3bf3bd926aa111ca",
    }
    migrations = committed["migration_set"]["ordered_migrations"]
    assert len(migrations) == receipt["migration_count"]
    assert migrations[0]["version"] == receipt["first_migration_version"]
    assert migrations[-1]["version"] == receipt["last_migration_version"]
    assert [migration["ordinal"] for migration in migrations] == list(range(1, receipt["migration_count"] + 1))


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
    assert "SELECT,INSERT" not in wait_source
    assert "database schema runtime privileges are incomplete" in wait_source


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
    # the audited column-grant map. The outbox has a dedicated table-level
    # grant because it is also deleted after materialization.
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
    assert locked_scientific_tables == set(SCIENTIFIC_RUNTIME_UPDATE_COLUMNS)
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

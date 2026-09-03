from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
from conftest import CONTROL_ROOT

from fs2_serve.postgres import PostgresStore
from fs2_serve.postgresql_release import (
    EXPECTED_MIGRATIONS,
    build_postgresql_release_contract,
    render_postgresql_release_contract,
    validate_migration_set,
    validate_postgresql_release_contract,
)

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
        "last_migration_version": "0019_scientific_deployment_authorization.sql",
        "migration_count": 19,
        "migration_set_sha256": "97d71b58d235015eb5e58901caa696f8e09c863682d5c3c48a3874bfec2520d7",
        "namespace_role_ownership_sha256": "47397ccc7c42612a11c568101f67ccd7a3446899b2ede5af3bf3bd926aa111ca",
    }
    migrations = committed["migration_set"]["ordered_migrations"]
    assert len(migrations) == receipt["migration_count"]
    assert migrations[0]["version"] == receipt["first_migration_version"]
    assert migrations[-1]["version"] == receipt["last_migration_version"]
    assert [migration["ordinal"] for migration in migrations] == list(range(1, receipt["migration_count"] + 1))


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

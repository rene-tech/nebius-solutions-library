"""Exact non-secret PostgreSQL release inputs shared across fs2-serve lanes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

SCHEMA: Final = "fs2-serve.nebius.ai/postgresql-release-contract/v1"
CANONICALIZATION: Final = "utf8-json-sort-keys-no-whitespace"

EXPECTED_MIGRATIONS: Final = (
    ("0001_initial.sql", "fa1ece4a99ef5d5b8f9260d120ec452bd64fedfe581a376faee69013020c0179"),
    ("0002_bound_operation_attempts.sql", "3b7e40e6ff290ff348e36b86a5d0310192b538b2cfce301d137ad6aac17ca428"),
    ("0003_bound_operation_identifiers.sql", "7f6d4d7de60efee4928ebd0274d2a4c7410aee2316604249992647c0c65bfe0c"),
    ("0004_index_queued_deadlines.sql", "2cb1e55877d36c953d003028b9db06491bcc524bb019e2abac16070daf0f6e95"),
    ("0005_terminal_accounting.sql", "fedb6789a4839d42645c5ffb6905ce46525c213d81f15d9d987eacc109614197"),
    ("0006_activation_controller.sql", "ac15d435e5fefb03da2780011e059736da803e5ded482414a5d1012ee265b022"),
    (
        "0007_activation_controller_health.sql",
        "b60a2976b366acc55c652f2e1cbdc4a06f2a38933930aa8d2805b48e063e150d",
    ),
    (
        "0008_activation_fencing_identity.sql",
        "b7ea9fe6497df00fa4a0d4c6bfb2b01dfa3e699f7b4392979b1d24f13a8fcd3d",
    ),
    (
        "0009_maintenance_least_privilege.sql",
        "3bf0342f7b9ef8b5dc1e88aeda985d6d8856f5cf111b82aded3d0c56e44d0c23",
    ),
    (
        "0010_admin_access_accounting.sql",
        "113d7ff18906fd7af94f14e8751c6d9480eba25b711440b528ff7dde9157c5e5",
    ),
    (
        "0011_admin_configuration.sql",
        "fa8ab57dcf32bba741c149352e796cb261341df535d0b972af432792bbd8da43",
    ),
    (
        "0012_model_deployments.sql",
        "bf4dfbff463a88f3be1cc04e452900d4eff9c18024161069d7beb281229f3eef",
    ),
    (
        "0013_durable_dynamic_dispatch.sql",
        "4daf1a47abd864c04f30dc48149a0c74b46aac1332c12ef40df518b2dea8b9ad",
    ),
    (
        "0014_scientific_artifact_results.sql",
        "88239e15fef20d10c515611d6c9336364a06d9752c475401de56af890c74ec4f",
    ),
    (
        "0015_scientific_batch_controller.sql",
        "21dc337408082d9b5d8f1584b66c2cb2fa8186d6c48716d09d340b1814d1bc5d",
    ),
)

NAMESPACE_ROLE_OWNERSHIP: Final[dict[str, Any]] = {
    "database": {
        "namespace": "fs2-data",
        "cluster_name": "fs2-control-db",
        "read_write_service_name": "fs2-control-db-rw",
        "port": 5432,
        "database_name": "fs2serve",
        "database_owner_role": "fs2serve",
        "resource_owner": "postgresql-platform-release",
    },
    "schema_migration_owner": {
        "namespace": "fs2-system",
        "job_name": "fs2-serve-control-plane-migrate",
        "service_account_name": "fs2-serve-control-plane-migration",
        "database_role": "fs2serve",
        "ownership": "sole-ddl-and-grant-owner",
    },
    "credential_secrets": [
        {
            "purpose": "activation",
            "namespace": "fs2-system",
            "name": "fs2-serve-database-activation",
            "key": "url",
            "database_group_role": "fs2_serve_activation",
            "writer_owner": "postgresql-platform-release",
            "consumer_owners": ["fs2-model-activation-controller"],
        },
        {
            "purpose": "maintenance",
            "namespace": "fs2-system",
            "name": "fs2-serve-database-maintenance",
            "key": "url",
            "database_group_role": "fs2_serve_maintenance",
            "writer_owner": "postgresql-platform-release",
            "consumer_owners": ["fs2-serve-control-plane-maintenance"],
        },
        {
            "purpose": "migrations",
            "namespace": "fs2-system",
            "name": "fs2-serve-database-migrations",
            "key": "url",
            "database_group_role": None,
            "database_owner_role": "fs2serve",
            "writer_owner": "postgresql-platform-release",
            "consumer_owners": ["fs2-serve-control-plane-migration"],
        },
        {
            "purpose": "reporting",
            "namespace": "fs2-observability",
            "name": "fs2-serve-database-reporting",
            "key": "url",
            "database_group_role": "fs2_serve_reporting",
            "writer_owner": "postgresql-platform-release",
            "consumer_owners": ["fs2-observability-grafana"],
        },
        {
            "purpose": "runtime",
            "namespace": "fs2-system",
            "name": "fs2-serve-database",
            "key": "url",
            "database_group_role": "fs2_serve_runtime",
            "writer_owner": "postgresql-platform-release",
            "consumer_owners": ["fs2-serve-control-plane-gateway"],
        },
    ],
    "database_group_roles": [
        {
            "purpose": "activation",
            "name": "fs2_serve_activation",
            "login": False,
            "creation_and_grant_owner": "fs2-serve-control-plane-migration",
        },
        {
            "purpose": "maintenance",
            "name": "fs2_serve_maintenance",
            "login": False,
            "creation_and_grant_owner": "fs2-serve-control-plane-migration",
        },
        {
            "purpose": "reporting",
            "name": "fs2_serve_reporting",
            "login": False,
            "creation_and_grant_owner": "fs2-serve-control-plane-migration",
        },
        {
            "purpose": "runtime",
            "name": "fs2_serve_runtime",
            "login": False,
            "creation_and_grant_owner": "fs2-serve-control-plane-migration",
        },
    ],
    "ownership_rules": [
        "postgresql-platform-release-owns-cluster-database-owner-and-secret-writes",
        "fs2-serve-control-plane-migration-is-sole-schema-and-group-grant-owner",
        "application-and-observability-workloads-read-only-their-namespaced-secret",
        "credential-login-members-are-distinct-and-receipt-bound-outside-this-value-suppressed-contract",
    ],
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def validate_migration_set(migrations_dir: Path) -> list[tuple[Path, str]]:
    """Require the one ordered immutable migration set; reject substitutions."""

    if not migrations_dir.is_dir() or migrations_dir.is_symlink():
        raise RuntimeError("migration directory is unavailable or is a symlink")
    paths = sorted(path for path in migrations_dir.iterdir() if path.suffix == ".sql")
    expected_names = [name for name, _ in EXPECTED_MIGRATIONS]
    actual_names = [path.name for path in paths]
    if actual_names != expected_names:
        raise RuntimeError("migration set is missing, extra, or reordered")
    manifest: list[tuple[Path, str]] = []
    for path, (_, expected_digest) in zip(paths, EXPECTED_MIGRATIONS, strict=True):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"migration is not one regular file: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_digest:
            raise RuntimeError(f"migration hash differs from the release contract: {path.name}")
        manifest.append((path, digest))
    return manifest


def build_postgresql_release_contract(migrations_dir: Path) -> dict[str, Any]:
    """Build the deterministic receipt input after verifying every SQL byte."""

    manifest = validate_migration_set(migrations_dir)
    ordered_migrations = [
        {"ordinal": ordinal, "version": path.name, "sha256": digest}
        for ordinal, (path, digest) in enumerate(manifest, start=1)
    ]
    migration_set_sha256 = _sha256(ordered_migrations)
    namespace_role_ownership_sha256 = _sha256(NAMESPACE_ROLE_OWNERSHIP)
    first_migration = ordered_migrations[0]
    last_migration = ordered_migrations[-1]
    body: dict[str, Any] = {
        "canonicalization": CANONICALIZATION,
        "migration_set": {
            "algorithm": "sha256",
            "ordered_migrations": ordered_migrations,
            "sha256": migration_set_sha256,
        },
        "namespace_role_ownership": NAMESPACE_ROLE_OWNERSHIP,
        "namespace_role_ownership_sha256": namespace_role_ownership_sha256,
        "required_release_receipt_inputs": {
            "migration_set_sha256": migration_set_sha256,
            "migration_count": len(ordered_migrations),
            "first_migration_version": first_migration["version"],
            "last_migration_version": last_migration["version"],
            "namespace_role_ownership_sha256": namespace_role_ownership_sha256,
        },
    }
    return {"schema": SCHEMA, "contract_payload_sha256": _sha256(body), **body}


def validate_postgresql_release_contract(value: object, migrations_dir: Path) -> dict[str, Any]:
    """Reject any committed or sibling-provided contract not byte-logically exact."""

    expected = build_postgresql_release_contract(migrations_dir)
    if value != expected:
        raise RuntimeError("PostgreSQL release contract is missing, extra, reordered, or hash-mismatched")
    return expected


def render_postgresql_release_contract(migrations_dir: Path) -> bytes:
    """Emit stable non-secret JSON for the final PostgreSQL release receipt."""

    return (json.dumps(build_postgresql_release_contract(migrations_dir), indent=2, sort_keys=True) + "\n").encode()

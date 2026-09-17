from __future__ import annotations

import importlib.util
import fcntl
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "stages/workloads/scripts/scientific_runtime_cache_bootstrap.py"
SPEC = importlib.util.spec_from_file_location(
    "scientific_runtime_cache_bootstrap", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)


def contract(root: Path, *names: str) -> dict[str, object]:
    current_uid = os.getuid()
    legacy_gid = os.getgid()
    legacy_uid = current_uid + 1 if current_uid < 2_147_483_647 else current_uid - 1
    current_gid = legacy_gid + 1 if legacy_gid < 2_147_483_647 else legacy_gid - 1
    directories = []
    activation_id = "3" * 64
    for index, name in enumerate(names):
        subject = {
            "schema": "fs2-serve.nebius.ai/scientific-runtime-cache-boundary/v1",
            "tenant_id": f"tenant-{index}",
            "model_id": name,
            "workload_namespace": "fs2-models",
            "directory": name,
            "run_as_user": current_uid,
            "run_as_group": current_gid,
            "legacy_uid": legacy_uid,
            "legacy_gid": legacy_gid,
            "origin": "legacy-existing" if (root / name).exists() else "new-empty",
            "activation_id": activation_id,
        }
        boundary_sha256 = __import__("hashlib").sha256(
            json.dumps(subject, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        directories.append(
            {
                "name": name,
                "uid": current_uid,
                "gid": current_gid,
                "legacy_uid": legacy_uid,
                "legacy_gid": legacy_gid,
                "tenant_id": subject["tenant_id"],
                "model_id": name,
                "origin": subject["origin"],
                "boundary_sha256": boundary_sha256,
                "migration_phase": BOOTSTRAP.MIGRATION_PHASE,
                "mode": "2770",
            }
        )
    quiescence = {
        "schema": BOOTSTRAP.ACTIVE_FENCE_SCHEMA,
        "lease_name": BOOTSTRAP.WRITER_LOCK_NAME,
        "lease_uid": "1" * 64,
        "lock_device": 0,
        "lock_inode": 0,
        "lock_content_sha256": "0" * 64,
        "zero_writers": True,
        "writer_admission_fenced": True,
        "active_writer_count": 0,
        "activation_id": activation_id,
        "admission_policy_name": "fs2-scientific-runtime-cache-writer-fence",
        "admission_policy_uid": "11111111-1111-4111-8111-111111111111",
        "admission_policy_resource_version": "17",
        "admission_binding_name": "fs2-scientific-runtime-cache-writer-fence",
        "observed_at": (datetime.now(UTC) - timedelta(seconds=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "evidence_sha256": "2" * 64,
    }
    writer_lock = root / BOOTSTRAP.WRITER_LOCK_NAME
    lock_bytes = BOOTSTRAP._writer_lock_bytes(quiescence)
    if not writer_lock.exists():
        writer_lock.write_bytes(lock_bytes)
        writer_lock.chmod(0o444)
    lock_status = writer_lock.stat()
    quiescence.update(
        {
            "lock_device": lock_status.st_dev,
            "lock_inode": lock_status.st_ino,
            "lock_content_sha256": __import__("hashlib").sha256(lock_bytes).hexdigest(),
        }
    )
    return {
        "schema": BOOTSTRAP.CONTRACT_SCHEMA,
        "root": root.as_posix(),
        "writer_quiescence": {
            **quiescence,
            "authorization_id": "unit-quiescence-review",
        },
        "directories": directories,
    }


def test_prepares_only_exact_model_boundaries_and_preserves_existing_entries(
    tmp_path: Path,
) -> None:
    mosaic = tmp_path / "mosaic"
    mosaic.mkdir(mode=0o700)
    compiled = mosaic / "compiled.bin"
    compiled.write_bytes(b"existing-cache-entry")
    compiled.chmod(0o600)

    contract_document = contract(tmp_path, "mosaic", "openfold3", "protenix")
    expected_by_name = {
        item["name"]: item for item in contract_document["directories"]
    }
    prepared = BOOTSTRAP.prepare(
        contract_document,
        expected_root=tmp_path,
    )

    assert prepared == ("mosaic", "openfold3", "protenix")
    for name in prepared:
        status = (tmp_path / name).stat()
        expected = expected_by_name[name]
        assert status.st_uid == expected["uid"]
        assert status.st_gid == expected["legacy_gid"]
        assert stat.S_IMODE(status.st_mode) == 0o2770
    assert compiled.read_bytes() == b"existing-cache-entry"
    assert compiled.stat().st_uid == expected_by_name["mosaic"]["uid"]
    assert compiled.stat().st_gid == os.getgid()
    assert stat.S_IMODE(compiled.stat().st_mode) == 0o660
    writer_lock = tmp_path / BOOTSTRAP.WRITER_LOCK_NAME
    assert writer_lock.is_file()
    assert stat.S_IMODE(writer_lock.stat().st_mode) == 0o444


def test_dual_access_migrates_nested_entries_without_changing_bytes_or_owner(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "mosaic" / "jax" / "compiled"
    nested.mkdir(parents=True, mode=0o700)
    for directory in (tmp_path / "mosaic", tmp_path / "mosaic" / "jax", nested):
        directory.chmod(0o700)
    artifact = nested / "kernel.bin"
    artifact.write_bytes(b"legacy-kernel")
    artifact.chmod(0o600)
    original_uid = artifact.stat().st_uid

    BOOTSTRAP.prepare(contract(tmp_path, "mosaic"), expected_root=tmp_path)

    assert artifact.read_bytes() == b"legacy-kernel"
    assert artifact.stat().st_uid == original_uid
    assert artifact.stat().st_gid == os.getgid()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o660
    assert stat.S_IMODE(nested.stat().st_mode) & stat.S_ISGID


@pytest.mark.parametrize("name", ["../mosaic", "mosaic/jax", ".", "Mosaic", "mosaic_"])
def test_refuses_nested_or_noncanonical_directory_names(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="name is invalid"):
        BOOTSTRAP.prepare(contract(tmp_path, name), expected_root=tmp_path)


def test_refuses_a_symlink_collision_without_touching_its_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "mosaic").symlink_to(external, target_is_directory=True)

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="not a real directory"):
        BOOTSTRAP.prepare(contract(tmp_path, "mosaic"), expected_root=tmp_path)

    assert stat.S_IMODE(external.stat().st_mode) != 0o2770


def test_refuses_ownership_contract_drift(tmp_path: Path) -> None:
    document = contract(tmp_path, "mosaic")
    directories = document["directories"]
    assert isinstance(directories, list)
    directories[0]["mode"] = "0777"

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="phase or directory mode differs"):
        BOOTSTRAP.prepare(document, expected_root=tmp_path)


def test_full_tree_preflight_rejects_hardlinks_before_any_metadata_change(
    tmp_path: Path,
) -> None:
    first = tmp_path / "mosaic"
    second = tmp_path / "protenix"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    first_artifact = first / "first.bin"
    first_artifact.write_bytes(b"first")
    first_artifact.chmod(0o600)
    linked = second / "linked.bin"
    os.link(first_artifact, linked)
    original = (first_artifact.stat().st_gid, stat.S_IMODE(first_artifact.stat().st_mode))

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="hard-linked"):
        BOOTSTRAP.prepare(contract(tmp_path, "mosaic", "protenix"), expected_root=tmp_path)

    assert (first_artifact.stat().st_gid, stat.S_IMODE(first_artifact.stat().st_mode)) == original
    assert not list(tmp_path.glob(".fs2-cache-migration-*.journal.json.committed.*"))


def test_failed_transaction_restores_original_metadata_and_retains_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mosaic"
    target.mkdir(mode=0o700)
    artifact = target / "kernel.bin"
    artifact.write_bytes(b"kernel")
    artifact.chmod(0o600)
    original = (
        artifact.stat().st_uid,
        artifact.stat().st_gid,
        stat.S_IMODE(artifact.stat().st_mode),
    )
    real_fchmod = BOOTSTRAP.os.fchmod
    calls = 0

    def fail_once(fd: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected transaction failure")
        real_fchmod(fd, mode)

    monkeypatch.setattr(BOOTSTRAP.os, "fchmod", fail_once)
    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="transaction aborted"):
        BOOTSTRAP.prepare(contract(tmp_path, "mosaic"), expected_root=tmp_path)

    assert (artifact.stat().st_gid, stat.S_IMODE(artifact.stat().st_mode)) == original
    journals = list(tmp_path.glob(".fs2-cache-migration-*.journal.json.payload.*"))
    assert len(journals) == 1
    journal = json.loads(journals[0].read_text())
    assert journal["schema"] == BOOTSTRAP.JOURNAL_SCHEMA
    assert any(entry["path"] == "kernel.bin" for entry in journal["entries"])


def test_retry_rejects_extra_unjournaled_entry_before_metadata_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mosaic"
    target.mkdir(mode=0o700)
    artifact = target / "kernel.bin"
    artifact.write_bytes(b"kernel")
    artifact.chmod(0o600)
    document = contract(tmp_path, "mosaic")
    real_write_once = BOOTSTRAP._write_once

    def insert_after_journal(root_fd: int, name: str, value: object) -> None:
        real_write_once(root_fd, name, value)
        if name.endswith(".journal.json"):
            extra = target / "unjournaled.bin"
            extra.write_bytes(b"late writer")
            extra.chmod(0o600)

    monkeypatch.setattr(BOOTSTRAP, "_write_once", insert_after_journal)
    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="changed after journal"):
        BOOTSTRAP.prepare(document, expected_root=tmp_path)


def test_active_writer_shared_lock_blocks_cache_migration(tmp_path: Path) -> None:
    document = contract(tmp_path, "mosaic")
    writer_lock = tmp_path / BOOTSTRAP.WRITER_LOCK_NAME
    descriptor = os.open(writer_lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="already held"):
            BOOTSTRAP.prepare(document, expected_root=tmp_path)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_signed_lease_must_bind_the_exact_flocked_inode_and_content(
    tmp_path: Path,
) -> None:
    document = contract(tmp_path, "mosaic")
    quiescence = document["writer_quiescence"]
    assert isinstance(quiescence, dict)
    quiescence["lock_inode"] += 1

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="lease is not one regular inode"):
        BOOTSTRAP.prepare(document, expected_root=tmp_path)


def test_signed_lease_refuses_content_for_another_activation(tmp_path: Path) -> None:
    document = contract(tmp_path, "mosaic")
    writer_lock = tmp_path / BOOTSTRAP.WRITER_LOCK_NAME
    writer_lock.chmod(0o644)
    writer_lock.write_text('{"schema":"wrong-lease"}\n', encoding="utf-8")
    writer_lock.chmod(0o444)

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="lease is not one regular inode"):
        BOOTSTRAP.prepare(document, expected_root=tmp_path)


def test_write_once_ignores_incomplete_staging_and_publishes_atomic_commit(
    tmp_path: Path,
) -> None:
    name = ".fs2-cache-migration-unit.journal.json"
    (tmp_path / f"{name}.staging.interrupted").write_bytes(b'{"partial":')
    root_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert BOOTSTRAP._read_once(root_fd, name) is None
        BOOTSTRAP._write_once(root_fd, name, {"schema": "unit", "entries": []})
        assert BOOTSTRAP._read_once(root_fd, name) == {
            "schema": "unit",
            "entries": [],
        }
    finally:
        os.close(root_fd)
    assert (tmp_path / f"{name}.staging.interrupted").read_bytes() == b'{"partial":'
    assert len(list(tmp_path.glob(f"{name}.committed.*"))) == 1


def test_second_fchmod_failure_after_fchown_restores_cartesian_intermediate_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mosaic"
    target.mkdir(mode=0o700)
    artifact = target / "kernel.bin"
    artifact.write_bytes(b"kernel")
    artifact.chmod(0o600)
    original = (artifact.stat().st_gid, stat.S_IMODE(artifact.stat().st_mode))
    real_fchmod = BOOTSTRAP.os.fchmod
    desired_calls = 0

    def fail_after_chown(fd: int, mode: int) -> None:
        nonlocal desired_calls
        if mode != original[2]:
            desired_calls += 1
            if desired_calls == 2:
                raise OSError("injected post-chown failure")
        real_fchmod(fd, mode)

    monkeypatch.setattr(BOOTSTRAP.os, "fchmod", fail_after_chown)
    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="transaction aborted"):
        BOOTSTRAP.prepare(contract(tmp_path, "mosaic"), expected_root=tmp_path)
    assert (
        artifact.stat().st_uid,
        artifact.stat().st_gid,
        stat.S_IMODE(artifact.stat().st_mode),
    ) == original


def test_recovery_accepts_fchown_cleared_setgid_intermediate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "mosaic"
    target.mkdir(mode=0o700)
    target.chmod(0o770)
    target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    status = target.stat()
    entry = {
        "boundary": "mosaic",
        "path": ".",
        "kind": "directory",
        "device": status.st_dev,
        "inode": status.st_ino,
        "links": status.st_nlink,
        "uid": status.st_uid,
        "gid": status.st_gid,
        "mode": 0o700,
        "desired_uid": status.st_uid,
        "desired_gid": status.st_gid,
        "desired_mode": 0o2770,
    }
    try:
        BOOTSTRAP._set_entry_metadata(target_fd, entry, desired=False)
    finally:
        os.close(target_fd)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_terraform_uses_execution_map_owners_and_blocks_control_plane() -> None:
    execution_map = json.loads(
        (ROOT / "catalog/runtime/contracts/scientific-execution-map.json").read_text(
            encoding="utf-8"
        )
    )
    claims: dict[str, tuple[int, int]] = {}
    claims_by_namespace: dict[str, dict[str, tuple[int, int]]] = {}
    for model in execution_map["models"]:
        for stage in model["stages"]:
            if not any(mount["kind"] == "runtime-cache" for mount in stage["mounts"]):
                continue
            namespace = model["workload_namespace"]
            namespace_claims = claims_by_namespace.setdefault(namespace, {})
            cache_mount = next(mount for mount in stage["mounts"] if mount["kind"] == "runtime-cache")
            roots = {
                value.split("/", 3)[2]
                for value in stage["environment"].values()
                if value.startswith("/cache/")
            }
            assert cache_mount["mount_path"] == "/cache"
            assert cache_mount["sub_path"] is None
            assert len(roots) == 1
            owner = (stage["workspace_uid"], stage["workspace_gid"])
            for cache_root in roots:
                if cache_root in claims:
                    assert claims[cache_root] == owner
                claims[cache_root] = owner
                if cache_root in namespace_claims:
                    assert namespace_claims[cache_root] == owner
                namespace_claims[cache_root] = owner

    assert claims == {
        "alphafold3": (11004, 11004),
        "mosaic": (11001, 11001),
        "openfold3": (11002, 11002),
        "protenix": (11003, 11003),
    }
    assert len({owner[0] for owner in claims.values()}) == len(claims)
    assert len({owner[1] for owner in claims.values()}) == len(claims)
    assert claims_by_namespace == {
        "fs2-academic-poc": {"alphafold3": (11004, 11004)},
        "fs2-models": {
            "mosaic": (11001, 11001),
            "openfold3": (11002, 11002),
            "protenix": (11003, 11003),
        },
    }

    cache_source = (ROOT / "stages/workloads/scientific_artifacts.tf").read_text(
        encoding="utf-8"
    )
    assert (
        'resource "kubernetes_job_v1" "scientific_runtime_cache_bootstrap"'
        in cache_source
    )
    assert (
        'resource "kubernetes_persistent_volume_claim_v1" "scientific_runtime_cache_additional"'
        in cache_source
    )
    assert (
        'resource "kubernetes_job_v1" "scientific_runtime_cache_bootstrap_additional"'
        in cache_source
    )
    assert "scientific_runtime_cache_additional_namespaces" in cache_source
    assert "namespace_claims = local.scientific_runtime_cache_namespace_claims" in cache_source
    assert "workspace_uid      = try(stage.workspace_uid, null)" in cache_source
    assert "workspace_gid      = try(stage.workspace_gid, null)" in cache_source
    assert "scientific_runtime_cache_boundaries" in cache_source
    assert 'migration_phase = "journaled-dual-access-legacy-group"' in cache_source
    assert "scientific-runtime-cache-ownership/v4" in cache_source
    assert "claim.tenant_id" in cache_source
    assert "cache-boundary" in cache_source
    assert 'kind       = "ValidatingAdmissionPolicy"' in cache_source
    assert 'kind       = "ValidatingAdmissionPolicyBinding"' in cache_source
    assert 'validationActions = ["Deny", "Audit"]' in cache_source
    assert "scientific_runtime_cache_writer_boundary_cel" in cache_source
    assert 'resources   = ["pods/ephemeralcontainers"]' in cache_source
    assert "request.userInfo.username" in cache_source
    assert "bootstrap_job_creator" in cache_source
    assert "scientific_workload_creator" in cache_source
    assert "job_controller" in cache_source
    assert "jobset_controller" in cache_source
    assert "hasPolicyOwner" in cache_source
    assert "scientific_runtime_cache_bootstrap_contract_cel" in cache_source
    assert "containers[0].env[0].value" in cache_source
    assert "scientific runtime-cache workloads may not use subPathExpr or host ports" in cache_source
    assert "scientific runtime-cache workloads may not project block devices" in cache_source
    assert 'key      = "kubernetes.io/metadata.name"' in cache_source
    assert "admission_policy_uid" in cache_source
    assert "admission_policy_resource_version" in cache_source
    assert "lock_device" in cache_source
    assert "lock_inode" in cache_source
    assert "lock_content_sha256" in cache_source
    assert cache_source.count('resource "kubernetes_service_account_v1" "scientific_runtime_cache_bootstrap') == 2
    assert cache_source.count('name      = "fs2-scientific-cache-bootstrap"') == 2
    assert cache_source.count("service_account_name            = kubernetes_service_account_v1.scientific_runtime_cache_bootstrap") == 2
    assert "kubernetes_role_binding" not in cache_source
    assert "mount.mount_path == local.scientific_runtime_cache_mount_path" in cache_source
    assert "mount.sub_path == null" in cache_source
    assert cache_source.count('mode = "2770"') == 2
    assert cache_source.count('"FSETID",') == 2
    assert '"storage.fs2.nebius/shared-cache" = "true"' in cache_source
    assert "fs_group" not in cache_source
    control_plane_source = (ROOT / "stages/workloads/control_plane.tf").read_text(
        encoding="utf-8"
    )
    assert (
        "kubernetes_job_v1.scientific_runtime_cache_bootstrap," in control_plane_source
    )
    assert (
        "kubernetes_job_v1.scientific_runtime_cache_bootstrap_additional,"
        in control_plane_source
    )

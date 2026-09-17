from __future__ import annotations

import importlib.util
import json
import os
import stat
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
    legacy_uid = os.getuid()
    legacy_gid = os.getgid()
    current_uid = legacy_uid + 1 if legacy_uid < 2_147_483_647 else legacy_uid - 1
    current_gid = legacy_gid + 1 if legacy_gid < 2_147_483_647 else legacy_gid - 1
    return {
        "schema": BOOTSTRAP.CONTRACT_SCHEMA,
        "root": root.as_posix(),
        "directories": [
            {
                "name": name,
                "uid": current_uid,
                "gid": current_gid,
                "legacy_uid": legacy_uid,
                "legacy_gid": legacy_gid,
                "migration_phase": BOOTSTRAP.MIGRATION_PHASE,
                "mode": "2770",
            }
            for name in names
        ],
    }


def test_prepares_only_exact_model_boundaries_and_preserves_existing_entries(
    tmp_path: Path,
) -> None:
    mosaic = tmp_path / "mosaic"
    mosaic.mkdir(mode=0o700)
    compiled = mosaic / "compiled.bin"
    compiled.write_bytes(b"existing-cache-entry")
    compiled.chmod(0o600)

    prepared = BOOTSTRAP.prepare(
        contract(tmp_path, "mosaic", "openfold3", "protenix"),
        expected_root=tmp_path,
    )

    assert prepared == ("mosaic", "openfold3", "protenix")
    for name in prepared:
        status = (tmp_path / name).stat()
        assert status.st_uid == os.getuid()
        assert status.st_gid == os.getgid()
        assert stat.S_IMODE(status.st_mode) == 0o2770
    assert compiled.read_bytes() == b"existing-cache-entry"
    assert compiled.stat().st_uid == os.getuid()
    assert compiled.stat().st_gid == os.getgid()
    assert stat.S_IMODE(compiled.stat().st_mode) == 0o660


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

    with pytest.raises(BOOTSTRAP.CacheOwnershipError, match="mode must be 2770"):
        BOOTSTRAP.prepare(document, expected_root=tmp_path)


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
    assert "scientific_runtime_cache_identities_by_model" in cache_source
    assert "scientific_runtime_cache_models_by_uid" in cache_source
    assert "scientific_runtime_cache_models_by_gid" in cache_source
    assert "scientific_runtime_cache_legacy_identities" in cache_source
    assert 'migration_phase = "dual-access-legacy-group"' in cache_source
    assert "scientific-runtime-cache-ownership/v2" in cache_source
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

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
    return {
        "schema": BOOTSTRAP.CONTRACT_SCHEMA,
        "root": root.as_posix(),
        "directories": [
            {
                "name": name,
                "uid": os.getuid(),
                "gid": os.getgid(),
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
    assert stat.S_IMODE(compiled.stat().st_mode) == 0o600


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
            roots = {
                value.split("/")[2]
                for value in stage["environment"].values()
                if value.startswith("/cache/")
            }
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
        "alphafold3": (1001, 1001),
        "mosaic": (10001, 10001),
        "openfold3": (10001, 10001),
        "protenix": (10001, 10001),
    }
    assert claims_by_namespace == {
        "fs2-academic-poc": {"alphafold3": (1001, 1001)},
        "fs2-models": {
            "mosaic": (10001, 10001),
            "openfold3": (10001, 10001),
            "protenix": (10001, 10001),
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

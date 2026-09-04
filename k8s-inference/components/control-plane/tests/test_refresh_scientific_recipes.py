from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from conftest import CONTROL_ROOT

SCRIPT = CONTROL_ROOT / "scripts/refresh_scientific_recipes.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fs2_refresh_scientific_recipes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_fixture(root: Path, *, qualification: bool = True) -> tuple[Path, Path]:
    profile_path = root / "scientific-workload-profiles.json"
    execution_map_path = root / "scientific-execution-map.json"
    profile: dict[str, object] = {
        "model_id": "boltzgen",
        "workload": {"stages": [{"id": "design", "resources": {"gpu": 1}}]},
        "execution_identity": {
            "model_revision": "1" * 40,
            "runtime_image_digest": "sha256:" + "2" * 64,
            "runtime_recipe_sha256": "3" * 64,
            "workload_recipe_sha256": "4" * 64,
            "artifact_manifest_digest": "5" * 64,
            "execution_identity_sha256": "6" * 64,
        },
    }
    if qualification:
        profile["qualification"] = {"execution_map_sha256": "7" * 64}
    profiles = {"profiles": [profile]}
    execution_map = {
        "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
        "models": [
            {
                "model_id": "boltzgen",
                "execution_identity_sha256": "8" * 64,
                "stages": [{"environment": {"probe": "café<&>\u2028\u2029"}}],
            }
        ],
    }
    profile_path.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
    execution_map_path.write_text(json.dumps(execution_map, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return profile_path, execution_map_path


def configure(module: ModuleType, monkeypatch: pytest.MonkeyPatch, root: Path, profile: Path, execution: Path) -> None:
    monkeypatch.setattr(module, "PROFILE_PATH", profile)
    monkeypatch.setattr(module, "EXECUTION_MAP_PATH", execution)
    monkeypatch.setattr(module, "SOLUTION_ROOT", root)
    monkeypatch.setattr(module, "MODEL_IDS", ("boltzgen",))
    monkeypatch.setattr(module, "runtime_recipe_sha256", lambda _root, _model_id: "9" * 64)


def test_helm_to_json_bytes_match_go_html_and_unicode_rules() -> None:
    module = load_script()
    assert module._helm_to_json_bytes({"z": "café<&>\u2028\u2029", "a": 1}) == (
        b'{"a":1,"z":"caf\xc3\xa9\\u003c\\u0026\\u003e\\u2028\\u2029"}'
    )


def test_write_mode_refreshes_the_complete_digest_chain(tmp_path: Path, monkeypatch, capsys) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    original_profiles = profile_path.read_bytes()
    original_map = execution_map_path.read_bytes()

    assert module.main(["--check"]) == 1
    assert profile_path.read_bytes() == original_profiles
    assert execution_map_path.read_bytes() == original_map
    capsys.readouterr()

    assert module.main([]) == 0
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    execution_map = json.loads(execution_map_path.read_text(encoding="utf-8"))
    profile = profiles["profiles"][0]
    identity = profile["execution_identity"]
    assert identity["runtime_recipe_sha256"] == "9" * 64
    assert (
        identity["workload_recipe_sha256"] == hashlib.sha256(module._canonical_bytes(profile["workload"])).hexdigest()
    )
    identity_payload = {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
    expected_identity = hashlib.sha256(module._canonical_bytes(identity_payload)).hexdigest()
    assert identity["execution_identity_sha256"] == expected_identity
    assert execution_map["models"][0]["execution_identity_sha256"] == expected_identity
    expected_map_digest = hashlib.sha256(module._helm_to_json_bytes(execution_map)).hexdigest()
    assert profile["qualification"]["execution_map_sha256"] == expected_map_digest
    assert not list(tmp_path.glob(".*.json.*"))

    refreshed_profiles = profile_path.read_bytes()
    refreshed_map = execution_map_path.read_bytes()
    assert module.main(["--check"]) == 0
    assert profile_path.read_bytes() == refreshed_profiles
    assert execution_map_path.read_bytes() == refreshed_map


def test_derivation_failure_writes_neither_contract(tmp_path: Path, monkeypatch) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path, qualification=False)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    original_profiles = profile_path.read_bytes()
    original_map = execution_map_path.read_bytes()

    with pytest.raises(SystemExit, match="has no qualification contract"):
        module.main([])

    assert profile_path.read_bytes() == original_profiles
    assert execution_map_path.read_bytes() == original_map


def test_second_replace_failure_rolls_back_the_first_contract(tmp_path: Path, monkeypatch) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    original_profiles = profile_path.read_bytes()
    original_map = execution_map_path.read_bytes()
    real_replace = module.os.replace
    failed = False

    def fail_execution_map_once(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == execution_map_path and not failed:
            failed = True
            raise OSError("injected second replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_execution_map_once)
    with pytest.raises(OSError, match="injected second replacement failure"):
        module.main([])

    assert profile_path.read_bytes() == original_profiles
    assert execution_map_path.read_bytes() == original_map
    assert not list(tmp_path.glob(".*.json.*"))

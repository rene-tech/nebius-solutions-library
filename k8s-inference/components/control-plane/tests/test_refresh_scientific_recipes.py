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
SCIENTIFIC_FLEET = (
    "alphafold3",
    "bindcraft",
    "boltzgen",
    "esmfold2",
    "esmfold2-fast",
    "mosaic",
    "openfold3-openbind",
    "proteina-complexa",
    "protenix-v2",
    "rfdiffusion",
)


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
    monkeypatch.setattr(module, "runtime_recipe_sha256", lambda _root, _model_id: "9" * 64)


def add_third_profile(module: ModuleType, profile_path: Path) -> None:
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    third = json.loads(json.dumps(profiles["profiles"][0]))
    third["model_id"] = "future-science"
    third["workload"]["stages"][0]["resources"]["gpu"] = 2
    identity = third["execution_identity"]
    identity["runtime_recipe_sha256"] = "a" * 64
    identity["workload_recipe_sha256"] = "b" * 64
    identity_payload = {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
    identity["execution_identity_sha256"] = hashlib.sha256(module._canonical_bytes(identity_payload)).hexdigest()
    third.pop("qualification", None)
    profiles["profiles"].append(third)
    profile_path.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")


def test_helm_to_json_bytes_match_go_html_and_unicode_rules() -> None:
    module = load_script()
    assert module._helm_to_json_bytes({"z": "café<&>\u2028\u2029", "a": 1}) == (
        b'{"a":1,"z":"caf\xc3\xa9\\u003c\\u0026\\u003e\\u2028\\u2029"}'
    )


@pytest.mark.parametrize("model_id", SCIENTIFIC_FLEET)
def test_every_scientific_fleet_recipe_has_an_exact_source_closure(model_id: str) -> None:
    module = load_script()
    digest = module.runtime_recipe_sha256(CONTROL_ROOT.parents[1], model_id)
    assert len(digest) == 64
    int(digest, 16)


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


def test_write_mode_atomically_refreshes_model_owner_and_pending_result_identity(tmp_path: Path, monkeypatch) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    original_profile = json.loads(profile_path.read_text(encoding="utf-8"))["profiles"][0]
    owner_path = tmp_path / "models/example/activation/fragment.json"
    owner_path.parent.mkdir(parents=True)
    result_path = owner_path.parent / "public-result.example.json"
    result_path.write_text(
        json.dumps(
            {
                "terminal_status": "failed",
                "semantic_validation": {"status": "not-run"},
                "error": {"code": module.PENDING_RESULT_ERROR},
                "execution_identity": {
                    "model_id": "boltzgen",
                    "variant_id": "unit-v1",
                    "model_revision": original_profile["execution_identity"]["model_revision"],
                    "runtime_image_digest": original_profile["execution_identity"]["runtime_image_digest"],
                    "runtime_recipe_sha256": "0" * 64,
                    "workload_recipe_sha256": "0" * 64,
                    "model_artifact_manifest_digest": "0" * 64,
                    "execution_identity_sha256": "0" * 64,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    owner_path.write_text(
        json.dumps(
            {
                "schema": module.PRIMARY_FRAGMENT_SCHEMA,
                "model_id": "boltzgen",
                "accepted_evidence": {
                    "source": {"revision": original_profile["execution_identity"]["model_revision"]},
                    "runtime_image": {"digest": original_profile["execution_identity"]["runtime_image_digest"]},
                },
                "profile_projection": {
                    "merge_target": module.PROFILE_MERGE_TARGET,
                    "profile": original_profile,
                },
                "execution_projection": {
                    "variant_id": "unit-v1",
                    "artifact_identity_inputs": ["a" * 64, "b" * 64],
                },
                "public_fixtures": {"result": result_path.relative_to(tmp_path).as_posix()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert module.main([]) == 0
    refreshed_profile = json.loads(profile_path.read_text(encoding="utf-8"))["profiles"][0]
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    assert owner["profile_projection"]["profile"] == refreshed_profile
    result = json.loads(result_path.read_text(encoding="utf-8"))
    executable_identity = {
        "model_id": "boltzgen",
        "variant_id": "unit-v1",
        "model_revision": refreshed_profile["execution_identity"]["model_revision"],
        "runtime_image_digest": refreshed_profile["execution_identity"]["runtime_image_digest"],
        "runtime_recipe_sha256": refreshed_profile["execution_identity"]["runtime_recipe_sha256"],
        "workload_recipe_sha256": refreshed_profile["execution_identity"]["workload_recipe_sha256"],
        "model_artifact_manifest_digest": hashlib.sha256(
            module._canonical_bytes(sorted(["a" * 64, "b" * 64]))
        ).hexdigest(),
    }
    assert result["execution_identity"] == {
        **executable_identity,
        "execution_identity_sha256": hashlib.sha256(module._canonical_bytes(executable_identity)).hexdigest(),
    }
    assert module.main(["--check"]) == 0

    result["terminal_status"] = "succeeded"
    result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    before_profiles = profile_path.read_bytes()
    before_result = result_path.read_bytes()
    monkeypatch.setattr(module, "runtime_recipe_sha256", lambda _root, _model_id: "a" * 64)
    with pytest.raises(SystemExit, match="not a refreshable pending fixture"):
        module.main([])
    assert profile_path.read_bytes() == before_profiles
    assert result_path.read_bytes() == before_result


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


def test_mapped_candidate_refreshes_recipes_but_retains_null_promotion_identity(
    tmp_path: Path, monkeypatch
) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path, qualification=False)
    profiles = json.loads(profile_path.read_text(encoding="utf-8"))
    candidate = profiles["profiles"][0]
    candidate["state"] = "candidate-unqualified"
    identity = candidate["execution_identity"]
    identity["artifact_manifest_digest"] = None
    identity["execution_identity_sha256"] = None
    profile_path.write_text(json.dumps(profiles, indent=2) + "\n", encoding="utf-8")
    execution_map = json.loads(execution_map_path.read_text(encoding="utf-8"))
    execution_map["models"][0]["execution_identity_sha256"] = None
    execution_map_path.write_text(json.dumps(execution_map, indent=2) + "\n", encoding="utf-8")
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)

    assert module.main([]) == 0
    refreshed_profile = json.loads(profile_path.read_text(encoding="utf-8"))["profiles"][0]
    refreshed_map = json.loads(execution_map_path.read_text(encoding="utf-8"))
    refreshed_identity = refreshed_profile["execution_identity"]
    assert refreshed_identity["runtime_recipe_sha256"] == "9" * 64
    assert refreshed_identity["workload_recipe_sha256"] == hashlib.sha256(
        module._canonical_bytes(refreshed_profile["workload"])
    ).hexdigest()
    assert refreshed_identity["artifact_manifest_digest"] is None
    assert refreshed_identity["execution_identity_sha256"] is None
    assert "qualification" not in refreshed_profile
    assert refreshed_map["models"][0]["execution_identity_sha256"] is None
    assert module.main(["--check"]) == 0


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


def test_third_profile_component_drift_is_discovered(tmp_path: Path, monkeypatch, capsys) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    assert module.main([]) == 0
    capsys.readouterr()
    add_third_profile(module, profile_path)

    assert module.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "future-science runtime_recipe_sha256" in output
    assert "future-science workload_recipe_sha256" in output


def test_unsupported_third_profile_fails_explicitly(tmp_path: Path, monkeypatch) -> None:
    module = load_script()
    profile_path, execution_map_path = write_fixture(tmp_path)
    configure(module, monkeypatch, tmp_path, profile_path, execution_map_path)
    add_third_profile(module, profile_path)

    def recipe_digest(_root: Path, model_id: str) -> str:
        if model_id == "future-science":
            raise module.ScientificAdapterError("no runtime recipe is registered")
        return "9" * 64

    monkeypatch.setattr(module, "runtime_recipe_sha256", recipe_digest)
    with pytest.raises(SystemExit, match="future-science has no refreshable runtime recipe"):
        module.main(["--check"])

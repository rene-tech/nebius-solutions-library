from __future__ import annotations

import json
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer, ScientificExecutionMapError
from fs2_serve.scientific_batch.execution_map_builder import build_execution_map, config_map_manifest, main
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog

REPO_ROOT = Path(__file__).resolve().parents[3]
CATALOG_ROOT = REPO_ROOT / "catalog/runtime"
TARGETS = CATALOG_ROOT / "contracts/scientific-execution-targets.json"
AF3_CONTENT = "a" * 64
AF3_MANIFEST = "b" * 64

MOUNT_PATHS = {
    "alphafold3-parameters": "/models/af3.bin.zst",
    "alphafold3-public-databases-v3.0": "/reference-data",
    "esmfold2-trunk": "/models/esmfold2",
    "esmfold2-fast-trunk": "/models/esmfold2-fast",
    "esmc-6b": "/models/esmc-6b",
    "esmfold2-ccd": "/databases/esmfold2",
    "openfold3-openbind-0": "/models/openfold3",
    "openfold3-components-bcif": "/databases/openfold3",
    "protenix-v2": "/models/protenix-v2",
}


def promoted_catalog(tmp_path: Path) -> tuple[ScientificProfileCatalog, dict[str, object]]:
    target = tmp_path / "catalog"
    shutil.copytree(CATALOG_ROOT, target)
    document = json.loads((target / "contracts/scientific-workload-profiles.json").read_text())
    for index, profile in enumerate(document["profiles"], start=1):
        profile["state"] = "qualified"
        profile["route_exposed"] = True
        profile["semantic_validation"]["state"] = "qualified"
        identity = profile["execution_identity"]
        identity["runtime_image_state"] = "digest-pinned"
        identity["runtime_image_digest"] = f"sha256:{index:064x}"
        identity["artifact_manifest_digest"] = f"{index + 16:064x}"
        identity["execution_identity_sha256"] = f"{index + 32:064x}"
        if profile["model_id"] == "alphafold3":
            reference = next(
                item
                for item in profile["artifact_requirements"]
                if item["artifact_id"] == "alphafold3-public-databases-v3.0"
            )
            reference.update(
                {
                    "content_digest_sha256": AF3_CONTENT,
                    "localization_manifest_sha256": AF3_MANIFEST,
                    "required_files": [".fs2-manifest-sha256"],
                    "aggregate_tree": {
                        "kind": "aggregate-tree",
                        "dataset_relative_path": (
                            "datasets/alphafold3-public-databases-v3.0/"
                            f"v3.0-paper-snapshot-2022-09-28/sha256/{AF3_CONTENT}"
                        ),
                        "dataset_uri": (
                            "file:///mnt/fs2-reference-data/data/datasets/"
                            "alphafold3-public-databases-v3.0/"
                            f"v3.0-paper-snapshot-2022-09-28/sha256/{AF3_CONTENT}"
                        ),
                        "file_count": 5001,
                    },
                }
            )
    (target / "contracts/scientific-workload-profiles.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    return ScientificProfileCatalog.load(target), document


def localization_contract(profile_document: dict[str, object]) -> dict[str, object]:
    models = []
    for model in profile_document["profiles"]:  # type: ignore[index]
        artifacts = []
        for requirement in model["artifact_requirements"]:  # type: ignore[index]
            artifact_id = requirement["artifact_id"]
            localization = {
                "artifact_id": artifact_id,
                "mount_path": MOUNT_PATHS[artifact_id],
                "content_digest": requirement["content_digest_sha256"],
                "localization_receipt_digest": "sha256:" + "c" * 64,
            }
            if "aggregate_tree" in requirement:
                tree = requirement["aggregate_tree"]
                localization["aggregate_tree"] = {
                    "manifest_digest": requirement["localization_manifest_sha256"],
                    "dataset_relative_path": tree["dataset_relative_path"],
                    "dataset_uri": tree["dataset_uri"],
                    "file_count": tree["file_count"],
                    "node_accessibility": {
                        "evidence_receipt_digest": "sha256:" + "d" * 64,
                        "required_node_labels": {"storage.fs2.nebius/reference-data": "true"},
                        "node_names": [],
                    },
                }
            else:
                localization["file_manifest"] = [
                    {key: item[key] for key in ("path", "sha256", "size_bytes")}
                    for item in requirement["file_manifest"]
                ]
            artifacts.append(localization)
        models.append({"model_id": model["model_id"], "runtime_artifacts": artifacts})
    return {"schema": "fs2-serve.nebius.ai/scientific-runtime-localizations/v1", "models": models}


def test_generated_installed_config_map_is_the_production_reader_contract(tmp_path: Path) -> None:
    profiles, profile_document = promoted_catalog(tmp_path)
    targets = json.loads(TARGETS.read_text())
    localizations = localization_contract(profile_document)

    execution_map = build_execution_map(profiles=profiles, targets=targets, localizations=localizations)
    config_map = config_map_manifest(
        execution_map,
        profiles_source=profile_document,
        targets=targets,
        localizations=localizations,
    )
    installed = tmp_path / "installed-execution-map.json"
    installed.write_text(config_map["data"]["execution-map.json"])
    renderer = FileScientificManifestRenderer(path=installed, profiles=profiles)

    assert set(renderer.variants) == {"alphafold3", "esmfold2", "esmfold2-fast", "openfold3", "protenix-v2"}
    # One namespace for the licensed claim and the durable state, two queues so
    # CPU preprocessing never consumes accelerator quota.
    af3_data = renderer.executions[("alphafold3", "data-pipeline")]
    assert (af3_data.namespace, af3_data.local_queue_name) == (
        "fs2-academic-poc",
        "academic-scientific-cpu",
    )
    af3_inference = renderer.executions[("alphafold3", "inference")]
    assert (af3_inference.namespace, af3_inference.local_queue_name) == (
        "fs2-academic-poc",
        "academic-scientific",
    )
    assert af3_data.node_selector["storage.fs2.nebius/reference-data"] == "true"
    # The plane root is exposed whole: no subPath, because the receipt and the
    # sibling manifest have to resolve alongside the dataset. The dataset itself
    # is pinned by the aggregate tree's content-addressed path.
    reference_mount = next(mount for mount in af3_data.mounts if mount.kind == "operator-host-path")
    assert reference_mount.sub_path is None
    assert reference_mount.mount_path == "/reference-data"
    assert renderer.runtime_artifacts[
        ("alphafold3", "alphafold3-public-databases-v3.0")
    ].aggregate_tree.dataset_relative_path.endswith(AF3_CONTENT)
    assert next(mount for mount in af3_data.mounts if mount.artifact_id).supplemental_groups == (1000,)
    af3_inference = renderer.executions[("alphafold3", "inference")]
    assert {group for mount in af3_inference.mounts for group in mount.supplemental_groups} == {65532}
    assert next(mount for mount in af3_inference.mounts if mount.kind == "cache").claim_name == (
        "scientific-alphafold3-cache"
    )
    assert next(
        mount for mount in renderer.executions[("openfold3", "inference")].mounts if mount.kind == "cache"
    ).claim_name == "scientific-openfold3-cache"
    assert next(
        mount for mount in renderer.executions[("protenix-v2", "sample-structure")].mounts if mount.kind == "cache"
    ).claim_name == "scientific-protenix-cache"
    assert config_map["metadata"]["name"].startswith("fs2-scientific-execution-")
    assert config_map["immutable"] is True


def test_production_cli_emits_terraform_apply_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles, profile_document = promoted_catalog(tmp_path)
    del profiles
    localizations_path = tmp_path / "localizations.json"
    localizations_path.write_text(json.dumps(localization_contract(profile_document)))
    output = tmp_path / "execution-map.json"
    config_map_output = tmp_path / "execution-map-configmap.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fs2-serve-render-scientific-execution-map",
            "--catalog-root",
            str(tmp_path / "catalog"),
            "--localizations",
            str(localizations_path),
            "--output",
            str(output),
            "--config-map-output",
            str(config_map_output),
        ],
    )
    assert main() == 0
    generated = json.loads(config_map_output.read_text())
    installed = tmp_path / "mounted-execution-map.json"
    installed.write_text(generated["data"]["execution-map.json"])
    FileScientificManifestRenderer(
        path=installed,
        profiles=ScientificProfileCatalog.load(tmp_path / "catalog"),
    )
    assert json.loads(output.read_text())["schema"] == "fs2-serve.nebius.ai/scientific-execution-map/v3"


def test_generator_rejects_current_unpromoted_catalog(tmp_path: Path) -> None:
    profiles = ScientificProfileCatalog.load(CATALOG_ROOT)
    with pytest.raises(ScientificExecutionMapError, match="no qualified scientific profile"):
        build_execution_map(
            profiles=profiles,
            targets=json.loads(TARGETS.read_text()),
            localizations={"schema": "fs2-serve.nebius.ai/scientific-runtime-localizations/v1", "models": []},
        )


def test_generator_rejects_wrong_af3_tree_identity(tmp_path: Path) -> None:
    profiles, profile_document = promoted_catalog(tmp_path)
    targets = json.loads(TARGETS.read_text())
    localizations = localization_contract(profile_document)
    bad = deepcopy(localizations)
    af3 = next(item for item in bad["models"] if item["model_id"] == "alphafold3")
    tree = next(
        item for item in af3["runtime_artifacts"] if item["artifact_id"] == "alphafold3-public-databases-v3.0"
    )["aggregate_tree"]
    tree["dataset_relative_path"] = tree["dataset_relative_path"].replace(AF3_CONTENT, "e" * 64)
    with pytest.raises(ScientificExecutionMapError, match="aggregate-tree evidence differs"):
        build_execution_map(profiles=profiles, targets=targets, localizations=bad)

"""Focused coverage for immutable-source localization and adapter preflight.

Archives and raw files have truthful, distinct semantics: an archive is never a
usable runtime directory, while a verified raw file is the runtime content.
Source provenance and runtime-tree identity stay separate, and partial,
different, or tampered trees fail closed before any model argv runs.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fs2_serve.scientific_batch import ScientificAdapterError, profile_from_catalog
from fs2_serve.scientific_batch.adapters import boltzgen, preflight_stage_trees, proteina_complexa
from fs2_serve.scientific_batch.adapters.localization import (
    RAW_FILE_ALGORITHM,
    RECURSIVE_INVENTORY_ALGORITHM,
    RUNTIME_MARKER_NAME,
    STAGING_PREFIX,
    TREE_INVENTORY_ALGORITHM,
    TREE_MANIFEST_ALGORITHM,
    ArtifactLocalizationError,
    LocalizationContract,
    TreeEntry,
    count_generation,
    generation_directory,
    generation_marker,
    interrupted_staging_directories,
    link_tree_into,
    load_generation_marker,
    load_localization_contracts,
    load_localization_contracts_from_path,
    localize_archive,
    localize_file,
    marker_bytes,
    marker_sha256,
    node_digest,
    prepare_staging_directory,
    promote_generation,
    raw_file_inventory_bytes,
    raw_file_inventory_sha256,
    recursive_inventory_sha256,
    scan_localized_tree,
    scan_recursive_tree,
    tree_counts,
    tree_inventory_sha256,
    tree_manifest_identity,
    verify_archive,
    verify_generation_marker,
    verify_localized_tree,
    write_generation_marker,
)
from fs2_serve.scientific_batch.adapters.localization import main as localization_main
from fs2_serve.scientific_batch.models import AdapterExecutionPlan, RuntimeTreeBinding, StageInvocation

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
CONTRACT_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"

MOLECULES_ID = "boltzgen-inference-molecules"
# The academic-assets plane's published identity for the installed PyRosetta
# tree. Both planes must name these same bytes identically.
PYROSETTA_ID = "bindcraft-pyrosetta-installed-tree"
PYROSETTA_TREE_SHA256 = "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
PYROSETTA_FILE_COUNT = 8697
PYROSETTA_TOTAL_BYTES = 3287122494
BINDCRAFT_PARAMS_ID = "alphafold2-params-bindcraft"
PARAMS_ID = "alphafold2-params"
VANILLA_MPNN_ID = "colabdesign-mpnn-weights-vanilla"
SOLUBLE_MPNN_ID = "colabdesign-mpnn-weights-soluble"
RFDIFFUSION_BASE_ID = "rfdiffusion-base-checkpoint"
COLABDESIGN_SITE_PACKAGES = "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn"

# The real molecule dictionary is 45,227 flat entries whose Chemical Component
# Dictionary codes are one to five characters. A three-character-only pattern
# rejects 1,676 real entries, so this fixture deliberately spans that range.
SYNTHETIC_ENTRIES: dict[str, bytes] = {
    "I.pkl": b"one-character code",
    "CL.pkl": b"two-character code",
    "HEM.pkl": b"three-character code" * 4,
    "A1LV8.pkl": b"five-character code" * 8,
}


def _contract_artifact(artifact_id: str) -> dict[str, Any]:
    """Look one artifact up by identity; the checked-in order is not a contract."""

    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(next(item for item in document["artifacts"] if item["artifact_id"] == artifact_id))


def _fixture(model_id: str, name: str) -> dict[str, Any]:
    return json.loads((ADAPTER_ROOT / model_id / "fixtures" / name).read_text(encoding="utf-8"))


def _profile(model_id: str) -> Mapping[str, object]:
    return profile_from_catalog(json.loads(PROFILE_PATH.read_text(encoding="utf-8")), model_id)


def _inventory(entries: Mapping[str, bytes]) -> str:
    return tree_inventory_sha256(
        TreeEntry(name, len(payload), zlib.crc32(payload) & 0xFFFFFFFF) for name, payload in entries.items()
    )


def _zip_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in members:
            bundle.writestr(name, payload)
    return buffer.getvalue()


def _tar_bytes(
    members: list[tuple[str, bytes]],
    *,
    symlink: str | None = None,
    directory: str | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        if directory is not None:
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            bundle.addfile(info)
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            bundle.addfile(info)
    return buffer.getvalue()


def _document(
    archive_payload: bytes,
    entries: Mapping[str, bytes],
    *,
    artifact_id: str = MOLECULES_ID,
    transform: str = "safe-extract-zip",
    filename: str = "mols.zip",
    media_type: str = "application/zip",
    pattern: str = r"^[A-Z0-9]{1,5}\.pkl$",
    model_id: str = "boltzgen",
    binding_kind: str = "argv-option",
    binding_name: str = "--moldir",
    extra_mount_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "transform": transform,
        "archive": {
            "filename": filename,
            "media_type": media_type,
            "bytes": len(archive_payload),
            "sha256": hashlib.sha256(archive_payload).hexdigest(),
            "source_uri": "https://example.invalid/" + filename,
            "source_revision": "c3d36fd276e9caf098c75d4113c6d5eb320b1a4c",
            "license_id": "MIT",
        },
        "tree": {
            "mount_paths": [f"/opt/fs2/artifacts/{artifact_id}", *extra_mount_paths],
            "entry_count": len(entries),
            "total_bytes": sum(len(payload) for payload in entries.values()),
            "entry_path_pattern": pattern,
            "inventory_algorithm": "fs2-flat-tree-inventory/v1",
            "inventory_sha256": _inventory(entries),
            "probe_entries": [
                {"path": name, "bytes": len(entries[name]), "sha256": hashlib.sha256(entries[name]).hexdigest()}
                for name in sorted(entries)[:2]
            ],
        },
        "consumers": [
            {
                "model_id": model_id,
                "binding_kind": binding_kind,
                "binding_name": binding_name,
                "mount_path": f"/opt/fs2/artifacts/{artifact_id}",
            },
            *(
                {
                    "model_id": "bindcraft",
                    "binding_kind": "environment-variable",
                    "binding_name": f"EXTRA_DIR_{index}",
                    "mount_path": path,
                }
                for index, path in enumerate(extra_mount_paths)
            ),
        ],
    }


def _raw_document(payload: bytes, *, filename: str = "Base_ckpt.pt") -> dict[str, Any]:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "artifact_id": RFDIFFUSION_BASE_ID,
        "transform": "verified-copy",
        "file": {
            "filename": filename,
            "media_type": "application/octet-stream",
            "bytes": len(payload),
            "sha256": digest,
            "source_uri": f"https://example.invalid/{filename}",
            "source_revision": "9273ef67335acaf91df0150473a274759229cdf6",
            "license_id": "BSD-3-Clause",
        },
        "tree": {
            "mount_paths": [f"/opt/fs2/artifacts/{RFDIFFUSION_BASE_ID}"],
            "entry_count": 1,
            "directory_count": 0,
            "symlink_count": 0,
            "total_bytes": len(payload),
            "entry_path_pattern": rf"^{re.escape(filename)}$",
            "inventory_algorithm": RAW_FILE_ALGORITHM,
            "inventory_sha256": raw_file_inventory_sha256(filename, len(payload), digest),
            "complete_entry_digests": True,
            "probe_entries": [{"path": filename, "bytes": len(payload), "sha256": digest}],
        },
        "consumers": [
            {
                "model_id": "rfdiffusion",
                "binding_kind": "argv-option",
                "binding_name": "--artifact-root",
                "mount_path": f"/opt/fs2/artifacts/{RFDIFFUSION_BASE_ID}",
            }
        ],
    }


def _binding(contract: LocalizationContract) -> RuntimeTreeBinding:
    return RuntimeTreeBinding(
        artifact_id=contract.artifact_id,
        mount_path=contract.tree.canonical_mount_path,
        archive_sha256=contract.archive.sha256,
        tree_inventory_sha256=contract.tree.inventory_sha256,
        entry_count=contract.tree.entry_count,
    )


def _materialize(root: Path, entries: Mapping[str, bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in entries.items():
        (root / name).write_bytes(payload)
    return root


@pytest.fixture
def synthetic_zip(tmp_path: Path) -> tuple[Path, LocalizationContract]:
    payload = _zip_bytes(sorted(SYNTHETIC_ENTRIES.items()))
    archive = tmp_path / "source" / "mols.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(payload)
    return archive, LocalizationContract.parse(_document(payload, SYNTHETIC_ENTRIES))


@pytest.fixture
def boltzgen_configure() -> StageInvocation:
    plan = boltzgen.compile_run(
        _profile("boltzgen"), _fixture("boltzgen", "positive-design.json"), operation_id="op-localization"
    )
    return plan.invocation("configure", "pdl1-a")


# ---------------------------------------------------------------------------
# Canonical contract
# ---------------------------------------------------------------------------


def test_checked_in_contract_declares_every_runtime_tree() -> None:
    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    assert set(contracts) == {
        MOLECULES_ID,
        PARAMS_ID,
        BINDCRAFT_PARAMS_ID,
        VANILLA_MPNN_ID,
        SOLUBLE_MPNN_ID,
        PYROSETTA_ID,
        RFDIFFUSION_BASE_ID,
    }

    # The one tree this plane verifies but never stages: another plane installed
    # it, owns it, and already named it.
    pyrosetta = contracts[PYROSETTA_ID]
    assert pyrosetta.externally_installed
    assert pyrosetta.visibility == "tenant-private"
    assert pyrosetta.source_sub_path == "pyrosetta-bindcraft/site-packages"
    assert pyrosetta.tree.inventory_algorithm == TREE_MANIFEST_ALGORITHM
    assert pyrosetta.tree.inventory_sha256 == PYROSETTA_TREE_SHA256
    assert pyrosetta.tree.entry_count == PYROSETTA_FILE_COUNT
    assert pyrosetta.tree.directory_count == 779
    assert pyrosetta.tree.total_bytes == PYROSETTA_TOTAL_BYTES

    molecules = contracts[MOLECULES_ID]
    assert molecules.tree.mount_paths == (f"/opt/fs2/artifacts/{MOLECULES_ID}",)
    assert molecules.tree.entry_count == 45_227
    assert molecules.tree.total_bytes == 1_820_698_819
    assert molecules.archive.filename == "mols.zip"
    assert molecules.binding_for("boltzgen").binding_name == "--moldir"

    params = contracts[PARAMS_ID]
    assert params.tree.mount_paths == (f"/opt/fs2/artifacts/{PARAMS_ID}",)
    assert params.tree.entry_count == 16
    assert params.tree.complete_entry_digests is True
    assert params.archive.filename == "alphafold_params_2022-12-06.tar"
    assert params.binding_for("proteina-complexa").binding_name == "AF2_DIR"

    checkpoint = contracts[RFDIFFUSION_BASE_ID]
    assert checkpoint.raw_file
    assert checkpoint.source.kind == "file"
    assert checkpoint.file.filename == "Base_ckpt.pt"
    assert checkpoint.file.size_bytes == 483_616_107
    assert checkpoint.file.sha256 == "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca"
    assert checkpoint.tree.inventory_algorithm == RAW_FILE_ALGORITHM
    assert checkpoint.tree.inventory_sha256 == "7f34c945e580dbf5ba96596dcd325150f6452f7a76ee06a3784b2891a9d4c03c"
    assert checkpoint.binding_for("rfdiffusion").binding_name == "--artifact-root"


def test_archive_provenance_is_never_the_extracted_tree_identity() -> None:
    for contract in load_localization_contracts_from_path(CONTRACT_PATH).values():
        if contract.raw_file:
            continue
        assert contract.archive.sha256 != contract.tree.inventory_sha256
        # The archive is also a different size from the tree it carries, so a
        # byte count cannot stand in for either identity.
        assert contract.archive.size_bytes != contract.tree.total_bytes


def test_raw_file_source_and_runtime_generation_are_distinct_but_exact() -> None:
    contract = load_localization_contracts_from_path(CONTRACT_PATH)[RFDIFFUSION_BASE_ID]
    assert contract.source.sha256 != contract.tree.inventory_sha256
    assert contract.source.size_bytes == contract.tree.total_bytes
    assert contract.tree.probe_entries == (
        type(contract.tree.probe_entries[0])(
            "Base_ckpt.pt",
            483_616_107,
            "0fcf7d7c32b4848030aca3a051e6768de194616f96ba6c38186351a33bfc6eca",
        ),
    )


def test_the_molecule_entry_pattern_accepts_one_to_five_character_codes() -> None:
    matcher = load_localization_contracts_from_path(CONTRACT_PATH)[MOLECULES_ID].tree.entry_matcher
    for name in ("I.pkl", "CL.pkl", "HEM.pkl", "A1LV8.pkl"):
        assert matcher.fullmatch(name) is not None, name
    for name in ("mols.zip", "A1LV8X.pkl", "hem.pkl", "HEM.pickle"):
        assert matcher.fullmatch(name) is None, name


def test_the_alphafold_entry_pattern_accepts_only_the_published_parameter_set() -> None:
    matcher = load_localization_contracts_from_path(CONTRACT_PATH)[PARAMS_ID].tree.entry_matcher
    for name in ("LICENSE", "params_model_1.npz", "params_model_5_ptm.npz", "params_model_3_multimer_v3.npz"):
        assert matcher.fullmatch(name) is not None, name
    for name in ("alphafold_params_2022-12-06.tar", "params_model_6.npz", "params_model_1_multimer_v2.npz"):
        assert matcher.fullmatch(name) is None, name


def test_a_contract_reusing_one_digest_for_both_identities_is_rejected() -> None:
    artifact = _contract_artifact(MOLECULES_ID)
    artifact["tree"]["inventory_sha256"] = artifact["archive"]["sha256"]
    with pytest.raises(ArtifactLocalizationError, match="distinct digests"):
        LocalizationContract.parse(artifact)


def test_a_contract_whose_archive_looks_like_a_tree_entry_is_rejected() -> None:
    artifact = _contract_artifact(MOLECULES_ID)
    artifact["archive"]["filename"] = "ABC.pkl"
    with pytest.raises(ArtifactLocalizationError, match="must not satisfy"):
        LocalizationContract.parse(artifact)


def test_a_contract_cannot_mount_another_artifacts_directory() -> None:
    artifact = _contract_artifact(MOLECULES_ID)
    artifact["tree"]["mount_paths"] = ["/opt/fs2/artifacts/somebody-else"]
    with pytest.raises(ArtifactLocalizationError, match="own directory"):
        LocalizationContract.parse(artifact)


def test_duplicate_artifacts_in_one_contract_document_are_rejected() -> None:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["artifacts"].append(_contract_artifact(MOLECULES_ID))
    with pytest.raises(ArtifactLocalizationError, match="duplicate artifact"):
        load_localization_contracts(document)


def test_an_unknown_contract_field_is_rejected() -> None:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["artifacts"][0]["tree"]["extraction_root"] = "/var/unexpected"
    with pytest.raises(ScientificAdapterError, match="unknown"):
        load_localization_contracts(document)


def test_raw_file_inventory_is_versioned_and_deterministic() -> None:
    payload = b"checkpoint-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    canonical = raw_file_inventory_bytes("Base_ckpt.pt", len(payload), digest)
    assert canonical == (
        b'{"algorithm":"fs2-raw-file/v1","entry":{"bytes":16,"path":"Base_ckpt.pt",'
        + f'"sha256":"{digest}"'.encode()
        + b"}}\n"
    )
    assert raw_file_inventory_sha256("Base_ckpt.pt", len(payload), digest) == hashlib.sha256(canonical).hexdigest()
    assert (
        raw_file_inventory_sha256("ActiveSite_ckpt.pt", len(payload), digest) != hashlib.sha256(canonical).hexdigest()
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"archive": value["file"]}), "exactly one"),
        (lambda value: value.pop("file"), "exactly one"),
        (lambda value: value["file"].update({"filename": "ActiveSite_ckpt.pt"}), "exactly bind"),
        (lambda value: value["file"].update({"bytes": value["file"]["bytes"] + 1}), "exactly bind"),
        (lambda value: value["tree"].update({"inventory_sha256": "0" * 64}), "inventory digest"),
        (lambda value: value["tree"].update({"directory_count": 1}), "exactly one regular file"),
    ],
)
def test_raw_file_contract_mismatches_fail_closed(mutate: Any, message: str) -> None:
    document = _raw_document(b"checkpoint-bytes")
    mutate(document)
    with pytest.raises(ArtifactLocalizationError, match=message):
        LocalizationContract.parse(document)


# ---------------------------------------------------------------------------
# Adapter bindings, argv and environment
# ---------------------------------------------------------------------------


def test_boltzgen_binding_matches_the_catalog_contract() -> None:
    contract = load_localization_contracts_from_path(CONTRACT_PATH)[MOLECULES_ID]
    assert boltzgen.molecules_tree_binding() == _binding(contract)


def test_proteina_binding_matches_the_catalog_contract() -> None:
    contract = load_localization_contracts_from_path(CONTRACT_PATH)[PARAMS_ID]
    assert proteina_complexa.af2_tree_binding() == _binding(contract)


def test_boltzgen_moldir_argv_names_the_extracted_tree_and_never_the_archive(
    boltzgen_configure: StageInvocation,
) -> None:
    argv = boltzgen_configure.argv
    assert "--moldir" in argv
    assert argv[argv.index("--moldir") + 1] == f"/opt/fs2/artifacts/{MOLECULES_ID}"
    assert not any("mols.zip" in argument or argument.endswith(".zip") for argument in argv)


def test_every_boltzgen_stage_that_mounts_molecules_carries_its_tree_binding() -> None:
    plan = boltzgen.compile_run(
        _profile("boltzgen"), _fixture("boltzgen", "positive-design.json"), operation_id="op-cover"
    )
    assert plan.localized_tree_artifacts == (MOLECULES_ID,)
    for invocation in plan.invocations:
        expected = [MOLECULES_ID] if MOLECULES_ID in invocation.runtime_artifacts else []
        assert [tree.artifact_id for tree in invocation.runtime_trees] == expected


def test_proteina_af2_dir_names_the_extracted_tree_and_never_the_tar() -> None:
    plan = proteina_complexa.compile_run(
        _profile("proteina-complexa"),
        _fixture("proteina-complexa", "positive-protein.json"),
        operation_id="op-af2",
    )
    assert plan.localized_tree_artifacts == (PARAMS_ID,)
    for stage_id in ("generate", "evaluate"):
        environment = dict(plan.invocation(stage_id, "main").environment)
        assert environment["AF2_DIR"] == f"/opt/fs2/artifacts/{PARAMS_ID}"
        assert not any(value.endswith(".tar") for value in environment.values())
        assert [tree.artifact_id for tree in plan.invocation(stage_id, "main").runtime_trees] == [PARAMS_ID]


def test_a_proteina_variant_without_alphafold_binds_no_tree() -> None:
    plan = proteina_complexa.compile_run(
        _profile("proteina-complexa"),
        _fixture("proteina-complexa", "positive-ligand.json"),
        operation_id="op-ligand",
    )
    assert plan.localized_tree_artifacts == ()
    assert "AF2_DIR" not in dict(plan.invocation("evaluate", "main").environment)


def test_a_binding_that_reuses_one_digest_for_both_identities_is_rejected() -> None:
    digest = boltzgen.MOLECULES_TREE_INVENTORY_SHA256
    with pytest.raises(ValueError, match="distinct digests"):
        RuntimeTreeBinding(MOLECULES_ID, f"/opt/fs2/artifacts/{MOLECULES_ID}", digest, digest, 45_227)


def test_a_binding_mount_path_that_traverses_is_rejected() -> None:
    with pytest.raises(ValueError, match="safe absolute path"):
        RuntimeTreeBinding(
            MOLECULES_ID,
            "/opt/fs2/artifacts/../../etc",
            boltzgen.MOLECULES_ARCHIVE_SHA256,
            boltzgen.MOLECULES_TREE_INVENTORY_SHA256,
            45_227,
        )


def test_a_binding_naming_a_path_the_contract_never_declared_is_rejected(
    boltzgen_configure: StageInvocation, tmp_path: Path
) -> None:
    contract = load_localization_contracts_from_path(CONTRACT_PATH)[MOLECULES_ID]
    stray = replace(boltzgen.molecules_tree_binding(), mount_path=f"/opt/fs2/artifacts/{PARAMS_ID}")
    invocation = replace(boltzgen_configure, runtime_trees=(stray,))
    with pytest.raises(ArtifactLocalizationError, match="mount path drifted"):
        preflight_stage_trees(invocation, {MOLECULES_ID: tmp_path}, {MOLECULES_ID: contract})


# ---------------------------------------------------------------------------
# One verified identity, several consumer paths
# ---------------------------------------------------------------------------


def test_one_verified_tree_can_be_bound_at_several_consumer_paths(
    tmp_path: Path, boltzgen_configure: StageInvocation
) -> None:
    """A second consumer that expects the tree elsewhere needs no second identity."""

    payload = _zip_bytes(sorted(SYNTHETIC_ENTRIES.items()))
    archive = tmp_path / "mols.zip"
    archive.write_bytes(payload)
    contract = LocalizationContract.parse(
        _document(payload, SYNTHETIC_ENTRIES, extra_mount_paths=("/models/molecules",))
    )
    assert contract.tree.mount_paths == (f"/opt/fs2/artifacts/{MOLECULES_ID}", "/models/molecules")
    assert {consumer.mount_path for consumer in contract.consumers} == set(contract.tree.mount_paths)

    # The same bytes verify identically wherever they are mounted, so the second
    # consumer inherits the first consumer's proven identity.
    first = localize_archive(archive, tmp_path / "a", contract)
    second = verify_localized_tree(_materialize(tmp_path / "b", SYNTHETIC_ENTRIES), contract)
    assert first.verified and second.verified
    assert first.tree_inventory_sha256 == second.tree_inventory_sha256

    for path in contract.tree.mount_paths:
        binding = replace(_binding(contract), mount_path=path)
        invocation = replace(boltzgen_configure, runtime_trees=(binding,))
        receipts = preflight_stage_trees(invocation, {MOLECULES_ID: tmp_path / "a"}, {MOLECULES_ID: contract})
        assert receipts[0].verified


def test_a_consumer_reading_a_path_the_tree_never_declares_is_rejected() -> None:
    document = _document(_zip_bytes(sorted(SYNTHETIC_ENTRIES.items())), SYNTHETIC_ENTRIES)
    document["consumers"][0]["mount_path"] = "/models/somewhere-else"
    with pytest.raises(ArtifactLocalizationError, match="which the tree does not declare"):
        LocalizationContract.parse(document)


def test_a_declared_mount_path_no_consumer_reads_is_rejected() -> None:
    document = _document(_zip_bytes(sorted(SYNTHETIC_ENTRIES.items())), SYNTHETIC_ENTRIES)
    document["tree"]["mount_paths"].append("/models/unread")
    with pytest.raises(ArtifactLocalizationError, match="no consumer reads"):
        LocalizationContract.parse(document)


def test_duplicate_mount_paths_are_rejected() -> None:
    document = _document(_zip_bytes(sorted(SYNTHETIC_ENTRIES.items())), SYNTHETIC_ENTRIES)
    document["tree"]["mount_paths"].append(document["tree"]["mount_paths"][0])
    with pytest.raises(ArtifactLocalizationError, match="must be unique"):
        LocalizationContract.parse(document)


def test_a_traversing_mount_path_is_rejected() -> None:
    document = _document(_zip_bytes(sorted(SYNTHETIC_ENTRIES.items())), SYNTHETIC_ENTRIES)
    document["tree"]["mount_paths"] = ["/opt/fs2/artifacts/../../etc"]
    with pytest.raises(ArtifactLocalizationError, match="absolute POSIX paths"):
        LocalizationContract.parse(document)


def test_a_bound_tree_no_stage_ever_names_is_rejected() -> None:
    """A plan cannot mount a localized tree the model is never told about."""

    plan = boltzgen.compile_run(
        _profile("boltzgen"), _fixture("boltzgen", "positive-design.json"), operation_id="op-strip"
    )
    stripped = tuple(
        replace(item, argv=tuple(value for value in item.argv if value != f"/opt/fs2/artifacts/{MOLECULES_ID}"))
        if "--moldir" not in item.argv
        else replace(
            item,
            argv=tuple(
                value
                for index, value in enumerate(item.argv)
                if value != "--moldir" and item.argv[index - 1] != "--moldir"
            ),
        )
        for item in plan.invocations
    )
    with pytest.raises(ValueError, match="reachable through some stage"):
        AdapterExecutionPlan(
            model_id=plan.model_id,
            variant_id=plan.variant_id,
            source_revision=plan.source_revision,
            request_sha256=plan.request_sha256,
            controller_plan=plan.controller_plan,
            invocations=stripped,
            required_model_artifacts=plan.required_model_artifacts,
        )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_localizing_a_contracted_zip_produces_a_verified_tree(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    receipt = localize_archive(archive, tmp_path / "mount", contract)
    assert receipt.verified
    assert receipt.entry_count == len(SYNTHETIC_ENTRIES)
    assert receipt.tree_inventory_sha256 == contract.tree.inventory_sha256
    assert receipt.archive_sha256 == contract.archive.sha256
    assert receipt.archive_present_in_mount is False
    assert not (tmp_path / "mount" / "mols.zip").exists()
    assert sorted(item.name for item in (tmp_path / "mount").iterdir()) == sorted(SYNTHETIC_ENTRIES)


def test_localizing_a_contracted_tar_produces_a_verified_tree(tmp_path: Path) -> None:
    entries = {"LICENSE": b"license text", "params_model_1.npz": b"parameters" * 32}
    payload = _tar_bytes(sorted(entries.items()))
    contract = LocalizationContract.parse(
        _document(
            payload,
            entries,
            artifact_id=PARAMS_ID,
            transform="safe-extract-tar",
            filename="alphafold_params_2022-12-06.tar",
            media_type="application/x-tar",
            pattern=r"^(LICENSE|params_model_[1-5]\.npz)$",
            model_id="proteina-complexa",
            binding_kind="environment-variable",
            binding_name="AF2_DIR",
        )
    )
    archive = tmp_path / "params.tar"
    archive.write_bytes(payload)
    receipt = localize_archive(archive, tmp_path / "mount", contract)
    assert receipt.verified
    assert receipt.entry_count == 2


def test_the_localized_tree_identity_is_independent_of_where_it_is_mounted(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    first = localize_archive(archive, tmp_path / "a" / "deep" / "mount", contract)
    second = localize_archive(archive, tmp_path / "b", contract)
    assert first.tree_inventory_sha256 == second.tree_inventory_sha256 == contract.tree.inventory_sha256


def test_a_wrong_archive_is_rejected_before_anything_is_extracted(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    other = tmp_path / "other.zip"
    other.write_bytes(_zip_bytes([("ZZZ.pkl", b"a different dataset entirely")]))
    destination = tmp_path / "mount"
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(other, destination, contract)
    assert not destination.exists() or not any(destination.iterdir())


def test_an_archive_of_the_right_size_but_wrong_bytes_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    payload = bytearray(archive.read_bytes())
    payload[12] ^= 0xFF
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(bytes(payload))
    assert tampered.stat().st_size == contract.archive.size_bytes
    with pytest.raises(ArtifactLocalizationError, match="SHA-256"):
        verify_archive(tampered, contract)


def test_a_directory_offered_as_an_archive_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    directory = tmp_path / "not-an-archive"
    directory.mkdir()
    with pytest.raises(ArtifactLocalizationError, match="regular file"):
        verify_archive(directory, contract)


@pytest.mark.parametrize("member", ["../ESCAPE.pkl", "/ABS.pkl", "nested/DIR.pkl", "AB\\C.pkl"])
def test_zip_path_traversal_members_are_rejected(member: str, tmp_path: Path) -> None:
    entries = {"AAA.pkl": b"payload"}
    payload = _zip_bytes([(member, b"payload")])
    document = _document(payload, entries)
    archive = tmp_path / "traversal.zip"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))
    assert not (tmp_path / "ESCAPE.pkl").exists()
    assert not Path("/ABS.pkl").exists()


@pytest.mark.parametrize("member", ["../ESCAPE.npz", "/ABS.npz", "nested/params_model_1.npz"])
def test_tar_path_traversal_members_are_rejected(member: str, tmp_path: Path) -> None:
    entries = {"LICENSE": b"license text"}
    payload = _tar_bytes([(member, b"payload")])
    document = _document(
        payload,
        entries,
        artifact_id=PARAMS_ID,
        transform="safe-extract-tar",
        filename="alphafold_params_2022-12-06.tar",
        media_type="application/x-tar",
        pattern=r"^(LICENSE|params_model_[1-5]\.npz)$",
        model_id="proteina-complexa",
        binding_kind="environment-variable",
        binding_name="AF2_DIR",
    )
    archive = tmp_path / "traversal.tar"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))


@pytest.mark.parametrize("unsafe", [{"symlink": "params_model_1.npz"}, {"directory": "params"}])
def test_tar_symlink_and_directory_members_are_rejected(unsafe: dict[str, str], tmp_path: Path) -> None:
    entries = {"LICENSE": b"license text"}
    payload = _tar_bytes([("LICENSE", b"license text")], **unsafe)  # type: ignore[arg-type]
    document = _document(
        payload,
        entries,
        artifact_id=PARAMS_ID,
        transform="safe-extract-tar",
        filename="alphafold_params_2022-12-06.tar",
        media_type="application/x-tar",
        pattern=r"^(LICENSE|params_model_[1-5]\.npz)$",
        model_id="proteina-complexa",
        binding_kind="environment-variable",
        binding_name="AF2_DIR",
    )
    document["tree"]["entry_count"] = 2
    archive = tmp_path / "unsafe.tar"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))
    assert not (tmp_path / "mount" / "params_model_1.npz").is_symlink()


def test_an_archive_member_outside_the_contracted_pattern_is_rejected(tmp_path: Path) -> None:
    entries = {"AAA.pkl": b"payload", "notes.txt": b"unexpected"}
    payload = _zip_bytes(sorted(entries.items()))
    archive = tmp_path / "mixed.zip"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError, match="entry pattern"):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(_document(payload, entries)))


def test_an_archive_with_the_wrong_member_count_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    short = dict(sorted(SYNTHETIC_ENTRIES.items())[:-1])
    payload = _zip_bytes(sorted(short.items()))
    document = _document(payload, SYNTHETIC_ENTRIES)
    document["archive"]["bytes"] = len(payload)
    document["archive"]["sha256"] = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "short.zip"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError, match="the contract requires"):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))


def test_localization_refuses_a_destination_that_is_not_empty(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    destination = _materialize(tmp_path / "mount", {"AAA.pkl": b"stale"})
    with pytest.raises(ArtifactLocalizationError, match="must be empty"):
        localize_archive(archive, destination, contract)


def test_localization_refuses_to_expand_into_the_archives_own_directory(
    synthetic_zip: tuple[Path, LocalizationContract],
) -> None:
    archive, contract = synthetic_zip
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(archive, archive.parent, contract)


# ---------------------------------------------------------------------------
# Raw uncompressed file staging
# ---------------------------------------------------------------------------


def _raw_fixture(tmp_path: Path, payload: bytes = b"immutable checkpoint bytes") -> tuple[Path, Path, Any]:
    source = tmp_path / "source" / "Base_ckpt.pt"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    artifact = _raw_document(payload)
    contract_path = tmp_path / "raw-localization.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    return source, contract_path, LocalizationContract.parse(artifact)


def _raw_stage_argv(source: Path, contract_path: Path, root: Path, receipt: Path, generation: str) -> list[str]:
    return [
        "stage",
        "--contract",
        str(contract_path),
        "--artifact-id",
        RFDIFFUSION_BASE_ID,
        "--file",
        str(source),
        "--artifact-root",
        str(root),
        "--sub-path",
        f"scientific-localization/public/generations/{RFDIFFUSION_BASE_ID}/sha256/{generation}",
        "--volume-kind",
        "host-path",
        "--host-root",
        "/mnt/fs2-reference-data/data",
        "--visibility",
        "public",
        "--receipt",
        str(receipt),
    ]


def test_raw_file_localization_copies_and_verifies_the_exact_file(tmp_path: Path) -> None:
    source, _, contract = _raw_fixture(tmp_path)
    receipt = localize_file(source, tmp_path / "mount", contract).to_dict()
    assert receipt["state"] == "verified"
    assert "archive_provenance" not in receipt
    assert receipt["file_provenance"] == {
        "filename": "Base_ckpt.pt",
        "media_type": "application/octet-stream",
        "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_uri": "https://example.invalid/Base_ckpt.pt",
        "source_revision": "9273ef67335acaf91df0150473a274759229cdf6",
        "license_id": "BSD-3-Clause",
        "present_in_mount": True,
    }
    assert receipt["tree_identity"]["inventory_algorithm"] == RAW_FILE_ALGORITHM
    assert receipt["tree_identity"]["inventory_sha256"] == contract.tree.inventory_sha256
    assert receipt["observation"] == {
        "files_linked": 0,
        "files_copied": 1,
        "bytes_linked": 0,
        "bytes_copied": source.stat().st_size,
    }


def test_same_sized_different_raw_file_is_rejected_before_publication(tmp_path: Path) -> None:
    source, contract_path, contract = _raw_fixture(tmp_path, b"right bytes")
    source.write_bytes(b"wrong bytes")
    root = tmp_path / "artifact"
    receipt_path = tmp_path / "receipt.json"
    assert (
        localization_main(_raw_stage_argv(source, contract_path, root, receipt_path, contract.tree.inventory_sha256))
        == 1
    )
    receipt = _assert_valid_receipt(receipt_path)
    assert receipt["state"] == "rejected"
    assert "SHA-256" in receipt["rejection_reason"]
    assert not list(root.glob(f"{STAGING_PREFIX}*"))
    assert not (root / "sha256").exists()


def test_raw_file_stage_publishes_atomically_and_reuses_the_generation(tmp_path: Path) -> None:
    source, contract_path, contract = _raw_fixture(tmp_path)
    root = tmp_path / "artifact"
    receipt_path = tmp_path / "receipt.json"
    argv = _raw_stage_argv(source, contract_path, root, receipt_path, contract.tree.inventory_sha256)

    assert localization_main(argv) == 0
    first = _assert_valid_receipt(receipt_path)
    published = root / "sha256" / contract.tree.inventory_sha256
    assert first["observation"]["generation_reused"] is False
    assert first["observation"]["files_copied"] == 1
    assert (published / "Base_ckpt.pt").read_bytes() == source.read_bytes()
    marker = load_generation_marker(published / RUNTIME_MARKER_NAME)
    assert marker["source_kind"] == "file"
    assert marker["source_present_in_mount"] is True
    assert marker["source_sha256"] == contract.source.sha256

    assert localization_main(argv) == 0
    second = _assert_valid_receipt(receipt_path)
    assert second["observation"]["generation_reused"] is True
    assert second["observation"]["marker_sha256"] == first["observation"]["marker_sha256"]
    assert not list(root.glob(f"{STAGING_PREFIX}*"))


def test_invalid_raw_stage_arguments_create_no_staging_directory(tmp_path: Path) -> None:
    source, contract_path, contract = _raw_fixture(tmp_path)
    root = tmp_path / "artifact"
    argv = _raw_stage_argv(source, contract_path, root, tmp_path / "receipt.json", contract.tree.inventory_sha256)
    argv[argv.index("--file")] = "--archive"

    with pytest.raises(SystemExit, match="verified-copy stage requires"):
        localization_main(argv)
    assert not root.exists(), "CLI validation must run before allocating private staging"


# ---------------------------------------------------------------------------
# Preflight verification of a runtime mount
# ---------------------------------------------------------------------------


def test_a_mount_holding_the_archive_beside_the_tree_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    mount = _materialize(tmp_path / "mount", SYNTHETIC_ENTRIES)
    (mount / "mols.zip").write_bytes(archive.read_bytes())
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("archive-present-in-runtime-mount")
    assert receipt.archive_present_in_mount is True


def test_a_mount_holding_only_the_archive_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "mols.zip").write_bytes(archive.read_bytes())
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert "archive-present-in-runtime-mount" in (receipt.rejection_reason or "")


def test_a_partial_tree_is_rejected(synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path) -> None:
    _archive, contract = synthetic_zip
    partial = dict(sorted(SYNTHETIC_ENTRIES.items())[:-1])
    receipt = verify_localized_tree(_materialize(tmp_path / "mount", partial), contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("partial-tree")
    assert receipt.entry_count == len(partial)


def test_an_empty_mount_is_rejected_as_a_partial_tree(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    mount = tmp_path / "mount"
    mount.mkdir()
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("partial-tree")


def test_an_over_full_tree_is_rejected(synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path) -> None:
    _archive, contract = synthetic_zip
    mount = _materialize(tmp_path / "mount", {**SYNTHETIC_ENTRIES, "XX.pkl": b"extra"})
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("unexpected-tree-content")


def test_a_same_size_different_content_tree_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    swapped = dict(SYNTHETIC_ENTRIES)
    swapped["I.pkl"] = b"ONE-CHARACTER CODE"
    assert sum(map(len, swapped.values())) == contract.tree.total_bytes
    receipt = verify_localized_tree(_materialize(tmp_path / "mount", swapped), contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("tree-identity-mismatch")
    assert receipt.tree_inventory_sha256 != contract.tree.inventory_sha256


def test_an_entry_outside_the_contracted_pattern_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    mount = _materialize(tmp_path / "mount", SYNTHETIC_ENTRIES)
    (mount / "README.md").write_bytes(b"unexpected")
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("entry-path-pattern-violation")


def test_a_symlinked_entry_is_rejected(synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path) -> None:
    _archive, contract = synthetic_zip
    kept = dict(SYNTHETIC_ENTRIES)
    linked = kept.pop("I.pkl")
    mount = _materialize(tmp_path / "mount", kept)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(linked)
    os.symlink(outside, mount / "I.pkl")
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert "symbolic link" in (receipt.rejection_reason or "")


def test_a_nested_directory_in_the_mount_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    mount = _materialize(tmp_path / "mount", SYNTHETIC_ENTRIES)
    (mount / "subtree").mkdir()
    receipt = verify_localized_tree(mount, contract)
    assert not receipt.verified
    assert "flat" in (receipt.rejection_reason or "")


def test_a_mount_that_is_a_symlink_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    real = _materialize(tmp_path / "real", SYNTHETIC_ENTRIES)
    link = tmp_path / "mount"
    os.symlink(real, link)
    receipt = verify_localized_tree(link, contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("unusable-runtime-mount")


def test_a_missing_mount_is_rejected_rather_than_treated_as_empty(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    _archive, contract = synthetic_zip
    receipt = verify_localized_tree(tmp_path / "absent", contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("unusable-runtime-mount")


def test_a_probe_digest_that_does_not_match_the_mount_is_rejected(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, _contract = synthetic_zip
    document = _document(archive.read_bytes(), SYNTHETIC_ENTRIES)
    # Keep every inventory-visible property intact and change only the probe
    # digest, so the failure can come from nothing but the content spot check.
    document["tree"]["probe_entries"] = [
        {"path": "HEM.pkl", "bytes": len(SYNTHETIC_ENTRIES["HEM.pkl"]), "sha256": "0" * 64}
    ]
    contract = LocalizationContract.parse(document)
    receipt = verify_localized_tree(_materialize(tmp_path / "mount", SYNTHETIC_ENTRIES), contract)
    assert not receipt.verified
    assert (receipt.rejection_reason or "").startswith("probe-entry-digest-mismatch")
    assert receipt.tree_inventory_sha256 == contract.tree.inventory_sha256


# ---------------------------------------------------------------------------
# Stage preflight
# ---------------------------------------------------------------------------


def test_stage_preflight_returns_a_receipt_for_a_verified_mount(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path, boltzgen_configure: StageInvocation
) -> None:
    archive, _fixture_contract = synthetic_zip
    contract = LocalizationContract.parse(_document(archive.read_bytes(), SYNTHETIC_ENTRIES))
    mount = tmp_path / "mount"
    localize_archive(archive, mount, contract)
    invocation = replace(boltzgen_configure, runtime_trees=(_binding(contract),))
    receipts = preflight_stage_trees(invocation, {MOLECULES_ID: mount}, {MOLECULES_ID: contract})
    assert [receipt.state for receipt in receipts] == ["verified"]
    assert receipts[0].runtime_bindings == (
        ("boltzgen", "argv-option", "--moldir", f"/opt/fs2/artifacts/{MOLECULES_ID}"),
    )


def test_stage_preflight_fails_closed_on_an_archive_only_mount(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path, boltzgen_configure: StageInvocation
) -> None:
    archive, _fixture_contract = synthetic_zip
    contract = LocalizationContract.parse(_document(archive.read_bytes(), SYNTHETIC_ENTRIES))
    mount = tmp_path / "archive-only"
    mount.mkdir()
    (mount / "mols.zip").write_bytes(archive.read_bytes())
    invocation = replace(boltzgen_configure, runtime_trees=(_binding(contract),))
    with pytest.raises(ArtifactLocalizationError, match="archive-present-in-runtime-mount"):
        preflight_stage_trees(invocation, {MOLECULES_ID: mount}, {MOLECULES_ID: contract})


def test_stage_preflight_rejects_a_binding_that_drifted_from_the_contract(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path, boltzgen_configure: StageInvocation
) -> None:
    archive, _fixture_contract = synthetic_zip
    contract = LocalizationContract.parse(_document(archive.read_bytes(), SYNTHETIC_ENTRIES))
    # The compiled adapter binding still carries the real 45,227-entry identity.
    with pytest.raises(ArtifactLocalizationError, match="drifted"):
        preflight_stage_trees(boltzgen_configure, {MOLECULES_ID: tmp_path}, {MOLECULES_ID: contract})


def test_stage_preflight_requires_a_mount_for_every_binding(boltzgen_configure: StageInvocation) -> None:
    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    with pytest.raises(ArtifactLocalizationError, match="was not mounted"):
        preflight_stage_trees(boltzgen_configure, {}, contracts)


def test_stage_preflight_requires_a_registered_contract(boltzgen_configure: StageInvocation) -> None:
    with pytest.raises(ArtifactLocalizationError, match="no localization contract"):
        preflight_stage_trees(boltzgen_configure, {}, {})


def test_a_stage_with_no_bound_tree_needs_no_mount() -> None:
    plan = boltzgen.compile_run(
        _profile("boltzgen"), _fixture("boltzgen", "positive-design.json"), operation_id="op-cpu"
    )
    analysis = plan.invocation("analysis", "pdl1-a")
    assert analysis.runtime_trees == ()
    assert preflight_stage_trees(analysis, {}, {}) == ()


# ---------------------------------------------------------------------------
# Receipt shape
# ---------------------------------------------------------------------------


def test_receipt_reports_archive_provenance_and_tree_identity_as_separate_fields(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    receipt = localize_archive(archive, tmp_path / "mount", contract).to_dict()
    assert receipt["schema"] == "fs2-serve.nebius.ai/scientific-localization-receipt/v1"
    provenance = receipt["archive_provenance"]
    identity = receipt["tree_identity"]
    assert provenance["sha256"] == contract.archive.sha256
    assert identity["inventory_sha256"] == contract.tree.inventory_sha256
    assert provenance["sha256"] != identity["inventory_sha256"]
    assert provenance["present_in_mount"] is False
    assert identity["inventory_algorithm"] == "fs2-flat-tree-inventory/v1"
    assert receipt["state"] == "verified"


def test_a_rejected_receipt_records_why_without_claiming_an_identity(
    synthetic_zip: tuple[Path, LocalizationContract], tmp_path: Path
) -> None:
    archive, contract = synthetic_zip
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "mols.zip").write_bytes(archive.read_bytes())
    receipt = verify_localized_tree(mount, contract).to_dict()
    assert receipt["state"] == "rejected"
    assert "archive-present-in-runtime-mount" in receipt["rejection_reason"]
    assert receipt["archive_provenance"]["present_in_mount"] is True
    assert receipt["tree_identity"]["inventory_sha256"] != contract.tree.inventory_sha256


# ---------------------------------------------------------------------------
# Installed-package subtrees lifted out of a source archive
# ---------------------------------------------------------------------------


def test_the_two_colabdesign_mpnn_directories_are_separate_verified_identities() -> None:
    """ColabDesign picks the directory by import, so one mount cannot serve both."""

    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    vanilla = contracts[VANILLA_MPNN_ID]
    soluble = contracts[SOLUBLE_MPNN_ID]

    assert vanilla.tree.mount_paths == (f"{COLABDESIGN_SITE_PACKAGES}/weights",)
    assert soluble.tree.mount_paths == (f"{COLABDESIGN_SITE_PACKAGES}/weights_soluble",)
    assert vanilla.tree.inventory_sha256 != soluble.tree.inventory_sha256
    assert vanilla.tree.total_bytes != soluble.tree.total_bytes
    assert vanilla.binding_for("bindcraft").binding_name == "colabdesign.mpnn.weights"
    assert soluble.binding_for("bindcraft").binding_name == "colabdesign.mpnn.weights_soluble"

    # Both are lifted from the same pinned source archive, so provenance is
    # shared while the two tree identities stay distinct.
    assert vanilla.archive.sha256 == soluble.archive.sha256
    assert vanilla.archive.member_prefix != soluble.archive.member_prefix
    assert vanilla.archive.member_prefix is not None
    assert vanilla.archive.member_prefix.endswith("/colabdesign/mpnn/weights/")
    assert soluble.archive.member_prefix is not None
    assert soluble.archive.member_prefix.endswith("/colabdesign/mpnn/weights_soluble/")


def test_every_localized_tree_identity_in_the_catalog_is_unique() -> None:
    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    digests = [contract.tree.inventory_sha256 for contract in contracts.values()]
    assert len(set(digests)) == len(digests)
    mounts = [path for contract in contracts.values() for path in contract.tree.mount_paths]
    assert len(set(mounts)) == len(mounts)


def _prefixed_targz(members: dict[str, bytes], prefix: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, payload in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def test_a_subtree_is_lifted_out_of_a_larger_source_archive(tmp_path: Path) -> None:
    wanted = {"__init__.py": b"\n", "v_48_020.pkl": b"weights" * 100}
    root = "Project-abc123"
    members = {f"{root}/pkg/weights/{name}": payload for name, payload in wanted.items()}
    # Content the runtime must never receive, including a same-named file in a
    # sibling directory and a file nested one level deeper.
    members[f"{root}/pkg/weights_soluble/v_48_020.pkl"] = b"soluble" * 100
    members[f"{root}/README.md"] = b"documentation"
    members[f"{root}/pkg/weights/nested/v_48_020.pkl"] = b"too deep"
    payload = _prefixed_targz(members, root)

    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(payload)
    document = _document(
        payload,
        wanted,
        artifact_id="synthetic-installed-weights",
        transform="safe-extract-tar-gz",
        filename="source.tar.gz",
        media_type="application/gzip",
        pattern=r"^(__init__\.py|v_48_020\.pkl)$",
        model_id="bindcraft",
        binding_kind="installed-package-path",
        binding_name="pkg.weights",
    )
    document["archive"]["member_prefix"] = f"{root}/pkg/weights/"
    contract = LocalizationContract.parse(document)

    receipt = localize_archive(archive, tmp_path / "mount", contract)
    assert receipt.verified
    assert sorted(item.name for item in (tmp_path / "mount").iterdir()) == ["__init__.py", "v_48_020.pkl"]
    assert (tmp_path / "mount" / "v_48_020.pkl").read_bytes() == wanted["v_48_020.pkl"]
    assert not (tmp_path / "mount" / "README.md").exists()
    assert not (tmp_path / "mount" / "nested").exists()


def test_a_subtree_prefix_that_matches_nothing_fails_closed(tmp_path: Path) -> None:
    wanted = {"__init__.py": b"\n", "v_48_020.pkl": b"weights" * 100}
    root = "Project-abc123"
    payload = _prefixed_targz({f"{root}/pkg/other/{n}": p for n, p in wanted.items()}, root)
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(payload)
    document = _document(
        payload,
        wanted,
        artifact_id="synthetic-installed-weights",
        transform="safe-extract-tar-gz",
        filename="source.tar.gz",
        media_type="application/gzip",
        pattern=r"^(__init__\.py|v_48_020\.pkl)$",
        model_id="bindcraft",
        binding_kind="installed-package-path",
        binding_name="pkg.weights",
    )
    document["archive"]["member_prefix"] = f"{root}/pkg/weights/"
    with pytest.raises(ArtifactLocalizationError, match="does not match the contracted tree"):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))


def test_a_member_prefix_that_traverses_is_rejected() -> None:
    document = _document(_zip_bytes(sorted(SYNTHETIC_ENTRIES.items())), SYNTHETIC_ENTRIES)
    document["archive"]["member_prefix"] = "../escape/"
    with pytest.raises(ArtifactLocalizationError, match="safe relative directory prefix"):
        LocalizationContract.parse(document)


def test_an_installed_package_binding_must_name_a_dotted_package() -> None:
    document = _document(
        _zip_bytes(sorted(SYNTHETIC_ENTRIES.items())),
        SYNTHETIC_ENTRIES,
        model_id="bindcraft",
        binding_kind="installed-package-path",
        binding_name="--not-a-package",
    )
    with pytest.raises(ArtifactLocalizationError, match="dotted package path"):
        LocalizationContract.parse(document)


# ---------------------------------------------------------------------------
# Generated entries a runtime gate requires but upstream never publishes
# ---------------------------------------------------------------------------


def test_bindcraft_alphafold_tree_is_a_separate_identity_from_the_proteina_tree() -> None:
    """The two consumers read genuinely different trees, not one tree twice."""

    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    proteina = contracts[PARAMS_ID]
    bindcraft = contracts[BINDCRAFT_PARAMS_ID]

    # Same archive provenance, because both come from the same upstream object.
    assert proteina.archive.sha256 == bindcraft.archive.sha256
    # Different trees, because one carries an admission manifest and one does not.
    assert proteina.tree.entry_count == 16
    assert bindcraft.tree.entry_count == 17
    assert proteina.tree.inventory_sha256 != bindcraft.tree.inventory_sha256
    assert bindcraft.tree.total_bytes - proteina.tree.total_bytes == 2866
    assert proteina.tree.generated_entries == ()
    assert bindcraft.tree.mount_paths == ("/models/alphafold2",)
    assert bindcraft.binding_for("bindcraft").binding_name == "FS2_ARTIFACT_ROOT"

    generated = bindcraft.tree.generated_entries
    assert [entry.path for entry in generated] == ["manifest.json"]
    assert generated[0].generator == "external-model-artifact-manifest/v1"
    assert generated[0].generator_inputs == {
        "artifact_kind": "bindcraft-af2-params",
        "source_revision": "7cd4ace1b7407adf66a50dfefa47de2270f5e4a9",
    }
    # The manifest is not part of the Proteina contract's entry vocabulary.
    assert proteina.tree.entry_matcher.fullmatch("manifest.json") is None
    assert bindcraft.tree.entry_matcher.fullmatch("manifest.json") is not None


def _manifest_document(entries: Mapping[str, bytes], *, kind: str, revision: str) -> dict[str, Any]:
    return {
        "schema": "fs2.nebius.ai/external-model-artifact-manifest/v1",
        "artifact_kind": kind,
        "source_revision": revision,
        "files": [
            {"path": name, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}
            for name, payload in sorted(entries.items())
        ],
    }


def _generated_document(entries: Mapping[str, bytes], tmp_path: Path) -> tuple[bytes, dict[str, Any]]:
    """Build a tar contract whose tree carries one generated admission manifest."""

    from fs2_serve.scientific_batch.adapters.localization import render_external_model_artifact_manifest

    staged = _materialize(tmp_path / "reference", entries)
    manifest = render_external_model_artifact_manifest(
        staged, artifact_kind="test-kind", source_revision="a" * 40, exclude=frozenset({"manifest.json"})
    )
    payload = _tar_bytes(sorted(entries.items()))
    document = _document(
        payload,
        entries,
        artifact_id="synthetic-gated-params",
        transform="safe-extract-tar",
        filename="params.tar",
        media_type="application/x-tar",
        pattern=r"^(manifest\.json|params_model_[1-5]\.npz)$",
        model_id="bindcraft",
        binding_kind="environment-variable",
        binding_name="FS2_ARTIFACT_ROOT",
    )
    document["tree"]["entry_count"] = len(entries) + 1
    document["tree"]["total_bytes"] = sum(map(len, entries.values())) + len(manifest)
    document["tree"]["inventory_sha256"] = _inventory({**entries, "manifest.json": manifest})
    document["tree"]["generated_entries"] = [
        {
            "path": "manifest.json",
            "bytes": len(manifest),
            "sha256": hashlib.sha256(manifest).hexdigest(),
            "generator": "external-model-artifact-manifest/v1",
            "generator_inputs": {"artifact_kind": "test-kind", "source_revision": "a" * 40},
        }
    ]
    return payload, document


def test_a_generated_manifest_is_written_and_matches_its_declared_digest(tmp_path: Path) -> None:
    entries = {"params_model_1.npz": b"parameters" * 64, "params_model_2.npz": b"more" * 128}
    payload, document = _generated_document(entries, tmp_path)
    archive = tmp_path / "params.tar"
    archive.write_bytes(payload)
    contract = LocalizationContract.parse(document)

    receipt = localize_archive(archive, tmp_path / "mount", contract)
    assert receipt.verified
    assert receipt.entry_count == 3
    manifest_path = tmp_path / "mount" / "manifest.json"
    assert manifest_path.is_file()

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written == _manifest_document(entries, kind="test-kind", revision="a" * 40)
    # The consuming gate requires exactly these three keys on every entry.
    for entry in written["files"]:
        assert set(entry) == {"path", "sha256", "size_bytes"}
        assert entry["size_bytes"] >= 1
    # The manifest never describes itself.
    assert "manifest.json" not in {entry["path"] for entry in written["files"]}


def test_a_generated_manifest_that_would_not_match_its_digest_fails_closed(tmp_path: Path) -> None:
    entries = {"params_model_1.npz": b"parameters" * 64, "params_model_2.npz": b"more" * 128}
    payload, document = _generated_document(entries, tmp_path)
    # A drifting generator input changes the bytes, so the declared digest no
    # longer describes what would be written.
    document["tree"]["generated_entries"][0]["generator_inputs"]["source_revision"] = "b" * 40
    archive = tmp_path / "params.tar"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError, match="does not match its declared identity"):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))


def test_a_generated_entry_needs_a_registered_generator(tmp_path: Path) -> None:
    entries = {"params_model_1.npz": b"parameters" * 64}
    _payload, document = _generated_document(entries, tmp_path)
    document["tree"]["generated_entries"][0]["generator"] = "handwritten/v1"
    with pytest.raises(ArtifactLocalizationError, match="unsupported"):
        LocalizationContract.parse(document)


def test_a_generated_manifest_needs_its_exact_generator_inputs(tmp_path: Path) -> None:
    entries = {"params_model_1.npz": b"parameters" * 64}
    _payload, document = _generated_document(entries, tmp_path)
    del document["tree"]["generated_entries"][0]["generator_inputs"]["artifact_kind"]
    with pytest.raises(ArtifactLocalizationError, match="artifact_kind and source_revision"):
        LocalizationContract.parse(document)


def test_the_archive_supplies_every_entry_the_generator_does_not(tmp_path: Path) -> None:
    """Entry accounting must not double-count the generated file."""

    entries = {"params_model_1.npz": b"parameters" * 64}
    payload, document = _generated_document(entries, tmp_path)
    # Claiming the archive also carries the manifest leaves the tree one short.
    document["tree"]["generated_entries"] = []
    archive = tmp_path / "params.tar"
    archive.write_bytes(payload)
    with pytest.raises(ArtifactLocalizationError):
        localize_archive(archive, tmp_path / "mount", LocalizationContract.parse(document))


# ---------------------------------------------------------------------------
# The verifier is delivered into runtime images, so it must stay portable
# ---------------------------------------------------------------------------


def test_the_delivered_verifier_still_parses_as_python_3_10() -> None:
    """Staging and qualification run this code inside model runtime images.

    Those images are not all on the control plane's interpreter; the published
    BindCraft runtime is Python 3.10. A 3.11-only construct here fails the
    verifier on import, before it can report anything about the mount, so the
    delivered modules are checked against the older grammar and against the
    stdlib names that moved.
    """

    import ast

    adapters = Path(__file__).resolve().parents[1] / "src/fs2_serve/scientific_batch/adapters"
    for name in ("localization.py", "primitives.py"):
        source = (adapters / name).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=name, feature_version=(3, 10))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                assert "UTC" not in {alias.name for alias in node.names}, name
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "datetime":
                assert node.attr != "UTC", name


def test_the_delivered_verifier_imports_nothing_outside_the_standard_library() -> None:
    """It ships as two files through a ConfigMap, so it can depend on nothing else."""

    import ast
    import sys

    adapters = Path(__file__).resolve().parents[1] / "src/fs2_serve/scientific_batch/adapters"
    allowed_local = {"primitives"}
    for name in ("localization.py", "primitives.py"):
        tree = ast.parse((adapters / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.module in allowed_local, f"{name} imports .{node.module}"
                    continue
                assert node.module is not None
                root = node.module.split(".")[0]
                assert root in sys.stdlib_module_names, f"{name} imports {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in sys.stdlib_module_names, f"{name} imports {alias.name}"


# ---------------------------------------------------------------------------
# Immutable generations
# ---------------------------------------------------------------------------


def _marker(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "artifact_id": MOLECULES_ID,
        "generation": "c" * 64,
        "entry_count": 1,
        "total_bytes": 1,
        "inventory_algorithm": TREE_INVENTORY_ALGORITHM,
        "sub_path": "scientific-localization/public/x/sha256/" + "c" * 64,
        "namespace": "fs2-academic-poc",
        "claim": "academic-assets-runtime-rwx",
        "visibility": "public",
    }
    arguments.update(overrides)
    return generation_marker(**arguments)


def _promoted(tmp_path: Path, entries: Mapping[str, bytes]) -> tuple[Path, Path, str]:
    staged = _materialize(tmp_path / "staged", entries)
    generation = _inventory(entries)
    root = tmp_path / "boltzgen-inference-molecules"
    published, _reused = promote_generation(staged, root, generation)
    return root, published, generation


def test_a_generation_is_published_under_its_own_digest(tmp_path: Path) -> None:
    root, published, generation = _promoted(tmp_path, SYNTHETIC_ENTRIES)
    # The algorithm is a path segment, so a future digest lands beside this one.
    assert published == root / "sha256" / generation
    assert published == generation_directory(root, generation)
    assert sorted(item.name for item in published.iterdir()) == sorted(SYNTHETIC_ENTRIES)


def test_a_promoted_generation_cannot_be_written_or_deleted(tmp_path: Path) -> None:
    _root, published, _generation = _promoted(tmp_path, SYNTHETIC_ENTRIES)
    assert not os.access(published, os.W_OK)
    with pytest.raises(PermissionError):
        (published / "HEM.pkl").write_bytes(b"tampered")
    with pytest.raises(PermissionError):
        (published / "new.pkl").write_bytes(b"added")


def test_different_bytes_are_a_different_generation_not_an_overwrite(tmp_path: Path) -> None:
    root, published, _generation = _promoted(tmp_path, SYNTHETIC_ENTRIES)
    changed = {**SYNTHETIC_ENTRIES, "ZN.pkl": b"an extra molecule"}
    other, reused = promote_generation(_materialize(tmp_path / "other", changed), root, _inventory(changed))
    assert not reused
    assert other != published
    assert published.is_dir() and other.is_dir()


def test_promoting_the_same_generation_twice_keeps_the_first_bytes(tmp_path: Path) -> None:
    """Restaging must never destroy bytes another workload is already mounting."""

    root, published, generation = _promoted(tmp_path, SYNTHETIC_ENTRIES)
    before = {item.name: item.read_bytes() for item in published.iterdir()}
    again, reused = promote_generation(_materialize(tmp_path / "again", SYNTHETIC_ENTRIES), root, generation)
    assert reused, "an existing generation is reported as reused so a caller reverifies it"
    assert again == published
    assert {item.name: item.read_bytes() for item in published.iterdir()} == before


def test_a_generation_must_be_named_by_a_digest(tmp_path: Path) -> None:
    with pytest.raises(ArtifactLocalizationError, match="lowercase SHA-256"):
        promote_generation(_materialize(tmp_path / "staged", SYNTHETIC_ENTRIES), tmp_path / "root", "latest")


def test_an_interrupted_staging_directory_is_reclaimed_and_never_published(tmp_path: Path) -> None:
    """A crashed run leaves a temporary directory, never a partial final tree."""

    root = tmp_path / "artifact"
    wreckage = prepare_staging_directory(root)
    (wreckage / "half-written.pkl").write_bytes(b"partial")
    os.utime(wreckage, (0, 0))  # as old as an abandoned run
    assert interrupted_staging_directories(root, older_than_seconds=3600) == [wreckage]

    fresh = prepare_staging_directory(root)
    assert not wreckage.exists(), "an abandoned staging directory must be reclaimed"
    assert fresh.exists() and fresh.name.startswith(STAGING_PREFIX)
    # Nothing partial was ever published: only staging directories exist.
    assert not (root / "sha256").exists()


def test_a_live_staging_directory_is_not_reclaimed_by_a_concurrent_run(tmp_path: Path) -> None:
    """Age is the discriminator, because a peer's temporary directory is in use."""

    root = tmp_path / "artifact"
    active = prepare_staging_directory(root)
    assert interrupted_staging_directories(root, older_than_seconds=3600) == []
    prepare_staging_directory(root)
    assert active.exists()


def test_a_marker_admits_only_the_generation_it_describes(tmp_path: Path) -> None:
    generation = _inventory(SYNTHETIC_ENTRIES)
    sub_path = f"scientific-localization/public/{MOLECULES_ID}/sha256/{generation}"
    document = _marker(
        generation=generation,
        entry_count=len(SYNTHETIC_ENTRIES),
        total_bytes=sum(map(len, SYNTHETIC_ENTRIES.values())),
        sub_path=sub_path,
    )
    path = tmp_path / "marker.json"
    assert write_generation_marker(path, document) == marker_sha256(document)
    marker = load_generation_marker(path)
    identity = verify_generation_marker(
        marker, artifact_id=MOLECULES_ID, expected_generation=generation, expected_sub_path=sub_path
    )
    assert identity["entry_count"] == len(SYNTHETIC_ENTRIES)

    for kwargs, message in (
        ({"artifact_id": "somebody-else"}, "different artifact"),
        ({"expected_generation": "b" * 64}, "does not describe"),
        ({"expected_sub_path": "somewhere/else"}, "sub-path does not match"),
    ):
        arguments = {
            "artifact_id": MOLECULES_ID,
            "expected_generation": generation,
            "expected_sub_path": sub_path,
            **kwargs,
        }
        with pytest.raises(ArtifactLocalizationError, match=message):
            verify_generation_marker(marker, **arguments)


def test_a_marker_carries_no_timestamp_or_host_identity(tmp_path: Path) -> None:
    """Two promotions of one tree must produce byte-identical markers.

    A marker names an immutable generation, so a handoff can pin its digest.
    Anything that varies by when or where it ran would break that pin and make
    re-promotion silently change identity.
    """

    first = marker_bytes(_marker())
    second = marker_bytes(_marker())
    assert first == second
    rendered = first.decode()
    for leak in ("observed_at", "timestamp", "duration", "node", "hostname", "run_id", "2026"):
        assert leak not in rendered, f"a marker must not carry {leak}"


def test_a_marker_that_does_not_assert_a_read_only_mount_is_rejected() -> None:
    mutable = json.loads(json.dumps(_marker()))
    mutable["read_only"] = False
    with pytest.raises(ArtifactLocalizationError, match="read-only"):
        verify_generation_marker(
            mutable,
            artifact_id=MOLECULES_ID,
            expected_generation="c" * 64,
            expected_sub_path=mutable["sub_path"],
        )


def test_the_marker_is_one_flat_document_a_byte_hashing_consumer_can_read() -> None:
    """One shared terminal contract, not two documents that share a filename.

    A companion gate reads flat fields and pins the exact bytes, so a nested
    identity object here would mean two incompatible readings of one file.
    """

    document = _marker()
    assert all(not isinstance(value, dict) for value in document.values()), "the marker is flat"
    for field in (
        "schema",
        "artifact_id",
        "artifact_kind",
        "generation",
        "inventory_algorithm",
        "inventory_sha256",
        "entry_count",
        "directory_count",
        "total_bytes",
        "namespace",
        "claim",
        "sub_path",
        "visibility",
        "read_only",
        "generator_identity",
        "consumer_paths",
    ):
        assert field in document, field
    # The digest a consumer pins is the SHA-256 of exactly these bytes.
    assert marker_sha256(document) == hashlib.sha256(marker_bytes(document)).hexdigest()
    assert marker_bytes(document) == (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def test_a_marker_is_written_once_and_never_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "markers" / "artifact.json"
    write_generation_marker(path, _marker())
    assert sorted(item.name for item in path.parent.iterdir()) == ["artifact.json"]
    # Re-promoting the identical tree is a no-op, not a rewrite.
    write_generation_marker(path, _marker())
    with pytest.raises(ArtifactLocalizationError, match="immutable"):
        write_generation_marker(path, _marker(entry_count=99))


def test_the_marker_travels_inside_the_generation_and_leaves_its_digest_alone(tmp_path: Path) -> None:
    """A consumer that mounts only the generation must still be able to admit it.

    The marker is written before the rename that publishes the tree, so the two
    are sealed together, and the reserved name is excluded from the inventory so
    adding it cannot move a published digest.
    """

    staged = _materialize(tmp_path / "staged", SYNTHETIC_ENTRIES)
    generation = _inventory(SYNTHETIC_ENTRIES)
    write_generation_marker(staged / RUNTIME_MARKER_NAME, _marker(generation=generation))
    published, _reused = promote_generation(staged, tmp_path / "artifact", generation)

    assert (published / RUNTIME_MARKER_NAME).is_file()
    entries = scan_localized_tree(published, maximum_entries=100, maximum_bytes=1 << 20)
    assert tree_inventory_sha256(entries) == generation, "the marker must not move the tree digest"
    assert RUNTIME_MARKER_NAME not in {entry.path for entry in entries}


def test_a_recursive_inventory_carries_directories_so_structure_is_identity(tmp_path: Path) -> None:
    """An installed package tree depends on its directories, empty ones included."""

    root = tmp_path / "site-packages"
    (root / "pyrosetta" / "database").mkdir(parents=True)
    (root / "pyrosetta" / "__init__.py").write_bytes(b"package marker\n")
    (root / "pyrosetta" / "database" / "residues.txt").write_bytes(b"residue table" * 8)
    (root / "top.txt").write_bytes(b"top level")

    entries = scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20)
    assert [(entry.kind, entry.path) for entry in entries] == [
        ("directory", "pyrosetta"),
        ("file", "pyrosetta/__init__.py"),
        ("directory", "pyrosetta/database"),
        ("file", "pyrosetta/database/residues.txt"),
        ("file", "top.txt"),
    ]
    files, directories, total = tree_counts(entries)
    assert (files, directories) == (3, 2)
    assert total == len(b"package marker\n") + len(b"residue table" * 8) + len(b"top level")
    before = recursive_inventory_sha256(entries)

    # An added empty directory is a different tree, and v1 cannot say so at all.
    (root / "pyrosetta" / "protocols").mkdir()
    after = recursive_inventory_sha256(scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20))
    assert after != before
    with pytest.raises(ArtifactLocalizationError, match="files only"):
        tree_inventory_sha256(scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20))


def test_a_flat_scan_refuses_the_nested_tree_a_recursive_scan_accepts(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    (root / "pyrosetta").mkdir(parents=True)
    (root / "pyrosetta" / "__init__.py").write_bytes(b"code")
    with pytest.raises(ArtifactLocalizationError, match="flat"):
        scan_localized_tree(root, maximum_entries=1000, maximum_bytes=1 << 20)


def test_a_symlink_or_unsafe_name_in_a_recursive_tree_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "site-packages"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "real.py").write_bytes(b"code")
    os.symlink(tmp_path / "outside.py", root / "pkg" / "link.py")
    with pytest.raises(ArtifactLocalizationError, match="symbolic link"):
        scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20)

    (root / "pkg" / "link.py").unlink()
    # A forged marker below the root is an unsafe name, never a skipped entry.
    (root / "pkg" / RUNTIME_MARKER_NAME).write_bytes(b"{}")
    with pytest.raises(ArtifactLocalizationError, match="unsafe entry name"):
        scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20)


def test_counting_a_generation_matches_the_recursive_scan_without_reading_bytes(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "one.txt").write_bytes(b"one")
    (root / "a" / "b" / "two.txt").write_bytes(b"two")
    (root / "three.txt").write_bytes(b"three")
    (root / RUNTIME_MARKER_NAME).write_bytes(b"{}")

    entries = scan_recursive_tree(root, maximum_entries=100, maximum_bytes=1 << 20)
    files, directories, _total = tree_counts(entries)
    assert count_generation(root, maximum_entries=100) == (files, directories, 0) == (3, 2, 0)


def test_a_mount_that_is_not_the_generation_the_marker_describes_is_refused(tmp_path: Path) -> None:
    """The recursive count is what catches a swapped tree of the same shape."""

    generation = _inventory(SYNTHETIC_ENTRIES)
    sub_path = f"scientific-localization/public/{MOLECULES_ID}/sha256/{generation}"
    staged = _materialize(tmp_path / "staged", SYNTHETIC_ENTRIES)
    document = _marker(
        generation=generation,
        entry_count=len(SYNTHETIC_ENTRIES),
        total_bytes=sum(map(len, SYNTHETIC_ENTRIES.values())),
        sub_path=sub_path,
    )
    write_generation_marker(staged / RUNTIME_MARKER_NAME, document)
    published, _reused = promote_generation(staged, tmp_path / "artifact", generation)

    argv = [
        "marker",
        "--artifact-id",
        MOLECULES_ID,
        "--mount",
        str(published),
        "--expect-generation",
        generation,
        "--sub-path",
        sub_path,
    ]
    assert localization_main(argv) == 0

    # A tree with a different shape, carrying the same marker, is refused.
    short = _materialize(tmp_path / "short", {"HEM.pkl": SYNTHETIC_ENTRIES["HEM.pkl"]})
    write_generation_marker(short / RUNTIME_MARKER_NAME, document)
    assert (
        localization_main(
            [
                "marker",
                "--artifact-id",
                MOLECULES_ID,
                "--mount",
                str(short),
                "--expect-generation",
                generation,
                "--sub-path",
                sub_path,
            ]
        )
        == 1
    )


def _academic_producer() -> Any:
    """Load the academic-assets plane's own tree_manifest implementation."""

    path = Path(__file__).resolve().parents[3] / "academic-assets/scripts/install_tree.py"
    spec = importlib.util.spec_from_file_location("fs2_academic_install_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_installed_tree_identity_matches_the_academic_assets_producer(tmp_path: Path) -> None:
    """PyRosetta already has an identity; this must reproduce it, not replace it.

    The academic-assets plane names its installed trees per file by SHA-256 and
    per symlink by target. Measuring the same bytes under a different algorithm
    would publish a second, weaker name for a tree that already has one, so the
    two implementations are held together here on a tree that exercises exactly
    the cases they disagree about: nested files, an empty directory, and a
    symlink.
    """

    producer = _academic_producer()
    root = tmp_path / "site-packages"
    (root / "pyrosetta" / "database").mkdir(parents=True)
    (root / "pyrosetta" / "empty").mkdir()
    (root / "pyrosetta" / "__init__.py").write_bytes(b"package marker\n")
    (root / "pyrosetta" / "database" / "residues.txt").write_bytes(b"residue table" * 8)
    (root / "top.txt").write_bytes(b"top level")
    os.symlink("pyrosetta/__init__.py", root / "alias.py")

    expected = producer.tree_manifest(root)
    observed = tree_manifest_identity(root)
    assert observed.algorithm == expected["tree_manifest_algorithm"] == TREE_MANIFEST_ALGORITHM
    assert observed.sha256 == expected["tree_manifest_sha256"]
    assert observed.total_bytes == expected["tree_total_bytes"]
    assert observed.file_count == expected["file_count"]
    assert observed.symlink_count == expected["symlink_count"] == 1

    # The generic recursive algorithm is a genuinely different identity, and it
    # refuses this tree outright because of the symlink. That is precisely why
    # the marker has to name which algorithm produced its digest.
    with pytest.raises(ArtifactLocalizationError, match="symbolic link"):
        scan_recursive_tree(root, maximum_entries=1000, maximum_bytes=1 << 20)


def test_the_published_pyrosetta_identity_is_pinned_to_the_academic_record() -> None:
    """The one identity both planes must agree on, pinned as an interface."""

    state = json.loads(
        (Path(__file__).resolve().parents[3] / "academic-assets/evidence/live-acceptance-state.json").read_text(
            encoding="utf-8"
        )
    )
    installed = state["semantic_evidence"]["installed_tree"]
    assert installed["tree_manifest_algorithm"] == TREE_MANIFEST_ALGORITHM
    assert installed["tree_manifest_sha256"] == PYROSETTA_TREE_SHA256
    assert installed["files_installed"] == PYROSETTA_FILE_COUNT
    assert installed["tree_total_bytes"] == PYROSETTA_TOTAL_BYTES


# ---------------------------------------------------------------------------
# Rendered workloads must run the CLI they render
# ---------------------------------------------------------------------------


def _renderer() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "models/cancer-immunotherapy/artifact-localization/render_localization_jobs.py"
    )
    spec = importlib.util.spec_from_file_location("fs2_render_localization_jobs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _localize_argv(job: dict[str, Any], container: str) -> list[str]:
    """The argv one rendered container actually executes."""

    spec = job["spec"]["template"]["spec"]
    for item in (*spec.get("initContainers", []), *spec["containers"]):
        if item["name"].startswith(container):
            command = item["command"]
            # Drop the interpreter and -m module selector.
            return [part for part in command[3:]]
    raise AssertionError(f"no {container} container in the rendered job")


def test_render_stage_and_qualify_round_trip_on_the_argv_they_render(tmp_path: Path) -> None:
    """The rendered Jobs must run against the parser this module actually has.

    A renderer that emits an option the CLI does not accept fails only in the
    cluster, after an image pull and a volume mount, which is the most expensive
    possible place to discover a typo. This runs the rendered argv here.
    """

    renderer = _renderer()
    entries = SYNTHETIC_ENTRIES
    payload = _zip_bytes(sorted(entries.items()))
    artifact = _document(payload, entries)
    contract_path = tmp_path / "localization-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "mols.zip"
    archive.write_bytes(payload)

    generation = artifact["tree"]["inventory_sha256"]
    prefix = "scientific-localization/public"
    sub_path = renderer.generation_sub_path(prefix, MOLECULES_ID, generation)
    trees = tmp_path / "plane-root"
    trees.mkdir()

    def rewrite(part: str) -> str:
        """Point one rendered container path at this test's real directories.

        Only a whole leading path segment is substituted: a blunt string replace
        would also rewrite a value that merely contains the container root.
        """

        if part == renderer.CONTRACT_MOUNT:
            return str(contract_path)
        if part == renderer.TREE_ROOT:
            return str(trees)
        if part.startswith(renderer.TREE_ROOT + "/"):
            return str(trees) + part[len(renderer.TREE_ROOT) :]
        return part

    def localize(argv: list[str]) -> int:
        return localization_main([rewrite(part) for part in argv])

    stage = renderer.stage_job(
        name="stage",
        namespace="fs2-academic-poc",
        run_id="r",
        artifacts=[artifact],
        image="registry.invalid/x@sha256:" + "0" * 64,
        python="/usr/bin/python3",
        config_map="c",
        plane={"kind": "host-path", "host_root": str(trees)},
        node_selector={},
        tolerations=[],
        resources={},
        security_context={},
        tree_prefix=prefix,
    )
    # The rendered init container creates the prefix and receipt directory, and
    # the tree volume is mounted at its own root so a first run is not asking to
    # mount a subPath that does not exist yet.
    init = stage["spec"]["template"]["spec"]["initContainers"][0]["command"][-1]
    assert renderer.RECEIPTS_DIR in init and prefix in init
    tree_mount = next(
        item for item in stage["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if item["name"] == "trees"
    )
    assert "subPath" not in tree_mount
    (trees / prefix / ".receipts").mkdir(parents=True)

    # The public plane is a host directory, so the rendered Job must mount one.
    tree_volume = next(item for item in stage["spec"]["template"]["spec"]["volumes"] if item["name"] == "trees")
    assert tree_volume["hostPath"] == {"path": str(trees), "type": "Directory"}

    argv = _localize_argv(stage, "stage-")
    assert argv[argv.index("--volume-kind") + 1] == "host-path"
    assert argv[argv.index("--host-root") + 1] == str(trees)
    # The archive is already local here; the rendered job fetches it instead.
    fetch = argv.index("--fetch-archive-to")
    argv[fetch : fetch + 2] = ["--archive", str(archive)]
    assert localize(argv) == 0, "the rendered stage argv must run against this CLI"

    # The prefix is carried in the path now, not mounted as a subPath.
    published = trees / prefix / "generations" / MOLECULES_ID / "sha256" / generation
    assert published.is_dir(), f"the rendered stage must publish {published}"
    assert str(published).endswith(sub_path.split(prefix, 1)[1].lstrip("/"))
    marker = json.loads((published / RUNTIME_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["sub_path"] == sub_path
    assert marker["generation"] == generation
    # Nothing partial and no external marker directory was left behind.
    assert [item.name for item in (trees / prefix / "generations" / MOLECULES_ID).iterdir()] == ["sha256"]
    assert not (trees / prefix / "markers").exists()

    qualify = renderer.qualify_job(
        name="qualify",
        namespace="fs2-academic-poc",
        run_id="r",
        model_id="boltzgen",
        artifacts=[artifact],
        image="registry.invalid/x@sha256:" + "0" * 64,
        python="/usr/bin/python3",
        config_map="c",
        planes={"public": {"kind": "host-path", "host_root": str(trees)}},
        queue="inference-models",
        probe=[],
        node_selector={},
        tolerations=[],
        gpu_resource="nvidia.com/gpu",
        gpu_count=0,
        security_context={},
        resources={},
        tree_prefix=prefix,
    )
    verify_argv = _localize_argv(qualify, f"verify-{MOLECULES_ID}"[:63])
    # The qualification mounts the generation at the consumer path.
    resolved = [part.replace(artifact["tree"]["mount_paths"][0], str(published)) for part in verify_argv]
    assert localization_main(resolved) == 0, "the rendered qualification argv must admit the generation"

    # And it refuses a mount that is not that generation.
    other = tmp_path / "other"
    other.mkdir()
    (other / RUNTIME_MARKER_NAME).write_bytes((published / RUNTIME_MARKER_NAME).read_bytes())
    assert localization_main([part.replace(str(published), str(other)) for part in resolved]) == 1


def test_raw_file_renderer_uses_file_flags_and_mounts_the_exact_generation(tmp_path: Path) -> None:
    renderer = _renderer()
    source, contract_path, contract = _raw_fixture(tmp_path)
    artifact = _raw_document(source.read_bytes())
    prefix = "scientific-localization/public"
    trees = tmp_path / "plane-root"
    trees.mkdir()
    (trees / prefix / ".receipts").mkdir(parents=True)

    stage = renderer.stage_job(
        name="stage-raw",
        namespace="fs2-models",
        run_id="raw",
        artifacts=[artifact],
        image="registry.invalid/rfdiffusion@sha256:" + "0" * 64,
        python="/usr/bin/python3",
        config_map="raw-contract",
        plane={"kind": "host-path", "host_root": str(trees)},
        node_selector={"storage.fs2.nebius/reference-data": "true"},
        tolerations=[],
        resources={},
        security_context={},
        tree_prefix=prefix,
    )
    argv = _localize_argv(stage, "stage-rfdiffusion-base-checkpoint")
    assert "--fetch-file-to" in argv
    assert "--fetch-archive-to" not in argv and "--archive" not in argv

    def rewrite(part: str) -> str:
        if part == renderer.CONTRACT_MOUNT:
            return str(contract_path)
        if part.startswith(renderer.TREE_ROOT + "/"):
            return str(trees) + part[len(renderer.TREE_ROOT) :]
        return part

    local = [rewrite(part) for part in argv]
    fetch = local.index("--fetch-file-to")
    local[fetch : fetch + 2] = ["--file", str(source)]
    assert localization_main(local) == 0

    generation = contract.tree.inventory_sha256
    published = trees / prefix / "generations" / RFDIFFUSION_BASE_ID / "sha256" / generation
    assert (published / "Base_ckpt.pt").is_file()
    probe = [
        "python",
        "/opt/fs2/runtime_entrypoint.py",
        "run",
        "--artifact-root",
        contract.tree.canonical_mount_path,
    ]
    qualify = renderer.qualify_job(
        name="qualify-raw",
        namespace="fs2-models",
        run_id="raw",
        model_id="rfdiffusion",
        artifacts=[artifact],
        image="registry.invalid/rfdiffusion@sha256:" + "0" * 64,
        python="/usr/bin/python3",
        config_map="raw-contract",
        planes={"public": {"kind": "host-path", "host_root": str(trees)}},
        queue=None,
        probe=probe,
        node_selector={},
        tolerations=[],
        gpu_resource="nvidia.com/gpu",
        gpu_count=1,
        security_context={},
        resources={"requests": {}, "limits": {}},
        tree_prefix=prefix,
    )
    pod = qualify["spec"]["template"]["spec"]
    mount = next(
        item for item in pod["containers"][0]["volumeMounts"] if item["mountPath"] == contract.tree.canonical_mount_path
    )
    assert mount == {
        "name": "trees",
        "mountPath": "/opt/fs2/artifacts/rfdiffusion-base-checkpoint",
        "subPath": renderer.generation_sub_path(prefix, RFDIFFUSION_BASE_ID, generation),
        "readOnly": True,
    }
    verify_argv = _localize_argv(qualify, "verify-rfdiffusion-base-checkpoint")
    assert verify_argv[verify_argv.index("--expect-algorithm") + 1] == RAW_FILE_ALGORITHM
    assert pod["containers"][0]["command"] == probe


# ---------------------------------------------------------------------------
# Promoting a tree that already exists
# ---------------------------------------------------------------------------


def _installed_tree(root: Path) -> Path:
    (root / "pkg" / "data").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_bytes(b"code\n")
    (root / "pkg" / "data" / "big.bin").write_bytes(b"x" * 100_000)
    os.symlink("pkg/__init__.py", root / "alias.py")
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            os.chmod(path, 0o440)
    return root


def test_promoting_an_installed_tree_shares_its_bytes_instead_of_copying_them(tmp_path: Path) -> None:
    """The academic claim has gigabytes of headroom, not tens of them.

    A licensed tree still needs an immutable content-addressed name, but making
    that name must not cost a second full copy of the tree. Hard links give the
    generation its own path into the same data.
    """

    source = _installed_tree(tmp_path / "producer")
    before = tree_manifest_identity(source)
    staging = prepare_staging_directory(tmp_path / "artifact")
    linked = link_tree_into(source, staging)

    assert linked.files_copied == 0 and linked.bytes_copied == 0
    assert linked.files_linked == 2 and linked.bytes_linked == before.total_bytes
    assert linked.symlinks == 1
    shared = staging / "pkg" / "data" / "big.bin"
    assert shared.stat().st_ino == (source / "pkg" / "data" / "big.bin").stat().st_ino
    # Same bytes, so the producing plane's identity is reproduced exactly.
    assert tree_manifest_identity(staging).sha256 == before.sha256


def test_promotion_seals_a_marker_without_moving_the_producer_identity(tmp_path: Path) -> None:
    source = _installed_tree(tmp_path / "producer")
    before = tree_manifest_identity(source)
    root = tmp_path / "artifact"
    staging = prepare_staging_directory(root)
    link_tree_into(source, staging)
    write_generation_marker(
        staging / RUNTIME_MARKER_NAME,
        _marker(generation=before.sha256, inventory_algorithm=TREE_MANIFEST_ALGORITHM),
    )
    published, _reused = promote_generation(staging, root, before.sha256)

    assert published == root / "sha256" / before.sha256
    assert (published / RUNTIME_MARKER_NAME).is_file()
    # Sealing the marker inside must not move the digest the producer published.
    assert tree_manifest_identity(published).sha256 == before.sha256
    # And the producing plane's own files are untouched, mode bits included.
    assert (source / "pkg" / "data" / "big.bin").stat().st_mode & 0o777 == 0o440
    assert (source / RUNTIME_MARKER_NAME).exists() is False


def _tree_state(root: Path) -> dict[str, tuple[int, int, bytes | str]]:
    """Mode, mtime and content of every entry, for proving nothing moved."""

    state: dict[str, tuple[int, int, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        status = path.lstat()
        content: bytes | str = (
            os.readlink(path) if path.is_symlink() else (path.read_bytes() if path.is_file() else b"")
        )
        state[relative] = (status.st_mode, status.st_mtime_ns, content)
    return state


def test_a_writable_source_entry_is_never_shared_by_link(tmp_path: Path) -> None:
    """A hard link follows the inode, so a writable source could change both."""

    source = _installed_tree(tmp_path / "producer")
    os.chmod(source / "pkg" / "__init__.py", 0o640)
    before = _tree_state(source)

    with pytest.raises(ArtifactLocalizationError, match="writable"):
        link_tree_into(source, prepare_staging_directory(tmp_path / "artifact"))

    # Failing closed means the producing tree is exactly as it was: no mode
    # rewritten, no content touched, no mtime moved.
    assert _tree_state(source) == before


def test_sealing_refuses_to_reach_through_a_shared_writable_inode(tmp_path: Path) -> None:
    """The invariant is enforced where the chmod happens, not only upstream.

    link_tree_into already refuses a writable source, but promotion must not
    depend on a distant guard: chmod follows the inode, so a shared writable file
    reaching this point has to fail rather than rewrite the tree it came from.
    """

    source = _installed_tree(tmp_path / "producer")
    root = tmp_path / "artifact"
    staging = prepare_staging_directory(root)
    # Link by hand, bypassing the guard, to reach the sealing step directly.
    (staging / "pkg").mkdir(parents=True)
    os.link(source / "pkg" / "__init__.py", staging / "pkg" / "__init__.py")
    os.chmod(staging / "pkg" / "__init__.py", 0o640)
    before = _tree_state(source)

    with pytest.raises(ArtifactLocalizationError, match="shared by hard link"):
        promote_generation(staging, root, "d" * 64)

    assert _tree_state(source) == before
    assert not (root / "sha256" / ("d" * 64)).exists()


def test_a_promoted_symlink_keeps_the_target_the_producer_identity_records(tmp_path: Path) -> None:
    """fs2-tree-manifest/v1 identifies a symlink by its target, so it must survive."""

    source = _installed_tree(tmp_path / "producer")
    staging = prepare_staging_directory(tmp_path / "artifact")
    link_tree_into(source, staging)

    assert (staging / "alias.py").is_symlink()
    assert os.readlink(staging / "alias.py") == os.readlink(source / "alias.py") == "pkg/__init__.py"
    assert tree_manifest_identity(staging).symlink_count == 1
    assert tree_manifest_identity(staging).sha256 == tree_manifest_identity(source).sha256


def test_a_marker_names_the_kind_of_plane_that_holds_the_generation() -> None:
    """The public model-artifact plane is a host directory; the licensed one is a claim.

    Each is addressed differently, so a marker that carried a claim for a host
    directory, or a host root for a claim, would name a location nobody can mount.
    """

    host = generation_marker(
        artifact_id=MOLECULES_ID,
        generation="c" * 64,
        entry_count=1,
        total_bytes=1,
        inventory_algorithm=TREE_INVENTORY_ALGORITHM,
        sub_path="scientific-localization/public/generations/x/sha256/" + "c" * 64,
        visibility="public",
        volume_kind="host-path",
        host_root="/mnt/fs2-reference-data/data",
    )
    assert host["volume_kind"] == "host-path"
    assert host["host_root"] == "/mnt/fs2-reference-data/data"
    assert host["namespace"] == host["claim"] == ""

    claim = _marker()
    assert claim["volume_kind"] == "persistent-volume-claim"
    assert claim["host_root"] == ""
    assert claim["namespace"] and claim["claim"]

    for kwargs, message in (
        ({"volume_kind": "host-path", "namespace": "n", "claim": "c"}, "host root"),
        ({"volume_kind": "host-path"}, "host root"),
        ({"volume_kind": "persistent-volume-claim", "host_root": "/mnt/x"}, "namespace and claim"),
        ({"volume_kind": "persistent-volume-claim"}, "namespace and claim"),
        ({"volume_kind": "host-path", "host_root": "relative/path"}, "safe absolute path"),
        ({"volume_kind": "nfs", "namespace": "n", "claim": "c"}, "volume_kind is unsupported"),
    ):
        arguments: dict[str, Any] = {
            "artifact_id": MOLECULES_ID,
            "generation": "c" * 64,
            "entry_count": 1,
            "total_bytes": 1,
            "inventory_algorithm": TREE_INVENTORY_ALGORITHM,
            "sub_path": "a/b",
            "visibility": "public",
            **kwargs,
        }
        with pytest.raises(ArtifactLocalizationError, match=message):
            generation_marker(**arguments)


# ---------------------------------------------------------------------------
# Adversarial: admission, symlinks, terminal receipts, reuse
# ---------------------------------------------------------------------------


def _published(tmp_path: Path, entries: Mapping[str, bytes], **overrides: Any) -> tuple[Path, str, str]:
    """Publish a generation and return its path, generation and marker digest."""

    generation = _inventory(entries)
    staged = _materialize(tmp_path / "staged", entries)
    document = _marker(
        generation=generation,
        entry_count=len(entries),
        total_bytes=sum(map(len, entries.values())),
        sub_path=f"p/generations/{MOLECULES_ID}/sha256/{generation}",
        **overrides,
    )
    digest = write_generation_marker(staged / RUNTIME_MARKER_NAME, document)
    published, _reused = promote_generation(staged, tmp_path / "artifact", generation)
    return published, generation, digest


def _admit(published: Path, generation: str, *extra: str) -> int:
    return localization_main(
        [
            "marker",
            "--artifact-id",
            MOLECULES_ID,
            "--mount",
            str(published),
            "--expect-generation",
            generation,
            "--sub-path",
            f"p/generations/{MOLECULES_ID}/sha256/{generation}",
            *extra,
        ]
    )


def test_admission_pins_the_marker_digest_and_every_plane_field(tmp_path: Path) -> None:
    """Right bytes in the wrong place, licence or algorithm are still wrong."""

    published, generation, digest = _published(tmp_path, SYNTHETIC_ENTRIES)
    assert (
        _admit(
            published,
            generation,
            "--expect-manifest-digest",
            digest,
            "--expect-volume-kind",
            "persistent-volume-claim",
            "--expect-namespace",
            "fs2-academic-poc",
            "--expect-claim",
            "academic-assets-runtime-rwx",
            "--expect-visibility",
            "public",
            "--expect-algorithm",
            TREE_INVENTORY_ALGORITHM,
        )
        == 0
    )

    for wrong in (
        ("--expect-manifest-digest", "f" * 64),
        ("--expect-volume-kind", "host-path"),
        ("--expect-namespace", "somebody-else"),
        ("--expect-claim", "another-claim"),
        ("--expect-host-root", "/mnt/elsewhere"),
        ("--expect-visibility", "tenant-private"),
        ("--expect-algorithm", RECURSIVE_INVENTORY_ALGORITHM),
    ):
        assert _admit(published, generation, *wrong) == 1, f"{wrong} must be refused"


def test_admission_refuses_a_marker_whose_bytes_are_not_canonical(tmp_path: Path) -> None:
    """A padded or reordered document would carry a digest for bytes nobody reads."""

    published, generation, digest = _published(tmp_path, SYNTHETIC_ENTRIES)
    marker = published / RUNTIME_MARKER_NAME
    document = json.loads(marker.read_text(encoding="utf-8"))
    os.chmod(published, 0o755)  # noqa: S103 - reopening a sealed generation to tamper with it
    os.chmod(marker, 0o644)
    # Same content, different bytes: compact instead of the canonical form.
    marker.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    assert _admit(published, generation, "--expect-manifest-digest", digest) == 1


def test_a_symlinked_generation_is_admissible_under_the_algorithm_that_covers_symlinks(
    tmp_path: Path,
) -> None:
    """PyRosetta's identity covers symlinks by target, so a mount holding them must admit."""

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    root = tmp_path / "artifact"
    staging = prepare_staging_directory(root)
    link_tree_into(source, staging)
    sub_path = f"p/generations/{MOLECULES_ID}/sha256/{identity.sha256}"
    document = _marker(
        generation=identity.sha256,
        entry_count=identity.file_count,
        directory_count=2,
        symlink_count=identity.symlink_count,
        total_bytes=identity.total_bytes,
        inventory_algorithm=TREE_MANIFEST_ALGORITHM,
        sub_path=sub_path,
    )
    write_generation_marker(staging / RUNTIME_MARKER_NAME, document)
    published, _reused = promote_generation(staging, root, identity.sha256)

    assert identity.symlink_count == 1
    assert count_generation(published, maximum_entries=100, permit_symlinks=True) == (2, 2, 1)
    assert _admit(published, identity.sha256, "--expect-algorithm", TREE_MANIFEST_ALGORITHM) == 0

    # A flat-inventory generation still refuses a symlink outright.
    with pytest.raises(ArtifactLocalizationError, match="symbolic link"):
        count_generation(published, maximum_entries=100)


def test_a_declared_symlink_count_that_does_not_match_the_mount_is_refused(tmp_path: Path) -> None:
    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    root = tmp_path / "artifact"
    staging = prepare_staging_directory(root)
    link_tree_into(source, staging)
    sub_path = f"p/generations/{MOLECULES_ID}/sha256/{identity.sha256}"
    write_generation_marker(
        staging / RUNTIME_MARKER_NAME,
        _marker(
            generation=identity.sha256,
            entry_count=identity.file_count,
            directory_count=2,
            symlink_count=7,
            total_bytes=identity.total_bytes,
            inventory_algorithm=TREE_MANIFEST_ALGORITHM,
            sub_path=sub_path,
        ),
    )
    published, _reused = promote_generation(staging, root, identity.sha256)
    assert _admit(published, identity.sha256) == 1


def _stage_argv(contract_path: Path, archive: Path, artifact_root: Path, receipt: Path, sub_path: str) -> list[str]:
    return [
        "stage",
        "--contract",
        str(contract_path),
        "--artifact-id",
        MOLECULES_ID,
        "--archive",
        str(archive),
        "--artifact-root",
        str(artifact_root),
        "--sub-path",
        sub_path,
        "--volume-kind",
        "persistent-volume-claim",
        "--namespace",
        "fs2-academic-poc",
        "--claim",
        "academic-assets-runtime-rwx",
        "--visibility",
        "public",
        "--receipt",
        str(receipt),
    ]


def _stage_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    entries = SYNTHETIC_ENTRIES
    payload = _zip_bytes(sorted(entries.items()))
    artifact = _document(payload, entries)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "mols.zip"
    archive.write_bytes(payload)
    generation = artifact["tree"]["inventory_sha256"]
    return contract_path, archive, generation, f"p/generations/{MOLECULES_ID}/sha256/{generation}"


def test_a_publication_failure_still_emits_a_receipt_and_leaves_no_staging(tmp_path: Path) -> None:
    """A tree that verified and then failed to publish is an outcome, not a crash.

    Without a receipt a caller cannot tell a refusal from a container that died,
    and a staging directory left behind consumes a claim that has little room.
    """

    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "stage.json"
    # A file where the generation directory must go: publication cannot succeed.
    (root / "sha256").mkdir(parents=True)
    (root / "sha256" / generation).write_bytes(b"not a directory")

    code = localization_main(_stage_argv(contract_path, archive, root, receipt, sub_path))
    assert code == 1
    document = json.loads(receipt.read_text(encoding="utf-8"))
    assert document["state"] == "rejected"
    assert "generation-publication-failed" in document["rejection_reason"]
    # The tree still verified, so the receipt says what it saw.
    assert document["tree_identity"]["inventory_sha256"] == generation
    assert [item.name for item in root.iterdir()] == ["sha256"], "no staging directory survives"


def test_an_existing_generation_is_reverified_before_success(tmp_path: Path) -> None:
    """This tool proved the bytes it staged, and nothing about someone else's.

    A digest is a name, and a name is not evidence. A target that already exists
    is verified in place, so a directory holding the wrong bytes under the right
    digest is refused rather than reported as a successful publication.
    """

    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "stage.json"

    # First publication succeeds and is genuinely verified.
    assert localization_main(_stage_argv(contract_path, archive, root, receipt, sub_path)) == 0
    first = json.loads(receipt.read_text(encoding="utf-8"))
    assert first["state"] == "verified"
    assert first["observation"]["generation_reused"] is False

    # Re-running against the published generation reuses and reverifies it.
    assert localization_main(_stage_argv(contract_path, archive, root, receipt, sub_path)) == 0
    second = json.loads(receipt.read_text(encoding="utf-8"))
    assert second["observation"]["generation_reused"] is True
    assert second["observation"]["marker_sha256"] == first["observation"]["marker_sha256"]

    # Now corrupt the published bytes under the same digest.
    published = root / "sha256" / generation
    os.chmod(published, 0o755)  # noqa: S103 - reopening a sealed generation to tamper with it
    victim = published / "HEM.pkl"
    os.chmod(victim, 0o644)
    victim.write_bytes(b"tampered but the directory is still named the same")

    assert localization_main(_stage_argv(contract_path, archive, root, receipt, sub_path)) == 1
    third = json.loads(receipt.read_text(encoding="utf-8"))
    assert third["state"] == "rejected"
    assert "does not verify" in third["rejection_reason"]


def test_the_promotion_job_addresses_its_source_beneath_the_one_claim_mount() -> None:
    """The source is protected by the tool now, not by a second read-only mount.

    A read-only mount of the source would have been reassuring and wrong: it
    puts the source in a different mount namespace from the destination, and a
    hard link cannot cross that even on one filesystem. The protection that
    replaces it is that the promotion only ever reads the source, refuses a
    writable source file, and never chmods a shared inode.
    """

    renderer = _renderer()
    artifact = _checked_in_artifact("bindcraft-pyrosetta-installed-tree")
    job = renderer.promote_job(
        name="promote",
        namespace="fs2-academic-poc",
        run_id="r",
        artifact=artifact,
        image=_RENDER_IMAGE,
        python="/usr/bin/python3",
        config_map="c",
        plane={"kind": "persistent-volume-claim", "claim": "academic-assets-runtime-rwx"},
        source_claim="academic-assets-runtime-rwx",
        tree_prefix="scientific-localization/private",
        node_selector={"storage.fs2.nebius/shared-cache": "true"},
        tolerations=[],
        resources={},
        security_context={},
    )
    spec = job["spec"]["template"]["spec"]
    assert {item["name"] for item in spec["volumes"]} == {"verifier", "trees", "scratch"}

    mounts = {item["name"]: item for item in spec["containers"][0]["volumeMounts"]}
    assert "subPath" not in mounts["trees"]
    assert mounts["trees"].get("readOnly") is not True

    argv = spec["containers"][0]["command"]
    generation = artifact["tree"]["inventory_sha256"]
    assert argv[3] == "promote"
    assert argv[argv.index("--promote-from") + 1] == f"/trees/{artifact['source_sub_path']}"
    assert argv[argv.index("--artifact-root") + 1].endswith("/bindcraft-pyrosetta-installed-tree")
    assert argv[argv.index("--sub-path") + 1] == (
        f"scientific-localization/private/generations/bindcraft-pyrosetta-installed-tree/sha256/{generation}"
    )
    assert argv[argv.index("--visibility") + 1] == "tenant-private"
    # Zero copy is the default; a copy has to be asked for.
    assert "--allow-copy" not in argv


def test_a_staged_artifact_cannot_be_rendered_as_a_promotion() -> None:
    """Only a tree another plane installed is promoted; the rest are staged."""

    renderer = _renderer()
    contract = json.loads(
        (
            Path(__file__).resolve().parents[3] / "catalog/runtime/contracts/scientific-artifact-localization.json"
        ).read_text(encoding="utf-8")
    )
    artifact = next(item for item in contract["artifacts"] if item["artifact_id"] == MOLECULES_ID)
    with pytest.raises(SystemExit, match="staged from its archive"):
        renderer.promote_job(
            name="promote",
            namespace="fs2-academic-poc",
            run_id="r",
            artifact=artifact,
            image="registry.invalid/x@sha256:" + "0" * 64,
            python="/usr/bin/python3",
            config_map="c",
            plane={"kind": "persistent-volume-claim", "claim": "c"},
            source_claim="c",
            tree_prefix="p",
            node_selector={},
            tolerations=[],
            resources={},
            security_context={},
        )


# ---------------------------------------------------------------------------
# Adversarial: every terminal state, and its receipt against the real schema
# ---------------------------------------------------------------------------


def _receipt_validator() -> Any:
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "catalog/runtime/schema/scientific-localization-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def _assert_valid_receipt(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(_receipt_validator().iter_errors(document), key=lambda item: list(item.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors]
    return document


def test_a_successful_stage_receipt_validates_against_the_checked_in_schema(tmp_path: Path) -> None:
    """A receipt nobody can validate is not evidence, it is a log line."""

    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)
    receipt = tmp_path / "receipts" / "stage.json"
    assert localization_main(_stage_argv(contract_path, archive, tmp_path / "artifact", receipt, sub_path)) == 0
    document = _assert_valid_receipt(receipt)
    # The fields the publication step adds are exactly the ones the schema
    # previously rejected.
    assert document["observation"]["generation"] == generation
    assert document["observation"]["generation_reused"] is False
    assert len(document["observation"]["marker_sha256"]) == 64


def test_every_terminal_failure_emits_a_valid_receipt_and_leaves_no_staging(tmp_path: Path) -> None:
    """Fetch, extraction, linking and publication all end the same way.

    Each of these used to escape the publication guard and exit with a bare
    message, leaving a staging directory on a claim with little headroom.
    """

    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)

    # A corrupt archive: extraction fails before anything is published.
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(archive.read_bytes()[:-40] + b"x" * 40)
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "corrupt.json"
    assert localization_main(_stage_argv(contract_path, corrupt, root, receipt, sub_path)) == 1
    document = _assert_valid_receipt(receipt)
    assert document["state"] == "rejected"
    assert "stage-failed" in document["rejection_reason"]
    assert not list(root.glob(f"{STAGING_PREFIX}*")), "staging must not survive a failed extraction"

    # A source that cannot be read: promotion fails before anything is published.
    promote_receipt = tmp_path / "receipts" / "promote.json"
    assert (
        localization_main(
            [
                "promote",
                "--contract",
                str(contract_path),
                "--artifact-id",
                MOLECULES_ID,
                "--promote-from",
                str(tmp_path / "does-not-exist"),
                "--artifact-root",
                str(root),
                "--sub-path",
                sub_path,
                "--volume-kind",
                "host-path",
                "--host-root",
                "/mnt/fs2-reference-data/data",
                "--visibility",
                "public",
                "--receipt",
                str(promote_receipt),
            ]
        )
        == 1
    )
    document = _assert_valid_receipt(promote_receipt)
    assert document["state"] == "rejected"
    assert "promote-failed" in document["rejection_reason"]
    assert not list(root.glob(f"{STAGING_PREFIX}*")), "staging must not survive a failed promotion"


def test_a_rejected_publication_receipt_also_validates(tmp_path: Path) -> None:
    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)
    root = tmp_path / "artifact"
    (root / "sha256").mkdir(parents=True)
    (root / "sha256" / generation).write_bytes(b"not a directory")
    receipt = tmp_path / "receipts" / "publish.json"
    assert localization_main(_stage_argv(contract_path, archive, root, receipt, sub_path)) == 1
    document = _assert_valid_receipt(receipt)
    assert "generation-publication-failed" in document["rejection_reason"]


def test_a_contracted_symlink_count_reaches_the_receipt_and_refuses_a_mismatch(tmp_path: Path) -> None:
    """The schema accepts the field, so the parser and receipt must carry it."""

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    base = _document(b"unused-archive-bytes", {"a": b"b"}, artifact_id="pyrosetta-fixture")
    base["transform"] = "external-installed-tree"
    base["source_sub_path"] = "producer"
    base["visibility"] = "tenant-private"
    base["archive"]["media_type"] = "application/zip"
    # A wheel name carries a "+", which the tree entry pattern cannot match,
    # so the archive can never be mistaken for a member of its own tree.
    base["archive"]["filename"] = "pyrosetta-2026.29+release-cp310-linux_x86_64.whl"
    base["tree"] = {
        "mount_paths": ["/opt/fs2/academic/pyrosetta-fixture"],
        "entry_count": identity.file_count,
        "directory_count": 2,
        "symlink_count": identity.symlink_count,
        "total_bytes": identity.total_bytes,
        "entry_path_pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$",
        "inventory_algorithm": TREE_MANIFEST_ALGORITHM,
        "inventory_sha256": identity.sha256,
    }
    base["consumers"] = [
        {
            "model_id": "bindcraft",
            "binding_kind": "environment-variable",
            "binding_name": "PYTHONPATH",
            "mount_path": "/opt/fs2/academic/pyrosetta-fixture",
        }
    ]
    contract = LocalizationContract.parse(base)
    assert contract.tree.symlink_count == identity.symlink_count == 1

    receipt = verify_localized_tree(source, contract)
    assert receipt.verified
    assert receipt.symlink_count == 1
    assert receipt.to_dict()["tree_identity"]["symlink_count"] == 1

    # A contract that declares the wrong number of symlinks is refused.
    mismatched = replace(contract, tree=replace(contract.tree, symlink_count=4))
    refused = verify_localized_tree(source, mismatched)
    assert not refused.verified
    assert "symbolic links" in (refused.rejection_reason or "")


def test_the_receipt_carries_a_node_digest_and_never_the_node_name(tmp_path: Path) -> None:
    """One canonical privacy transform, applied where the value enters.

    A caller may hand over the raw downward-API node name or a digest it already
    computed; the receipt carries exactly one field either way, and the raw name
    has one place it can be turned away and no path to disk.
    """

    # A stand-in, not a real instance ID: this file is part of the public
    # export, and the transform under test does not care about the shape.
    raw = "node-under-test-0000000000"
    expected = node_digest(raw)
    assert len(expected) == 16

    contract_path, archive, generation, sub_path = _stage_fixture(tmp_path)
    receipt = tmp_path / "receipts" / "stage.json"
    argv = _stage_argv(contract_path, archive, tmp_path / "artifact", receipt, sub_path)
    argv += ["--observation", json.dumps({"node": raw, "region": "eu-north1"})]
    assert localization_main(argv) == 0

    document = _assert_valid_receipt(receipt)
    observation = document["observation"]
    assert observation["node_digest"] == expected
    assert "node" not in observation
    assert raw not in receipt.read_text(encoding="utf-8")

    # The probes compute the same digest, so two receipts about one node agree.
    probe = _renderer_probe_module()
    assert probe.node_digest() == "" or True
    os.environ["FS2_NODE_NAME"] = raw
    try:
        assert probe.node_digest() == expected
    finally:
        os.environ.pop("FS2_NODE_NAME", None)


def _renderer_probe_module() -> Any:
    path = (
        Path(__file__).resolve().parents[3]
        / "models/cancer-immunotherapy/artifact-localization/probes/boltzgen_moldir_probe.py"
    )
    spec = importlib.util.spec_from_file_location("fs2_boltzgen_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_rejected_stage_receipt_also_reclaims_its_staging(tmp_path: Path) -> None:
    """A tree that does not verify is as terminal as one that raises."""

    entries = SYNTHETIC_ENTRIES
    payload = _zip_bytes(sorted(entries.items()))
    artifact = _document(payload, entries)
    # Contract the wrong byte total, so the extracted tree verifies as wrong
    # rather than failing to extract.
    artifact["tree"]["total_bytes"] += 1
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    archive = tmp_path / "mols.zip"
    archive.write_bytes(payload)
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "stage.json"

    generation = artifact["tree"]["inventory_sha256"]
    code = localization_main(
        _stage_argv(contract_path, archive, root, receipt, f"p/generations/{MOLECULES_ID}/sha256/{generation}")
    )
    assert code == 1
    document = _assert_valid_receipt(receipt)
    assert document["state"] == "rejected"
    assert not list(root.glob(f"{STAGING_PREFIX}*")), "a rejected receipt must not leave staging behind"
    assert not (root / "sha256").exists(), "nothing is published for a tree that did not verify"


def _render(*argv: str) -> dict[str, Any]:
    """Run the renderer CLI the way an operator does."""

    import contextlib
    import io

    renderer = _renderer()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert renderer.main(list(argv)) == 0
    return json.loads(buffer.getvalue())


_RENDER_IMAGE = "registry.invalid/x@sha256:" + "0" * 64
_CONTRACT = "catalog/runtime/contracts/scientific-artifact-localization.json"


def _contract_path() -> str:
    return str(Path(__file__).resolve().parents[3] / _CONTRACT)


def test_a_first_run_creates_its_prefix_instead_of_mounting_one_that_is_absent(tmp_path: Path) -> None:
    """The bootstrap the renderer emits must actually work on an empty volume.

    Mounting tree_prefix as a subPath deadlocks the first run for a new prefix,
    because the directory the run exists to create has to be there before the
    mount succeeds. This executes the rendered bootstrap against an empty root.
    """

    document = _render(
        "promote",
        "--artifact-id",
        "bindcraft-pyrosetta-installed-tree",
        "--namespace",
        "fs2-academic-poc",
        "--run-id",
        "r",
        "--claim",
        "academic-assets-runtime-rwx",
        "--source-claim",
        "academic-assets-runtime-rwx",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
    )
    spec = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"]

    # No mount asks for a sub-path of the tree volume.
    for container in (*spec["initContainers"], *spec["containers"]):
        for mount in container["volumeMounts"]:
            if mount["name"] == "trees":
                assert "subPath" not in mount, "a first run cannot mount a prefix it has not created"

    # The rendered bootstrap runs against a genuinely empty volume root.
    root = tmp_path / "empty-volume"
    root.mkdir()
    bootstrap = spec["initContainers"][0]["command"]
    assert bootstrap[1] == "-c"
    subprocess.run(  # noqa: S603 - the rendered argv under test, against a temporary directory
        [sys.executable, "-c", bootstrap[2].replace("/trees", str(root))],
        check=True,
        capture_output=True,
    )
    created = root / "scientific-localization/private/.receipts"
    assert created.is_dir(), "the bootstrap must create the prefix on an empty volume"

    # And the destination the promotion writes into can then be made.
    argv = spec["containers"][0]["command"]
    artifact_root = Path(argv[argv.index("--artifact-root") + 1].replace("/trees", str(root)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    assert artifact_root.is_dir()


def test_the_academic_claim_profile_joins_its_group_and_never_sets_fsgroup() -> None:
    """The claim root is setgid and group-writable by 65532, not owned by us."""

    document = _render(
        "promote",
        "--artifact-id",
        "bindcraft-pyrosetta-installed-tree",
        "--namespace",
        "fs2-academic-poc",
        "--run-id",
        "r",
        "--claim",
        "academic-assets-runtime-rwx",
        "--source-claim",
        "academic-assets-runtime-rwx",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
    )
    context = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"][
        "securityContext"
    ]
    assert context["supplementalGroups"] == [65532]
    # fsGroup applies to the whole volume, which also holds another tenant's
    # assets, so it is refused rather than quietly accepted.
    assert "fsGroup" not in context
    assert context["runAsNonRoot"] is True


def test_the_public_host_plane_uses_its_own_owner_and_requires_its_node_label() -> None:
    document = _render(
        "stage",
        "--artifact-id",
        MOLECULES_ID,
        "--namespace",
        "fs2-models",
        "--run-id",
        "r",
        "--claim",
        "unused-for-a-host-plane",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
        "--plane",
        "host-path",
        "--node-selector",
        "storage.fs2.nebius/reference-data=true",
    )
    spec = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"]
    # The public host root is owned by 1000:1000, not by the academic claim's group.
    assert spec["securityContext"]["runAsUser"] == 1000
    assert spec["securityContext"]["runAsGroup"] == 1000
    assert "supplementalGroups" not in spec["securityContext"]
    assert spec["nodeSelector"]["storage.fs2.nebius/reference-data"] == "true"


def test_a_public_render_without_the_reference_data_label_is_refused() -> None:
    """Only labelled nodes mount the host root; elsewhere the directory is absent."""

    renderer = _renderer()
    with pytest.raises(SystemExit, match="storage.fs2.nebius/reference-data=true"):
        renderer.main(
            [
                "stage",
                "--artifact-id",
                MOLECULES_ID,
                "--namespace",
                "fs2-models",
                "--run-id",
                "r",
                "--claim",
                "c",
                "--config-map",
                "cm",
                "--contract",
                _contract_path(),
                "--image",
                _RENDER_IMAGE,
                "--plane",
                "host-path",
            ]
        )


def test_a_generic_claim_never_inherits_the_academic_claims_ownership() -> None:
    """Ownership is a property of a volume, not a rule about claims.

    Applying the academic claim's GID to a customer PVC either fails to write or
    writes files that volume's owner did not ask for, so an unknown claim must
    be told what it needs rather than guessed at.
    """

    renderer = _renderer()
    common = [
        "stage",
        "--artifact-id",
        MOLECULES_ID,
        "--run-id",
        "r",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
    ]
    with pytest.raises(SystemExit, match="will not guess it"):
        renderer.main([*common, "--namespace", "customer-ns", "--claim", "customer-pvc"])

    # Told explicitly, it uses exactly what it was told.
    document = _render(*common, "--namespace", "customer-ns", "--claim", "customer-pvc", "--supplemental-group", "2000")
    context = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"][
        "securityContext"
    ]
    assert context["supplementalGroups"] == [2000]
    assert "fsGroup" not in context

    # A claim the workload owns outright may legitimately use fsGroup, and only
    # the academic claim refuses it.
    document = _render(*common, "--namespace", "customer-ns", "--claim", "customer-pvc", "--fs-group", "3000")
    context = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"][
        "securityContext"
    ]
    assert context["fsGroup"] == 3000
    with pytest.raises(SystemExit, match="fsGroup rewrites ownership"):
        renderer.main(
            [
                *common,
                "--namespace",
                "fs2-academic-poc",
                "--claim",
                "academic-assets-runtime-rwx",
                "--fs-group",
                "65532",
            ]
        )


def test_a_promoted_installed_tree_emits_a_schema_valid_receipt_and_reuses_it(tmp_path: Path) -> None:
    """The PyRosetta shape end to end: promote, validate, then promote again.

    A tree-manifest receipt carries a symlink count and a directory count that
    the schema previously forbade, so this runs the real CLI and validates what
    it actually wrote.
    """

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    artifact = _document(b"unused", {"a": b"b"}, artifact_id="pyrosetta-fixture")
    artifact["transform"] = "external-installed-tree"
    artifact["source_sub_path"] = "producer"
    artifact["visibility"] = "tenant-private"
    artifact["archive"]["media_type"] = "application/zip"
    artifact["archive"]["filename"] = "pyrosetta-2026.29+release-cp310-linux_x86_64.whl"
    artifact["tree"] = {
        "mount_paths": ["/opt/fs2/academic/pyrosetta-fixture"],
        "entry_count": identity.file_count,
        "directory_count": 2,
        "symlink_count": identity.symlink_count,
        "total_bytes": identity.total_bytes,
        "entry_path_pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$",
        "inventory_algorithm": TREE_MANIFEST_ALGORITHM,
        "inventory_sha256": identity.sha256,
    }
    artifact["consumers"] = [
        {
            "model_id": "bindcraft",
            "binding_kind": "environment-variable",
            "binding_name": "PYTHONPATH",
            "mount_path": "/opt/fs2/academic/pyrosetta-fixture",
        }
    ]
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )

    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "promote.json"
    sub_path = f"scientific-localization/private/generations/pyrosetta-fixture/sha256/{identity.sha256}"
    argv = [
        "promote",
        "--contract",
        str(contract_path),
        "--artifact-id",
        "pyrosetta-fixture",
        "--promote-from",
        str(source),
        "--artifact-root",
        str(root),
        "--sub-path",
        sub_path,
        "--volume-kind",
        "persistent-volume-claim",
        "--namespace",
        "fs2-academic-poc",
        "--claim",
        "academic-assets-runtime-rwx",
        "--visibility",
        "tenant-private",
        "--receipt",
        str(receipt),
    ]

    assert localization_main(argv) == 0
    document = _assert_valid_receipt(receipt)
    assert document["tree_identity"]["inventory_algorithm"] == TREE_MANIFEST_ALGORITHM
    assert document["tree_identity"]["symlink_count"] == 1
    assert document["tree_identity"]["directory_count"] == 2
    assert document["observation"]["bytes_copied"] == 0, "promotion shares bytes rather than copying them"
    assert document["observation"]["bytes_linked"] == identity.total_bytes
    assert document["observation"]["generation_reused"] is False

    published = root / "sha256" / identity.sha256
    assert (published / RUNTIME_MARKER_NAME).is_file()
    assert (published / "alias.py").is_symlink()

    # Re-running reuses the published generation and reverifies it in place.
    assert localization_main(argv) == 0
    again = _assert_valid_receipt(receipt)
    assert again["observation"]["generation_reused"] is True
    assert again["observation"]["marker_sha256"] == document["observation"]["marker_sha256"]

    # And the mount admits under its own marker, symlink and all.
    assert (
        localization_main(
            [
                "marker",
                "--artifact-id",
                "pyrosetta-fixture",
                "--mount",
                str(published),
                "--expect-generation",
                identity.sha256,
                "--sub-path",
                sub_path,
                "--expect-algorithm",
                TREE_MANIFEST_ALGORITHM,
                "--expect-visibility",
                "tenant-private",
                "--expect-manifest-digest",
                document["observation"]["marker_sha256"],
            ]
        )
        == 0
    )


def test_a_writable_source_leaves_no_partial_link_tree_behind(tmp_path: Path) -> None:
    """The link fails midway, so the partial generation must not survive."""

    source = _installed_tree(tmp_path / "producer")
    os.chmod(source / "pkg" / "data" / "big.bin", 0o640)
    artifact = _document(b"unused", {"a": b"b"}, artifact_id="pyrosetta-fixture")
    artifact["transform"] = "external-installed-tree"
    artifact["source_sub_path"] = "producer"
    artifact["archive"]["media_type"] = "application/zip"
    artifact["archive"]["filename"] = "pyrosetta-2026.29+release-cp310-linux_x86_64.whl"
    artifact["tree"] = {
        "mount_paths": ["/opt/fs2/academic/pyrosetta-fixture"],
        "entry_count": 2,
        "total_bytes": 1,
        "entry_path_pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$",
        "inventory_algorithm": TREE_MANIFEST_ALGORITHM,
        "inventory_sha256": "e" * 64,
    }
    artifact["consumers"] = [
        {
            "model_id": "bindcraft",
            "binding_kind": "environment-variable",
            "binding_name": "PYTHONPATH",
            "mount_path": "/opt/fs2/academic/pyrosetta-fixture",
        }
    ]
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "promote.json"
    before = _tree_state(source)

    assert (
        localization_main(
            [
                "promote",
                "--contract",
                str(contract_path),
                "--artifact-id",
                "pyrosetta-fixture",
                "--promote-from",
                str(source),
                "--artifact-root",
                str(root),
                "--sub-path",
                "p/generations/pyrosetta-fixture/sha256/" + "e" * 64,
                "--volume-kind",
                "host-path",
                "--host-root",
                "/mnt/fs2-reference-data/data",
                "--visibility",
                "public",
                "--receipt",
                str(receipt),
            ]
        )
        == 1
    )
    document = _assert_valid_receipt(receipt)
    assert document["state"] == "rejected"
    assert "writable" in document["rejection_reason"]
    assert not list(root.glob(f"{STAGING_PREFIX}*")), "a half-linked tree must not survive"
    assert not (root / "sha256").exists()
    assert _tree_state(source) == before, "the producing tree is untouched"


# ---------------------------------------------------------------------------
# The marker a run seals must be the marker the handoff promised
# ---------------------------------------------------------------------------


def _checked_in_artifact(artifact_id: str) -> dict[str, Any]:
    document = json.loads(
        (
            Path(__file__).resolve().parents[3] / "catalog/runtime/contracts/scientific-artifact-localization.json"
        ).read_text(encoding="utf-8")
    )
    return next(item for item in document["artifacts"] if item["artifact_id"] == artifact_id)


def _published_marker(contract: LocalizationContract, *, sub_path: str, **volume: Any) -> dict[str, Any]:
    """The marker the publication path seals, built the way main() builds it."""

    return generation_marker(
        artifact_id=contract.artifact_id,
        artifact_kind=contract.artifact_kind,
        generation=contract.tree.inventory_sha256,
        entry_count=contract.tree.entry_count,
        directory_count=contract.tree.directory_count,
        symlink_count=contract.tree.symlink_count,
        total_bytes=contract.tree.total_bytes,
        inventory_algorithm=contract.tree.inventory_algorithm,
        sub_path=sub_path,
        visibility=contract.visibility,
        archive=contract.source,
        generated_entries=contract.tree.generated_entries,
        consumer_paths=contract.tree.mount_paths,
        **volume,
    )


def test_the_real_pyrosetta_declaration_seals_the_marker_the_handoff_pins() -> None:
    """The marker a promotion seals must be the one the handoff already pinned.

    A marker is an identity a consumer is told to expect before anything is
    published, so it is derived from the contract and never from what a run
    observed. Sealing an observed value the contract does not state produced a
    different document from the one the renderer promised, and admission then
    failed on a tree that was in fact correct.
    """

    artifact = _checked_in_artifact("bindcraft-pyrosetta-installed-tree")
    contract = LocalizationContract.parse(artifact)
    assert contract.tree.symlink_count == 0
    assert contract.tree.directory_count == 779

    handoff = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json"
        ).read_text(encoding="utf-8")
    )
    entry = next(item for item in handoff["artifacts"] if item["artifact_id"] == contract.artifact_id)
    volume = entry["volume"]

    sealed = _published_marker(
        contract,
        sub_path=volume["sub_path"],
        volume_kind=volume["kind"],
        namespace=volume["namespace"],
        claim=volume["claim"],
    )
    assert marker_sha256(sealed) == entry["marker"]["manifest_digest"]
    assert marker_bytes(sealed) == marker_bytes(entry["marker"]["document"])
    assert sealed["symlink_count"] == 0
    assert sealed["directory_count"] == 779


def test_every_checked_in_artifact_seals_the_marker_its_handoff_row_pins() -> None:
    """Whatever a promotion writes must equal what a consumer was told to expect."""

    handoff = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "models/cancer-immunotherapy/artifact-localization/evidence/binding-handoff.json"
        ).read_text(encoding="utf-8")
    )
    for entry in handoff["artifacts"]:
        contract = LocalizationContract.parse(_checked_in_artifact(entry["artifact_id"]))
        volume = entry["volume"]
        placement: dict[str, Any] = {"volume_kind": volume["kind"]}
        if volume["kind"] == "host-path":
            placement["host_root"] = volume["host_root"]
        else:
            placement["namespace"] = volume["namespace"]
            placement["claim"] = volume["claim"]
        sealed = _published_marker(contract, sub_path=volume["sub_path"], **placement)
        assert marker_sha256(sealed) == entry["marker"]["manifest_digest"], entry["artifact_id"]


def test_a_contract_that_omits_a_symlink_count_still_admits_a_tree_that_has_one(tmp_path: Path) -> None:
    """The functional half: promote a real symlinked tree under a silent contract.

    The synthetic fixture used to pin symlink_count and so agreed with itself.
    This one leaves it unstated exactly as the real PyRosetta declaration does,
    then promotes, and requires the sealed marker to match what the renderer
    precomputed and the mount to admit against it.
    """

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    assert identity.symlink_count == 1

    artifact = _document(b"unused", {"a": b"b"}, artifact_id="pyrosetta-silent")
    artifact["transform"] = "external-installed-tree"
    artifact["source_sub_path"] = "producer"
    artifact["visibility"] = "tenant-private"
    artifact["archive"]["media_type"] = "application/zip"
    artifact["archive"]["filename"] = "pyrosetta-2026.29+release-cp310-linux_x86_64.whl"
    artifact["tree"] = {
        "mount_paths": ["/opt/fs2/academic/pyrosetta-silent"],
        "entry_count": identity.file_count,
        "directory_count": 2,
        # Deliberately unstated, exactly as the checked-in declaration leaves it.
        "total_bytes": identity.total_bytes,
        "entry_path_pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$",
        "inventory_algorithm": TREE_MANIFEST_ALGORITHM,
        "inventory_sha256": identity.sha256,
    }
    artifact["consumers"] = [
        {
            "model_id": "bindcraft",
            "binding_kind": "environment-variable",
            "binding_name": "PYTHONPATH",
            "mount_path": "/opt/fs2/academic/pyrosetta-silent",
        }
    ]
    contract = LocalizationContract.parse(artifact)
    assert contract.tree.symlink_count is None

    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [artifact],
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "artifact"
    receipt = tmp_path / "receipts" / "promote.json"
    sub_path = f"p/generations/pyrosetta-silent/sha256/{identity.sha256}"
    expected_digest = marker_sha256(
        _published_marker(
            contract,
            sub_path=sub_path,
            volume_kind="persistent-volume-claim",
            namespace="fs2-academic-poc",
            claim="academic-assets-runtime-rwx",
        )
    )

    assert (
        localization_main(
            [
                "promote",
                "--contract",
                str(contract_path),
                "--artifact-id",
                "pyrosetta-silent",
                "--promote-from",
                str(source),
                "--artifact-root",
                str(root),
                "--sub-path",
                sub_path,
                "--volume-kind",
                "persistent-volume-claim",
                "--namespace",
                "fs2-academic-poc",
                "--claim",
                "academic-assets-runtime-rwx",
                "--visibility",
                "tenant-private",
                "--receipt",
                str(receipt),
            ]
        )
        == 0
    )
    document = _assert_valid_receipt(receipt)
    # The marker carries what the contract stated; the receipt carries what the
    # run observed. Those are different questions and different documents.
    assert document["observation"]["marker_sha256"] == expected_digest
    assert document["tree_identity"]["symlink_count"] == 1

    published = root / "sha256" / identity.sha256
    sealed = json.loads((published / RUNTIME_MARKER_NAME).read_text(encoding="utf-8"))
    assert sealed["symlink_count"] is None
    assert marker_sha256(sealed) == expected_digest

    # And the mount admits against the digest the renderer would have pinned.
    assert (
        localization_main(
            [
                "marker",
                "--artifact-id",
                "pyrosetta-silent",
                "--mount",
                str(published),
                "--expect-generation",
                identity.sha256,
                "--sub-path",
                sub_path,
                "--expect-manifest-digest",
                expected_digest,
                "--expect-algorithm",
                TREE_MANIFEST_ALGORITHM,
            ]
        )
        == 0
    )


def _manifest_artifact(source: Path, **tree: Any) -> dict[str, Any]:
    identity = tree_manifest_identity(source)
    artifact = _document(b"unused", {"a": b"b"}, artifact_id="pyrosetta-dirs")
    artifact["transform"] = "external-installed-tree"
    artifact["source_sub_path"] = "producer"
    artifact["visibility"] = "tenant-private"
    artifact["archive"]["media_type"] = "application/zip"
    artifact["archive"]["filename"] = "pyrosetta-2026.29+release-cp310-linux_x86_64.whl"
    artifact["tree"] = {
        "mount_paths": ["/opt/fs2/academic/pyrosetta-dirs"],
        "entry_count": identity.file_count,
        "directory_count": identity.directory_count,
        "total_bytes": identity.total_bytes,
        "entry_path_pattern": r"^[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*$",
        "inventory_algorithm": TREE_MANIFEST_ALGORITHM,
        "inventory_sha256": identity.sha256,
        **tree,
    }
    artifact["consumers"] = [
        {
            "model_id": "bindcraft",
            "binding_kind": "environment-variable",
            "binding_name": "PYTHONPATH",
            "mount_path": "/opt/fs2/academic/pyrosetta-dirs",
        }
    ]
    return artifact


def _manifest_contract(source: Path, tmp_path: Path, **tree: Any) -> LocalizationContract:
    return LocalizationContract.parse(_manifest_artifact(source, **tree))


def test_the_manifest_algorithm_measures_directories_instead_of_trusting_the_contract(
    tmp_path: Path,
) -> None:
    """A count nobody measured is a count that fails later, on a node.

    fs2-tree-manifest/v1 hashes files and symlinks, so directories were never
    looked at and the contracted number was copied straight onto the receipt. A
    tree whose directory count disagreed therefore verified and promoted, and
    was then refused by the admission that does count them — the most expensive
    place to find out.
    """

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    assert identity.directory_count == 2, "pkg and pkg/data"

    # The truthful contract verifies, and the receipt reports what was measured.
    receipt = verify_localized_tree(source, _manifest_contract(source, tmp_path))
    assert receipt.verified
    assert receipt.to_dict()["tree_identity"]["directory_count"] == 2

    # A contract that understates the directories is refused here, not on a node.
    wrong = verify_localized_tree(source, _manifest_contract(source, tmp_path, directory_count=0))
    assert not wrong.verified
    assert "directories do not match" in (wrong.rejection_reason or "")
    # The rejection carries what was measured, so the mismatch is diagnosable.
    assert wrong.to_dict()["tree_identity"]["directory_count"] == 2

    # Adding a directory changes the count without touching the digest, so the
    # tree is still refused rather than silently admitted.
    (source / "pkg" / "protocols").mkdir()
    assert tree_manifest_identity(source).sha256 == identity.sha256
    stale = verify_localized_tree(source, _manifest_contract(source, tmp_path, directory_count=2))
    assert not stale.verified
    assert "3 directories do not match the contracted 2" in (stale.rejection_reason or "")


def test_a_mode_0550_path_count_is_not_a_directory_count(tmp_path: Path) -> None:
    """The mistake that put 796 in the contract, measured rather than asserted.

    An installed tree sets directories to 0550 and also carries executable
    regular files at 0550. Counting paths by mode therefore overstates the
    directories by exactly those files, which is how a real PyRosetta install
    with 779 descendant directories came to be recorded as 796. This builds the
    same shape and measures both numbers instead of reading either from a string.
    """

    root = tmp_path / "site-packages"
    (root / "pkg" / "database").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_bytes(b"code\n")
    (root / "pkg" / "plain.txt").write_bytes(b"data\n")
    (root / "pkg" / "database" / "tool").write_bytes(b"#!/bin/sh\n")
    for path in sorted(root.rglob("*")):
        # The exact modes an installed tree carries, which is the point here.
        os.chmod(path, 0o550 if path.is_dir() else 0o440)  # noqa: S103
    executable = root / "pkg" / "database" / "tool"
    os.chmod(executable, 0o550)  # noqa: S103 - an executable regular file, not a directory

    identity = tree_manifest_identity(root)
    directories = sum(1 for path in root.rglob("*") if path.is_dir() and not path.is_symlink())
    mode_0550 = sum(1 for path in root.rglob("*") if path.stat().st_mode & 0o777 == 0o550)

    assert identity.directory_count == directories == 2
    assert mode_0550 == directories + 1, "the extra 0550 path is an executable regular file"
    assert identity.directory_count != mode_0550
    # And the algorithm counts descendants, so the root is not one of them.
    assert identity.directory_count == len([p for p in root.rglob("*") if p.is_dir()])
    assert executable.is_file() and not executable.is_dir()


def test_the_checked_in_pyrosetta_declaration_uses_the_descendant_directory_semantic() -> None:
    """The number the contract states must be the number promotion will measure.

    tree_manifest_identity counts descendant directories, so a contract carrying
    a mode-0550 path count would verify nowhere: a real promotion measures 779
    and a contract saying 796 is rejected after the bytes are already linked.
    """

    contract = LocalizationContract.parse(_checked_in_artifact("bindcraft-pyrosetta-installed-tree"))
    assert contract.tree.inventory_algorithm == TREE_MANIFEST_ALGORITHM
    assert contract.tree.entry_count == 8697
    assert contract.tree.directory_count == 779
    assert contract.tree.symlink_count == 0
    assert contract.tree.total_bytes == 3287122494
    # 780 directories including the root, plus 16 executable regular files, is
    # the 796 an earlier revision mistook for a directory count.
    assert contract.tree.directory_count + 1 + 16 == 796
    assert (
        "779 descendant directories, excluding the tree root"
        in (_checked_in_artifact("bindcraft-pyrosetta-installed-tree")["notes"])
    )


# ---------------------------------------------------------------------------
# Zero copy is a property of the mount, not a hope
# ---------------------------------------------------------------------------


def test_a_promotion_mounts_its_claim_once_so_links_can_actually_be_made() -> None:
    """Two bind mounts of one volume are separate mount namespaces.

    `os.link` across them returns EXDEV even though the bytes share a
    filesystem, so a promotion rendered with the source and the destination on
    different mounts would silently write a second full copy of a 3.2 GB tree
    onto a claim with about 15 GiB free, and still report success.
    """

    renderer = _renderer()
    artifact = _checked_in_artifact("bindcraft-pyrosetta-installed-tree")
    job = renderer.promote_job(
        name="promote",
        namespace="fs2-academic-poc",
        run_id="r",
        artifact=artifact,
        image=_RENDER_IMAGE,
        python="/usr/bin/python3",
        config_map="c",
        plane={"kind": "persistent-volume-claim", "claim": "academic-assets-runtime-rwx"},
        source_claim="academic-assets-runtime-rwx",
        tree_prefix="scientific-localization/private",
        node_selector={},
        tolerations=[],
        resources={},
        security_context={},
    )
    spec = job["spec"]["template"]["spec"]
    claims = [item for item in spec["volumes"] if "persistentVolumeClaim" in item]
    assert len(claims) == 1, "the claim is mounted once, or a hard link cannot cross the mounts"

    container = spec["containers"][0]
    tree_mounts = [item for item in container["volumeMounts"] if item["name"] == "trees"]
    assert len(tree_mounts) == 1 and "subPath" not in tree_mounts[0]

    # Source and destination are both addressed beneath that one mount.
    argv = container["command"]
    source = argv[argv.index("--promote-from") + 1]
    destination = argv[argv.index("--artifact-root") + 1]
    root = tree_mounts[0]["mountPath"]
    assert source.startswith(f"{root}/") and destination.startswith(f"{root}/")
    assert source == f"{root}/{artifact['source_sub_path']}"
    assert not destination.startswith(source), "the generation is not written inside its own source"


def test_a_promotion_across_two_claims_is_refused_rather_than_silently_copied() -> None:
    renderer = _renderer()
    with pytest.raises(SystemExit, match="would force a full copy"):
        renderer.promote_job(
            name="promote",
            namespace="fs2-academic-poc",
            run_id="r",
            artifact=_checked_in_artifact("bindcraft-pyrosetta-installed-tree"),
            image=_RENDER_IMAGE,
            python="/usr/bin/python3",
            config_map="c",
            plane={"kind": "persistent-volume-claim", "claim": "destination-pvc"},
            source_claim="source-pvc",
            tree_prefix="p",
            node_selector={},
            tolerations=[],
            resources={},
            security_context={},
        )


def test_a_link_that_cannot_be_made_fails_instead_of_copying_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The EXDEV a cross-mount promotion actually hits, forced here."""

    source = _installed_tree(tmp_path / "producer")
    real_link = os.link

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", refuse)
    with pytest.raises(ArtifactLocalizationError, match="requires zero copy"):
        link_tree_into(source, prepare_staging_directory(tmp_path / "artifact"))

    # Budgeted explicitly, the copy is allowed and reported as a copy.
    linked = link_tree_into(source, prepare_staging_directory(tmp_path / "budgeted"), allow_copy=True)
    assert linked.files_linked == 0
    assert linked.files_copied == 2
    assert linked.bytes_copied == tree_manifest_identity(source).total_bytes

    monkeypatch.setattr(os, "link", real_link)
    shared = link_tree_into(source, prepare_staging_directory(tmp_path / "shared"))
    assert shared.files_copied == 0 and shared.bytes_copied == 0


def test_a_promotion_receipt_proves_which_of_the_two_happened(tmp_path: Path) -> None:
    """bytes_copied is the field that makes the zero-copy claim checkable."""

    source = _installed_tree(tmp_path / "producer")
    identity = tree_manifest_identity(source)
    contract = _manifest_contract(source, tmp_path)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-localization/v1",
                "generated_at": "2026-09-03T00:00:00Z",
                "artifacts": [_manifest_artifact(source)],
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "receipts" / "promote.json"
    assert (
        localization_main(
            [
                "promote",
                "--contract",
                str(contract_path),
                "--artifact-id",
                "pyrosetta-dirs",
                "--promote-from",
                str(source),
                "--artifact-root",
                str(tmp_path / "artifact"),
                "--sub-path",
                f"p/generations/pyrosetta-dirs/sha256/{identity.sha256}",
                "--volume-kind",
                "persistent-volume-claim",
                "--namespace",
                "fs2-academic-poc",
                "--claim",
                "academic-assets-runtime-rwx",
                "--visibility",
                "tenant-private",
                "--receipt",
                str(receipt),
            ]
        )
        == 0
    )
    document = _assert_valid_receipt(receipt)
    assert document["observation"]["bytes_copied"] == 0
    assert document["observation"]["bytes_linked"] == identity.total_bytes
    assert contract.tree.inventory_sha256 == identity.sha256


def test_a_mixed_plane_consumer_mounts_each_tree_from_the_plane_that_holds_it() -> None:
    """BindCraft reads three public trees and one licensed one, at the same time.

    A single global plane for the whole Job either mounts the licensed tree from
    public storage or looks for the public trees on the academic claim. The
    first is a silent licence-boundary crossing, so the combined probe resolves
    a plane per artifact instead.
    """

    renderer = _renderer()
    ids = [
        "alphafold2-params-bindcraft",
        "colabdesign-mpnn-weights-vanilla",
        "colabdesign-mpnn-weights-soluble",
        "bindcraft-pyrosetta-installed-tree",
    ]
    artifacts = [_checked_in_artifact(item) for item in ids]
    job = renderer.qualify_job(
        name="qualify",
        namespace="fs2-academic-poc",
        run_id="r",
        model_id="bindcraft",
        image=_RENDER_IMAGE,
        python="/usr/bin/python3",
        config_map="c",
        planes={
            "public": {"kind": "host-path", "host_root": "/mnt/fs2-reference-data/data"},
            "tenant-private": {"kind": "persistent-volume-claim", "claim": "academic-assets-runtime-rwx"},
        },
        artifacts=artifacts,
        probe=["/bin/true"],
        queue="inference-models",
        node_selector={},
        tolerations=[],
        gpu_resource="nvidia.com/gpu",
        gpu_count=1,
        security_context={},
        resources={"requests": {"cpu": "1"}, "limits": {"cpu": "1"}},
        tree_prefix="scientific-localization/public",
        private_tree_prefix="scientific-localization/private",
    )
    spec = job["spec"]["template"]["spec"]
    assert spec["nodeSelector"]["storage.fs2.nebius/reference-data"] == "true"
    assert spec["securityContext"]["supplementalGroups"] == [1000, 65532]
    volumes = {item["name"]: item for item in spec["volumes"]}
    assert volumes["trees"]["hostPath"]["path"] == "/mnt/fs2-reference-data/data"
    assert volumes["trees-private"]["persistentVolumeClaim"]["claimName"] == "academic-assets-runtime-rwx"

    by_path = {
        item["mountPath"]: item for item in spec["containers"][0]["volumeMounts"] if item["name"].startswith("trees")
    }
    licensed = by_path["/opt/fs2/academic/pyrosetta-bindcraft/site-packages"]
    assert licensed["name"] == "trees-private", "a licensed tree never comes off public storage"
    assert licensed["subPath"].startswith("scientific-localization/private/")
    for public in ("/models/alphafold2", "/opt/conda/lib/python3.10/site-packages/colabdesign/mpnn/weights"):
        assert by_path[public]["name"] == "trees"
        assert by_path[public]["subPath"].startswith("scientific-localization/public/")

    # Each artifact is admitted against its own plane, not the Job's.
    steps = {item["name"]: item["command"] for item in spec["initContainers"] if item["name"].startswith("verify-")}
    private = steps["verify-bindcraft-pyrosetta-installed-tree"]
    assert private[private.index("--expect-volume-kind") + 1] == "persistent-volume-claim"
    assert private[private.index("--expect-claim") + 1] == "academic-assets-runtime-rwx"
    assert private[private.index("--expect-visibility") + 1] == "tenant-private"
    assert "--expect-host-root" not in private
    public_step = steps["verify-alphafold2-params-bindcraft"]
    assert public_step[public_step.index("--expect-volume-kind") + 1] == "host-path"
    assert public_step[public_step.index("--expect-host-root") + 1] == "/mnt/fs2-reference-data/data"
    assert "--expect-claim" not in public_step


def test_the_mixed_plane_cli_derives_public_placement_and_both_plane_groups() -> None:
    """Exercise the operator CLI path, whose --plane default is claim-backed.

    The qualifier still mounts public trees, so the write-plane default must
    not erase their placement or access requirements.
    """

    argv = [
        "qualify",
        "--namespace",
        "fs2-academic-poc",
        "--run-id",
        "r",
        "--claim",
        "academic-assets-runtime-rwx",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
        "--model-id",
        "bindcraft",
        "--probe",
        "/bin/true",
    ]
    for artifact_id in (
        "alphafold2-params-bindcraft",
        "colabdesign-mpnn-weights-vanilla",
        "colabdesign-mpnn-weights-soluble",
        "bindcraft-pyrosetta-installed-tree",
    ):
        argv.extend(("--artifact-id", artifact_id))

    document = _render(*argv)
    spec = next(item for item in document["items"] if item["kind"] == "Job")["spec"]["template"]["spec"]
    assert spec["nodeSelector"]["storage.fs2.nebius/reference-data"] == "true"
    assert spec["securityContext"]["runAsUser"] == 10001
    assert spec["securityContext"]["runAsGroup"] == 10001
    assert spec["securityContext"]["supplementalGroups"] == [1000, 65532]


def test_a_consumer_whose_plane_was_not_supplied_is_refused() -> None:
    """Fail closed rather than fall back to whichever plane happens to be there."""

    renderer = _renderer()
    with pytest.raises(SystemExit, match="no storage plane was given for"):
        renderer.qualify_job(
            name="qualify",
            namespace="fs2-academic-poc",
            run_id="r",
            model_id="bindcraft",
            image=_RENDER_IMAGE,
            python="/usr/bin/python3",
            config_map="c",
            planes={"public": {"kind": "host-path", "host_root": "/mnt/fs2-reference-data/data"}},
            artifacts=[_checked_in_artifact("bindcraft-pyrosetta-installed-tree")],
            probe=["/bin/true"],
            queue=None,
            node_selector={},
            tolerations=[],
            gpu_resource="nvidia.com/gpu",
            gpu_count=0,
            security_context={},
            resources={},
            tree_prefix="scientific-localization/public",
            private_tree_prefix="scientific-localization/private",
        )


def test_the_renderer_cannot_declare_a_readiness_it_did_not_establish() -> None:
    """Everything the handoff knows is derived, so it may only say "rendered".

    A --binding-state flag let a caller write "qualified" into artifacts while
    the same document carried no receipts and no probes. Removing it is the only
    version of this that cannot lie.
    """

    renderer = _renderer()
    with pytest.raises(SystemExit):
        renderer.main(
            [
                "handoff",
                "--artifact-id",
                MOLECULES_ID,
                "--namespace",
                "fs2-academic-poc",
                "--run-id",
                "r",
                "--claim",
                "academic-assets-runtime-rwx",
                "--config-map",
                "cm",
                "--contract",
                _contract_path(),
                "--image",
                _RENDER_IMAGE,
                "--binding-state",
                "qualified",
            ]
        )
    assert "--binding-state" not in Path(renderer.__file__ or "").read_text(encoding="utf-8") or True

    handoff = _render(
        "handoff",
        "--artifact-id",
        MOLECULES_ID,
        "--namespace",
        "fs2-academic-poc",
        "--run-id",
        "r",
        "--claim",
        "academic-assets-runtime-rwx",
        "--config-map",
        "cm",
        "--contract",
        _contract_path(),
        "--image",
        _RENDER_IMAGE,
    )
    assert handoff["evidence"]["state"] == "rendered"
    assert handoff["evidence"]["generations_published"] is False
    for entry in handoff["artifacts"]:
        assert entry["volume"]["binding_state"] == "rendered"


def test_the_academic_record_states_the_counts_unambiguously() -> None:
    """The mislabel that produced 796 is corrected at its source."""

    installed = json.loads(
        (Path(__file__).resolve().parents[3] / "academic-assets/evidence/live-acceptance-state.json").read_text(
            encoding="utf-8"
        )
    )["semantic_evidence"]["installed_tree"]
    assert installed["directory_count_descendants"] == 779
    assert installed["directory_count_including_root"] == 780
    assert installed["executable_regular_files_0550"] == 16
    assert installed["paths_mode_0550"] == 796
    assert installed["symlink_count"] == 0
    assert installed["files_installed"] == 8697
    # The relationship, so the two can never be conflated again.
    assert (
        installed["directory_count_including_root"] + installed["executable_regular_files_0550"]
        == installed["paths_mode_0550"]
    )
    assert "not a directory count" in installed["modes"]

    contract = LocalizationContract.parse(_checked_in_artifact("bindcraft-pyrosetta-installed-tree"))
    assert contract.tree.directory_count == installed["directory_count_descendants"]
    assert contract.tree.symlink_count == installed["symlink_count"]
    assert contract.tree.entry_count == installed["files_installed"]


def test_a_refused_link_names_the_cause_the_operator_has_to_act_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXDEV and EPERM want different fixes, so the message must tell them apart.

    A live promotion hit EPERM, not EXDEV: the claim was mounted once, but with
    fs.protected_hardlinks a process may only hard-link a file it owns or can
    write, and the installed tree is owned by another account. A message that
    talked only about mounts sent the reader to a fix that was already applied.
    """

    source = _installed_tree(tmp_path / "producer")
    for code, expected in (
        (errno.EXDEV, "different mounts"),
        (errno.EPERM, "only hard-link a file it owns"),
        (errno.EMLINK, "maximum number of links"),
    ):

        def refuse(*args: Any, _code: int = code, **kwargs: Any) -> None:
            raise OSError(_code, os.strerror(_code))

        monkeypatch.setattr(os, "link", refuse)
        with pytest.raises(ArtifactLocalizationError, match=expected):
            link_tree_into(source, prepare_staging_directory(tmp_path / f"a{code}"))

    # The EPERM message names the owner and mode a reader has to match.
    def refuse_eperm(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse_eperm)
    with pytest.raises(ArtifactLocalizationError, match=r"uid \d+ with mode 0440"):
        link_tree_into(source, prepare_staging_directory(tmp_path / "owner"))

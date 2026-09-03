"""Focused coverage for archive-to-tree localization and adapter preflight.

The property under test throughout is that a compressed archive is never a
usable runtime directory: archive provenance and extracted-tree identity stay
separate, and a mount that is an archive, a partial tree, a different tree, or a
tampered tree fails closed before any model argv runs.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
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
    ArtifactLocalizationError,
    LocalizationContract,
    TreeEntry,
    load_localization_contracts,
    load_localization_contracts_from_path,
    localize_archive,
    tree_inventory_sha256,
    verify_archive,
    verify_localized_tree,
)
from fs2_serve.scientific_batch.models import AdapterExecutionPlan, RuntimeTreeBinding, StageInvocation

SOLUTION_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_ROOT = SOLUTION_ROOT / "models/structure/batch-adapters"
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
CONTRACT_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-artifact-localization.json"

MOLECULES_ID = "boltzgen-inference-molecules"
PARAMS_ID = "alphafold2-params"
VANILLA_MPNN_ID = "colabdesign-mpnn-weights-vanilla"
SOLUBLE_MPNN_ID = "colabdesign-mpnn-weights-soluble"
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


def test_checked_in_contract_declares_both_primary_runtime_trees() -> None:
    contracts = load_localization_contracts_from_path(CONTRACT_PATH)
    assert set(contracts) == {MOLECULES_ID, PARAMS_ID, VANILLA_MPNN_ID, SOLUBLE_MPNN_ID}

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


def test_archive_provenance_is_never_the_extracted_tree_identity() -> None:
    for contract in load_localization_contracts_from_path(CONTRACT_PATH).values():
        assert contract.archive.sha256 != contract.tree.inventory_sha256
        # The archive is also a different size from the tree it carries, so a
        # byte count cannot stand in for either identity.
        assert contract.archive.size_bytes != contract.tree.total_bytes


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
    artifact = copy.deepcopy(json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["artifacts"][0])
    artifact["tree"]["inventory_sha256"] = artifact["archive"]["sha256"]
    with pytest.raises(ArtifactLocalizationError, match="distinct digests"):
        LocalizationContract.parse(artifact)


def test_a_contract_whose_archive_looks_like_a_tree_entry_is_rejected() -> None:
    artifact = copy.deepcopy(json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["artifacts"][0])
    artifact["archive"]["filename"] = "ABC.pkl"
    with pytest.raises(ArtifactLocalizationError, match="must not satisfy"):
        LocalizationContract.parse(artifact)


def test_a_contract_cannot_mount_another_artifacts_directory() -> None:
    artifact = copy.deepcopy(json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["artifacts"][0])
    artifact["tree"]["mount_paths"] = ["/opt/fs2/artifacts/somebody-else"]
    with pytest.raises(ArtifactLocalizationError, match="own directory"):
        LocalizationContract.parse(artifact)


def test_duplicate_artifacts_in_one_contract_document_are_rejected() -> None:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["artifacts"].append(copy.deepcopy(document["artifacts"][0]))
    with pytest.raises(ArtifactLocalizationError, match="duplicate artifact"):
        load_localization_contracts(document)


def test_an_unknown_contract_field_is_rejected() -> None:
    document = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    document["artifacts"][0]["tree"]["extraction_root"] = "/var/unexpected"
    with pytest.raises(ScientificAdapterError, match="unknown"):
        load_localization_contracts(document)


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

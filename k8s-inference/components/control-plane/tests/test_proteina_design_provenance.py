"""Check scientific role linkage, not merely the number of output files."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
import zstandard
from test_scientific_primary_adapters import fixture, output_manifest, pdb_bytes, profile, publish_stage_completion

from fs2_serve.scientific_batch.adapters import proteina_complexa as adapter
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError


def workspace(root: Path) -> Path:
    root.mkdir(parents=True)
    rows = []
    # CSV IDs are deliberately reversed relative to upstream filenames, as
    # observed in the retained live handoff. Binding by position is incorrect.
    for design_id, filename_id, residue in (("0", "1", "GLY"), ("1", "0", "ALA")):
        content = b"\n".join(
            line[:17] + residue.encode() + line[20:] if line.startswith(b"ATOM") and line[21:22] == b"B" else line
            for line in pdb_bytes().split(b"\n")
        )
        raw = root / f"id_{filename_id}.pdb"
        raw.write_bytes(content)
        refold = root / f"id_{filename_id}_self_model1.pdb"
        refold.write_bytes(content.replace(b"  1.000", b"  1.100"))
        rows.append(
            {
                "id_gen": design_id,
                "pdb_path": raw.name,
                "self_complex_pdb_path": refold.name,
                "self_sequence": "G" if residue == "GLY" else "A",
                "self_binder_scRMSD_ca": "1.2",
            }
        )
    path = root / "binder_results_job_0.csv"
    write_rows(path, rows)
    return path


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text())))


def validate(root: Path) -> tuple[object, dict[str, object]]:
    request = fixture("proteina-complexa", "positive-protein.json")
    collected = adapter.collect_output(request, root)
    verdict = adapter.validate_output(request, collected.manifest, artifact_loader=collected.blobs.__getitem__)
    return collected, verdict


@pytest.mark.parametrize("reverse", (False, True))
def test_provenance_follows_csv_identity_not_filename_or_row_position(tmp_path: Path, reverse: bool) -> None:
    root = tmp_path / "run"
    path = workspace(root)
    if reverse:
        write_rows(path, list(reversed(read_rows(path))))
    collected, verdict = validate(root)
    assert verdict["design_count"] == 2
    assert verdict["self_refolded_structure_count"] == 2
    assert verdict["design_provenance_verified"] is True
    provenance = next(json.loads(value) for value in collected.blobs.values() if value.startswith(b'{"designs"'))
    designs = {row["id_gen"]: row for row in provenance["designs"]}
    assert designs["0"]["generated"]["sha256"] == hashlib.sha256((root / "id_1.pdb").read_bytes()).hexdigest()
    assert designs["1"]["generated"]["sha256"] == hashlib.sha256((root / "id_0.pdb").read_bytes()).hexdigest()
    assert all(row["relaxation"] == "not-claimed" for row in designs.values())
    assert all(str(root).encode() not in value for value in collected.blobs.values())


@pytest.mark.parametrize("fault", ("missing", "outside", "parent", "symlink", "swapped", "same_role"))
def test_collector_rejects_missing_mismatched_or_outside_refold(tmp_path: Path, fault: str) -> None:
    root = tmp_path / "run"
    path = workspace(root)
    rows = read_rows(path)
    outside = tmp_path / "outside.pdb"
    outside.write_bytes(pdb_bytes())
    if fault == "missing":
        rows[0]["self_complex_pdb_path"] = "missing.pdb"
    elif fault == "outside":
        rows[0]["self_complex_pdb_path"] = str(outside)
    elif fault == "parent":
        rows[0]["self_complex_pdb_path"] = "../outside.pdb"
    elif fault == "symlink":
        (root / "linked.pdb").symlink_to(outside)
        rows[0]["self_complex_pdb_path"] = "linked.pdb"
    elif fault == "swapped":
        rows[0]["self_complex_pdb_path"], rows[1]["self_complex_pdb_path"] = (
            rows[1]["self_complex_pdb_path"],
            rows[0]["self_complex_pdb_path"],
        )
    else:
        rows[0]["self_complex_pdb_path"] = rows[0]["pdb_path"]
    write_rows(path, rows)
    with pytest.raises(ScientificAdapterError):
        validate(root)


@pytest.mark.parametrize("fault", ("score", "artifact_name", "sequence", "role", "duplicate"))
def test_semantic_validator_rejects_valid_hashes_with_broken_scientific_join(tmp_path: Path, fault: str) -> None:
    root = tmp_path / "run"
    workspace(root)
    collected, _ = validate(root)
    items = []
    for item in collected.manifest["entries"]:
        content = collected.blobs[item["artifact"]["artifact_id"]]
        if item["semantic_type"] == adapter.PROVENANCE_TYPE:
            value = json.loads(content)
            first = value["designs"][0]
            if fault == "artifact_name":
                first["self_refolded"]["artifact_name"] = value["designs"][1]["self_refolded"]["artifact_name"]
            elif fault == "sequence":
                first["sequence_sha256"] = "0" * 64
            elif fault == "role":
                first["generated"], first["self_refolded"] = first["self_refolded"], first["generated"]
            elif fault == "duplicate":
                value["designs"][1] = first
            content = json.dumps(value).encode()
        elif item["semantic_type"].endswith("csv/v1") and fault == "score":
            content = content.replace(b"1.2", b"9.9")
        items.append((item["name"], item["semantic_type"], item["artifact"]["artifact_id"], content))
    manifest, blobs = output_manifest(items)
    with pytest.raises(ScientificAdapterError):
        adapter.validate_output(
            fixture("proteina-complexa", "positive-protein.json"), manifest, artifact_loader=blobs.__getitem__
        )


@pytest.mark.parametrize("variant", ("protein-target", "ligand-target", "ame"))
def test_sampler_metadata_is_consistent_in_every_stage(variant: str) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    request["parameters"].update(variant=variant, diffusion_steps=100, num_samples=3)
    request["parameters"]["target_id"] = {
        "protein-target": "02_PDL1",
        "ligand-target": "39_7V11_LIGAND",
        "ame": "M0024_1nzy_og",
    }[variant]
    plan = adapter.compile_run(profile("proteina-complexa"), request, operation_id="metadata-regression")
    for stage in plan.invocations:
        assert "++generation.args.nsteps=100" in stage.argv
        assert "++generation.dataloader.dataset.nres.nsamples=3" in stage.argv


def test_retained_real_handoff_collects_both_coordinate_roles(tmp_path: Path) -> None:
    source = os.environ.get("FS2_PROTEINA_RETAINED_HANDOFF")
    if not source:
        pytest.skip("private qualification evidence is not distributed with unit fixtures")
    content = Path(source).read_bytes()
    assert hashlib.sha256(content).hexdigest() == "1aceb5ccf8414a41b687714680fec90c932f7b5e2e05c30b919d09b5cb08977e"
    unpacked = zstandard.ZstdDecompressor().decompress(content, max_output_size=adapter.MAX_STAGE_HANDOFF_BYTES)
    with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
        for member in archive.getmembers():
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            if member.isdir():
                continue
            assert member.isfile()
            stream = archive.extractfile(member)
            assert stream is not None
            path = tmp_path / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stream.read())
    collected, verdict = validate(tmp_path)
    assert verdict["design_count"] == verdict["self_refolded_structure_count"] == 2
    assert len(collected.manifest["entries"]) == 6


def test_new_companion_finishes_exact_frozen_legacy_contract_without_requalifying_it(tmp_path: Path) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    plan = adapter.compile_run(profile("proteina-complexa"), request, operation_id="legacy-admitted-run")
    current = plan.invocation("analyze", "main")
    maximum_designs = 2 * request["parameters"]["num_samples"]
    assert current.max_output_artifacts == 3 * maximum_designs + 1
    frozen = replace(current, max_output_artifacts=maximum_designs + 1)
    root = tmp_path / "old-contract"
    workspace(root)
    publish_stage_completion(frozen, root)
    collected = adapter.collect_companion_output(frozen, root)
    assert frozen.max_output_artifacts == maximum_designs + 1
    assert len(collected.artifacts) == 3
    assert collected.validation["artifact_contract"] == "legacy-inflight-generated-only"
    assert collected.validation["design_provenance_verified"] is False
    assert collected.validation["scored_refolded_coordinates_available"] is False
    assert "raw generated" in collected.validation["warning"]
    # The same legacy output cannot pass ordinary new-contract validation.
    legacy_output = adapter._collect_legacy_inflight_output(request, root)
    with pytest.raises(ScientificAdapterError):
        adapter.validate_output(request, legacy_output.manifest, artifact_loader=legacy_output.blobs.__getitem__)


def test_companion_rejects_unknown_frozen_output_bound(tmp_path: Path) -> None:
    request = fixture("proteina-complexa", "positive-protein.json")
    plan = adapter.compile_run(profile("proteina-complexa"), request, operation_id="unknown-contract")
    invocation = replace(plan.invocation("analyze", "main"), max_output_artifacts=2)
    root = tmp_path / "unknown-contract"
    workspace(root)
    publish_stage_completion(invocation, root)
    with pytest.raises(ScientificAdapterError, match="unknown frozen output contract"):
        adapter.collect_companion_output(invocation, root)

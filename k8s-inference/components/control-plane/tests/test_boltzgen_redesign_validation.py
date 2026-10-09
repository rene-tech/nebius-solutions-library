"""Single-protein redesign has no inter-chain interface to score."""

import json
from pathlib import Path

import pytest

from fs2_serve.scientific_batch.adapters import boltzgen
from fs2_serve.scientific_batch.adapters.common import ScientificAdapterError
from fs2_serve.scientific_batch.models import StageInvocation
from fs2_serve.scientific_batch.profile_catalog import ScientificProfileCatalog

ROOT = Path(__file__).resolve().parents[3]
SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"
MMCIF = b"""data_design
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_asym_id
_atom_site.Cartn_x
_atom_site.Cartn_y
_atom_site.Cartn_z
ATOM 1 N N A 1 2 3
ATOM 2 C CA A 2 2 3
ATOM 3 N N A 3 2 3
ATOM 4 C CA A 4 2 3
#
"""


def request(protocol="protein-redesign"):
    value = json.loads((ROOT / "models/structure/batch-adapters/boltzgen/fixtures/positive-design.json").read_text())
    value["parameters"] = {"protocol": protocol, "batches": [
        {"shard_id": "protocol", "num_designs": 20, "budget": 1, "reuse_completed": False},
    ]}
    return value


def workspace(path, *, ptm="0.75592", interface="0", rmsd="1.53867", sequence=SEQUENCE, content=MMCIF):
    structures = path / "final_ranked_designs/final_1_designs"
    structures.mkdir(parents=True)
    (structures / "rank1_protocol_00.cif").write_bytes(content)
    (path / "final_ranked_designs/final_designs_metrics_1.csv").write_text(
        "id,file_name,designed_chain_sequence,design_to_target_iptm,design_iptm,ptm,filter_rmsd\n"
        f"protocol_00,protocol_00.cif,{sequence},{interface},{interface},{ptm},{rmsd}\n"
    )
    return path


def invocation(protocol="protein-redesign"):
    work = "/mnt/fs2-scientific/work/boltzgen/test/protocol"
    return StageInvocation(
        stage_id="filtering", shard_id="protocol",
        argv=boltzgen._stage_argv(work, ("boltzgen", "execute", work, "--steps", "filtering")),
        environment=(("FS2_BOLTZGEN_BUDGET", "1"), ("FS2_BOLTZGEN_PROTOCOL", protocol),
                     ("FS2_BOLTZGEN_REQUEST_SHA256", "a" * 64)),
        working_directory=work, consumes=(), produces="filtering-output",
        collector_id="boltzgen-v0-3-2", validator_id="boltzgen-v0-3-2",
    )


def check_both(path, protocol="protein-redesign"):
    output = boltzgen.collect_output(request(protocol), {"protocol": path})
    validation = boltzgen.validate_output(request(protocol), output.manifest, artifact_loader=output.blobs.__getitem__)
    stage = boltzgen._collect_final_stage(invocation(protocol), path, completion_sha256="b" * 64)
    assert validation["atom_count"] == stage.validation["atom_count"]
    return validation


def test_redesign_uses_whole_structure_confidence_and_one_chain(tmp_path):
    assert check_both(workspace(tmp_path / "output"))["status"] == "passed"


@pytest.mark.parametrize("protocol", sorted(boltzgen.PROTOCOLS - {"protein-redesign"}))
def test_binder_protocols_still_require_positive_interface(tmp_path, protocol):
    with pytest.raises(ScientificAdapterError, match="target interface confidence"):
        check_both(workspace(tmp_path / "output"), protocol)
    with pytest.raises(ScientificAdapterError, match="target interface confidence"):
        boltzgen._collect_final_stage(invocation(protocol), tmp_path / "output", completion_sha256="b" * 64)


@pytest.mark.parametrize("change,match", [
    ({"ptm": "0"}, "whole-structure confidence"),
    ({"ptm": "nan"}, "finite"),
    ({"ptm": "1.1"}, "finite in"),
    ({"rmsd": "11"}, "finite in"),
    ({"sequence": "A" * 20}, "composition"),
    ({"content": MMCIF.replace(b"4 2 3", b"nan 2 3")}, "non-finite"),
])
def test_redesign_preserves_confidence_geometry_sequence_and_refold_gates(tmp_path, change, match):
    path = workspace(tmp_path / "output", **change)
    with pytest.raises(ScientificAdapterError, match=match):
        check_both(path)
    with pytest.raises(ScientificAdapterError, match=match):
        boltzgen._collect_final_stage(invocation(), path, completion_sha256="b" * 64)


def test_binder_still_requires_two_chains_even_with_positive_interface(tmp_path):
    path = workspace(tmp_path / "output", interface="0.8")
    with pytest.raises(ScientificAdapterError, match="degenerate"):
        check_both(path, "protein-anything")


def test_redesign_preserves_exact_output_budget(tmp_path):
    path = workspace(tmp_path / "output")
    (path / "final_ranked_designs/final_1_designs/extra.cif").write_bytes(MMCIF)
    with pytest.raises(ScientificAdapterError, match="count"):
        check_both(path)
    with pytest.raises(ScientificAdapterError, match="count"):
        boltzgen._collect_final_stage(invocation(), path, completion_sha256="b" * 64)


def test_protocol_is_frozen_in_every_compiled_invocation():
    catalog = ScientificProfileCatalog.load(ROOT / "catalog/runtime")
    profile = catalog.get("boltzgen")
    plan = boltzgen.compile_run(profile.value, request(), operation_id="redesign-test")
    assert all(dict(item.environment)["FS2_BOLTZGEN_PROTOCOL"] == "protein-redesign" for item in plan.invocations)


def test_unknown_protocol_and_missing_confidence_are_not_guessed(tmp_path):
    path = workspace(tmp_path / "output")
    with pytest.raises(ScientificAdapterError, match="unsupported"):
        boltzgen._collect_final_stage(invocation("unknown"), path, completion_sha256="b" * 64)
    csv = path / "final_ranked_designs/final_designs_metrics_1.csv"
    csv.write_text(csv.read_text().replace(",ptm,", ",unused,"))
    with pytest.raises(ScientificAdapterError, match="lacks confidence"):
        check_both(path)

"""Real receipts, and the states they are actually allowed to establish.

The renderer may only ever say ``rendered``, because a contract says what should
be there. These tests use the receipts a live run on k8s-inference-h100 actually
produced, and require that a state is raised only by a document that backs it
and that every digest published is recomputed from bytes on disk.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from fs2_serve.scientific_batch.adapters.localization import (
    LocalizationContract,
    load_localization_contracts_from_path,
)
from fs2_serve.scientific_batch.adapters.primitives import ArtifactLocalizationError
from fs2_serve.scientific_batch.adapters.receipt_ingest import (
    BindingState,
    ingest_artifact,
    ingest_report,
    load_admission,
    load_localization_receipt,
    load_probe,
    runtime_artifact_entry,
)
from fs2_serve.scientific_batch.execution import FileScientificManifestRenderer

REPO = Path(__file__).resolve().parents[3]
LIVE = REPO / "models/cancer-immunotherapy/artifact-localization/evidence/live-20260903"
CONTRACTS = REPO / "catalog/runtime/contracts/scientific-artifact-localization.json"

BOLTZGEN = "boltzgen-inference-molecules"
PYROSETTA = "bindcraft-pyrosetta-installed-tree"


def contract(artifact_id: str) -> LocalizationContract:
    return load_localization_contracts_from_path(CONTRACTS)[artifact_id]


def test_the_live_boltzgen_receipts_qualify_it_and_nothing_is_assumed() -> None:
    """One model carried all the way from bytes on a node to a usable identity."""

    admission = load_admission(LIVE / "boltzgen-admission.json")
    probe = load_probe(LIVE / "boltzgen-moldir-probe.json")
    evidence = ingest_artifact(contract(BOLTZGEN), admission=admission, probe=probe)

    assert evidence.state is BindingState.QUALIFIED
    assert evidence.generation == "8ab1a59c72fc27a37dea61aab9408d7619f7a91fe32409f7a2b36fd59ebeecdc"
    assert evidence.marker_digest == "7f0e2c401abd73c1d4ff6deb6719e027db6ee9a75f7b7ed940b1e63ff54bbae4"

    # Every digest is recomputed here from the bytes of the evidence file, so a
    # reader can check it without trusting this code.
    assert evidence.receipt_digest == hashlib.sha256((LIVE / "boltzgen-admission.json").read_bytes()).hexdigest()


def test_a_promotion_receipt_establishes_promoted_and_a_probe_is_what_adds_qualified() -> None:
    receipt = load_localization_receipt(LIVE / "pyrosetta-promote-receipt.json")
    admission = load_admission(LIVE / f"bindcraft-admission-{PYROSETTA}.json")
    evidence = ingest_artifact(contract(PYROSETTA), receipt=receipt, admission=admission)

    # Promoted, because a terminal receipt and an admission both back it; not
    # qualified, because no model has read this tree yet and saying otherwise
    # would be the fabrication this whole path exists to prevent.
    assert evidence.state is BindingState.PROMOTED
    assert evidence.generation == "a93d68e198c81cbb87926e012dff6b50a73e99d9a41261e65f73d264c792aa8d"
    assert evidence.attested_by == "localization-receipt"
    # The live promotion really did share the bytes.
    observation = receipt.document["observation"]
    assert observation["bytes_copied"] == 0
    assert observation["bytes_linked"] == 3287122494


def test_an_artifact_with_no_evidence_stays_where_it_was() -> None:
    with pytest.raises(ArtifactLocalizationError, match="nothing has been established"):
        ingest_artifact(contract(BOLTZGEN))


def test_evidence_for_another_artifact_is_an_error_not_a_lower_state(tmp_path: Path) -> None:
    """Handing over the wrong receipt is a mistake, and silence would hide it."""

    with pytest.raises(ArtifactLocalizationError, match="admitted 'boltzgen-inference-molecules'"):
        ingest_artifact(contract(PYROSETTA), admission=load_admission(LIVE / "boltzgen-admission.json"))


def _tamper(tmp_path: Path, name: str, mutate: Any) -> Path:
    document = json.loads((LIVE / name).read_text(encoding="utf-8"))
    mutate(document)
    path = tmp_path / name
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_a_marker_digest_that_does_not_match_its_marker_is_refused(tmp_path: Path) -> None:
    """The admission carries both, so the two must agree or one was edited."""

    def swap(document: dict[str, Any]) -> None:
        document["manifest_digest"] = "f" * 64

    with pytest.raises(ArtifactLocalizationError, match="hashes to"):
        load_admission(_tamper(tmp_path, "boltzgen-admission.json", swap))


def test_a_marker_edited_after_the_fact_is_refused(tmp_path: Path) -> None:
    """Changing the content changes its digest, which no longer matches."""

    def inflate(document: dict[str, Any]) -> None:
        document["marker"]["entry_count"] = 1

    with pytest.raises(ArtifactLocalizationError, match="hashes to"):
        load_admission(_tamper(tmp_path, "boltzgen-admission.json", inflate))


def test_an_admission_of_a_different_generation_is_refused(tmp_path: Path) -> None:
    def restamp(document: dict[str, Any]) -> None:
        marker = document["marker"]
        marker["generation"] = "b" * 64
        marker["inventory_sha256"] = "b" * 64
        document["manifest_digest"] = hashlib.sha256(
            (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode()
        ).hexdigest()

    admission = load_admission(_tamper(tmp_path, "boltzgen-admission.json", restamp))
    with pytest.raises(ArtifactLocalizationError, match="different generation"):
        ingest_artifact(contract(BOLTZGEN), admission=admission)


def test_a_rejected_receipt_never_establishes_anything() -> None:
    """The EPERM refusal from the live run is evidence of a failure, not a state."""

    with pytest.raises(ArtifactLocalizationError, match="did not verify"):
        load_localization_receipt(LIVE / "pyrosetta-promote-eperm-rejected.json")


def test_a_probe_of_some_other_tree_cannot_qualify_this_one() -> None:
    """A passing probe is only evidence for the mount it actually read."""

    with pytest.raises(ArtifactLocalizationError, match="does not mention any mount path"):
        ingest_artifact(
            contract(BOLTZGEN),
            admission=load_admission(LIVE / "boltzgen-admission.json"),
            probe=load_probe(LIVE / "bindcraft-mount-probe.json"),
        )


def test_a_failed_probe_is_refused(tmp_path: Path) -> None:
    def fail(document: dict[str, Any]) -> None:
        document["state"] = "failed"
        document["reason"] = "the loader could not read the tree"

    with pytest.raises(ArtifactLocalizationError, match="did not pass"):
        load_probe(_tamper(tmp_path, "boltzgen-moldir-probe.json", fail))


def _live_evidence() -> dict[str, Any]:
    admission = load_admission(LIVE / "boltzgen-admission.json")
    probe = load_probe(LIVE / "boltzgen-moldir-probe.json")
    return ingest_artifact(contract(BOLTZGEN), admission=admission, probe=probe)


def test_the_execution_map_parser_accepts_what_ingestion_emits(tmp_path: Path) -> None:
    """The join this whole chain turns on, checked against the real parser.

    An entry that is merely plausible is worth nothing: the execution map has
    its own field contract and rejects anything else, so the only way to know
    ingestion produces a usable entry is to hand it to the parser that will
    consume it. This reuses the sibling suite's known-good map and substitutes
    only the runtime artifact, so a failure can only be the entry.
    """

    handoff = pytest.importorskip("test_scientific_batch_execution_handoff")
    handoff.runtime_execution_map(tmp_path)
    source = json.loads((tmp_path / "complete.json").read_text(encoding="utf-8"))

    evidence = _live_evidence()
    value = copy.deepcopy(source)
    value["models"][0]["runtime_artifacts"] = [runtime_artifact_entry(evidence)]
    path = tmp_path / "ingested.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    profile = handoff.runtime_profile()
    catalog = handoff.ScientificProfileCatalog(
        profiles={profile.model_id: profile},
        validators=handoff.ScientificProfileCatalog.load(handoff.CATALOG_ROOT)._validators,
    )
    parsed = FileScientificManifestRenderer(
        path=path,
        profiles=catalog,
        tools_image="registry.test/control@sha256:" + "9" * 64,
        internal_api_url="http://control.fs2.svc:8080",
    )
    model_id = value["models"][0]["model_id"]
    localized = parsed.runtime_artifacts[(model_id, BOLTZGEN)]

    # The parser normalizes digests, so these prove the values survived it.
    assert localized.content_digest == f"sha256:{evidence.generation}"
    assert localized.localization_receipt_digest == f"sha256:{evidence.receipt_digest}"
    tree = localized.aggregate_tree
    assert tree is not None
    assert tree.file_count == 45227
    assert tree.expanded_bytes == 1820698819
    assert tree.canonical_path.endswith(evidence.generation)
    assert tree.marker_relative_path == ".fs2-runtime-tree.json"


def test_an_entry_the_parser_would_reject_is_caught_here_first(tmp_path: Path) -> None:
    """A mount path the artifact does not provide is refused before rendering."""

    with pytest.raises(ArtifactLocalizationError, match="is not a mount path"):
        runtime_artifact_entry(_live_evidence(), mount_path="/somewhere/else")


def test_the_report_states_the_weakest_thing_that_is_true() -> None:
    """A set of artifacts is only as established as its least established member."""

    boltzgen = _live_evidence()
    pyrosetta = ingest_artifact(
        contract(PYROSETTA),
        receipt=load_localization_receipt(LIVE / "pyrosetta-promote-receipt.json"),
        admission=load_admission(LIVE / f"bindcraft-admission-{PYROSETTA}.json"),
    )
    report = ingest_report([boltzgen, pyrosetta])
    assert report["state"] == "promoted", "one qualified and one promoted is not a qualified set"
    assert [item["artifact_id"] for item in report["artifacts"]] == sorted([BOLTZGEN, PYROSETTA])
    assert ingest_report([boltzgen])["state"] == "qualified"
    assert ingest_report([])["state"] == "rendered"


def test_every_live_receipt_still_backs_the_state_it_was_recorded_with() -> None:
    """The checked-in summary and the receipts beside it must not drift apart."""

    summary = json.loads((LIVE / "live-qualification.json").read_text(encoding="utf-8"))
    cases = {
        BOLTZGEN: (None, "boltzgen-admission.json", "boltzgen-moldir-probe.json"),
        "alphafold2-params-bindcraft": (
            None,
            "bindcraft-admission-alphafold2-params-bindcraft.json",
            "bindcraft-mount-probe.json",
        ),
        "colabdesign-mpnn-weights-vanilla": (
            None,
            "bindcraft-admission-colabdesign-mpnn-weights-vanilla.json",
            "bindcraft-mount-probe.json",
        ),
        "colabdesign-mpnn-weights-soluble": (
            None,
            "bindcraft-admission-colabdesign-mpnn-weights-soluble.json",
            "bindcraft-mount-probe.json",
        ),
        PYROSETTA: ("pyrosetta-promote-receipt.json", f"bindcraft-admission-{PYROSETTA}.json", None),
        "alphafold2-params": (None, "complexa-admission.json", None),
    }
    for artifact_id, (receipt_name, admission_name, probe_name) in cases.items():
        evidence = ingest_artifact(
            contract(artifact_id),
            receipt=load_localization_receipt(LIVE / receipt_name) if receipt_name else None,
            admission=load_admission(LIVE / admission_name) if admission_name else None,
            probe=load_probe(LIVE / probe_name) if probe_name else None,
        )
        recorded = summary["artifacts"][artifact_id]
        assert evidence.marker_digest == recorded["manifest_digest"], artifact_id
        assert evidence.generation == recorded["generation"], artifact_id
        # The summary's own words must not outrun what the receipts establish.
        if recorded["state"] == "qualified":
            assert evidence.state is BindingState.QUALIFIED, artifact_id
        else:
            assert evidence.state is BindingState.PROMOTED, artifact_id

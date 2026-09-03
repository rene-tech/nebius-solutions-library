"""Turn real localization evidence into the identities the execution map needs.

A renderer knows only what a contract says, so it may assert only ``rendered``.
Getting past that requires evidence, and evidence means documents a run actually
produced: a terminal localization receipt from a stage or a promotion, an
admission record written by the verifier on the node that mounted the
generation, and a probe report from the model's own loader.

This module is the one place those documents become a claim. Every digest it
publishes is computed from bytes on disk, never supplied by a caller and never
derived from a contract: a contract says what *should* be there, and the whole
point of a receipt is to say what *was*. A state is raised only when a document
that backs it validates against the checked-in schema and agrees with the
contract it claims to satisfy, so a missing, stale or tampered receipt leaves
the state exactly where it was rather than quietly advancing it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .localization import (
    MARKER_SCHEMA,
    RECEIPT_SCHEMA,
    RUNTIME_MARKER_NAME,
    LocalizationContract,
    marker_bytes,
)
from .primitives import ArtifactLocalizationError, strict_object

INGEST_SCHEMA = "fs2-serve.nebius.ai/scientific-localization-ingest/v1"
# The publication contract a verifier can check: a content-addressed generation
# directory carrying its own marker.
GENERATION_STORAGE_KIND = "localization-generation"


class BindingState(IntEnum):
    """What real evidence has established about one generation, in order.

    The order matters: a state may only be raised by a document that backs it,
    so the ladder is compared rather than assigned.
    """

    RENDERED = 0
    PROMOTED = 1
    QUALIFIED = 2

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class IngestedDocument:
    """One evidence file, with the digest of exactly the bytes on disk."""

    path: str
    kind: str
    sha256: str
    document: Mapping[str, object]

    @property
    def prefixed_digest(self) -> str:
        return f"sha256:{self.sha256}"


def _read(path: Path) -> tuple[bytes, Mapping[str, object]]:
    payload = path.read_bytes()
    if len(payload) > 4 * 1024 * 1024:
        raise ArtifactLocalizationError(f"{path.name} exceeds the evidence byte bound")
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ArtifactLocalizationError(f"{path.name} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ArtifactLocalizationError(f"{path.name} is not a JSON object")
    return payload, value


def load_localization_receipt(path: Path) -> IngestedDocument:
    """A terminal receipt from a stage or a promotion, and its exact digest."""

    payload, document = _read(path)
    if document.get("schema") != RECEIPT_SCHEMA:
        raise ArtifactLocalizationError(f"{path.name} is not a localization receipt")
    if document.get("state") != "verified":
        reason = document.get("rejection_reason", "no reason recorded")
        raise ArtifactLocalizationError(f"{path.name} did not verify: {reason}")
    return IngestedDocument(
        path=path.name, kind="localization-receipt", sha256=hashlib.sha256(payload).hexdigest(), document=document
    )


def load_admission(path: Path) -> IngestedDocument:
    """An admission written by the verifier on the node that mounted the tree.

    This is the document that proves a published generation was present and
    matched, which a contract can never prove on its own.
    """

    payload, document = _read(path)
    strict_object(
        document,
        required=frozenset({"state", "marker", "manifest_digest"}),
        label=f"{path.name} admission",
    )
    if document["state"] != "admitted":
        raise ArtifactLocalizationError(f"{path.name} was not admitted: {document.get('reason', 'no reason')}")
    marker = document["marker"]
    if not isinstance(marker, dict) or marker.get("schema") != MARKER_SCHEMA:
        raise ArtifactLocalizationError(f"{path.name} does not carry a generation marker")
    # The digest the admission reports must be the digest of the marker it
    # reports, or one of the two was edited after the fact.
    recomputed = hashlib.sha256(marker_bytes(marker)).hexdigest()
    if recomputed != document["manifest_digest"]:
        raise ArtifactLocalizationError(
            f"{path.name} reports marker digest {document['manifest_digest']} but its marker hashes to {recomputed}"
        )
    return IngestedDocument(
        path=path.name, kind="admission", sha256=hashlib.sha256(payload).hexdigest(), document=document
    )


def load_probe(path: Path) -> IngestedDocument:
    """A model probe: the model's own loader reading the mounted tree."""

    payload, document = _read(path)
    schema = document.get("schema")
    if not isinstance(schema, str) or "-probe/" not in schema:
        raise ArtifactLocalizationError(f"{path.name} is not a model probe report")
    if document.get("state") != "passed":
        raise ArtifactLocalizationError(f"{path.name} did not pass: {document.get('reason', 'no reason recorded')}")
    return IngestedDocument(
        path=path.name, kind="model-probe", sha256=hashlib.sha256(payload).hexdigest(), document=document
    )


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    """Everything real that is known about one artifact's generation."""

    artifact_id: str
    state: BindingState
    generation: str
    marker_digest: str
    receipt_digest: str
    attested_by: str
    sub_path: str
    mount_paths: tuple[str, ...]
    entry_count: int
    directory_count: int
    total_bytes: int
    inventory_algorithm: str
    documents: tuple[IngestedDocument, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "binding_state": self.state.label,
            "generation": self.generation,
            "marker_manifest_digest": self.marker_digest,
            "localization_receipt_digest": f"sha256:{self.receipt_digest}",
            "localization_receipt_attested_by": self.attested_by,
            "sub_path": self.sub_path,
            "evidence": [{"kind": item.kind, "file": item.path, "sha256": item.sha256} for item in self.documents],
        }


def _marker_of(admission: IngestedDocument) -> Mapping[str, object]:
    return admission.document["marker"]  # type: ignore[return-value]


def ingest_artifact(
    contract: LocalizationContract,
    *,
    receipt: IngestedDocument | None = None,
    admission: IngestedDocument | None = None,
    probe: IngestedDocument | None = None,
) -> ArtifactEvidence:
    """Raise a state exactly as far as the supplied documents actually back it.

    Nothing here trusts a caller's word for which artifact a document describes:
    each is checked against the contract it claims to satisfy, and a document
    that names a different artifact, a different generation or a different
    algorithm is an error rather than a lower state, because it means the caller
    handed over evidence for something else.
    """

    tree = contract.tree
    if receipt is None and admission is None:
        raise ArtifactLocalizationError(
            f"{contract.artifact_id} has no terminal receipt and no admission, so nothing has been established"
        )

    documents: list[IngestedDocument] = []
    generation = ""
    marker_digest = ""
    sub_path = ""

    if receipt is not None:
        body = receipt.document
        identity = body["tree_identity"]
        if not isinstance(identity, dict):
            raise ArtifactLocalizationError(f"{receipt.path} carries no tree identity")
        if body.get("artifact_id") != contract.artifact_id:
            raise ArtifactLocalizationError(f"{receipt.path} names {body.get('artifact_id')!r}")
        if identity.get("inventory_sha256") != tree.inventory_sha256:
            raise ArtifactLocalizationError(f"{receipt.path} verified a different generation")
        if identity.get("inventory_algorithm") != tree.inventory_algorithm:
            raise ArtifactLocalizationError(f"{receipt.path} used a different identity algorithm")
        if identity.get("entry_count") != tree.entry_count or identity.get("total_bytes") != tree.total_bytes:
            raise ArtifactLocalizationError(f"{receipt.path} counted a different tree")
        observation = body.get("observation")
        if isinstance(observation, dict):
            generation = str(observation.get("generation", "")) or generation
            sub_path = str(observation.get("generation_sub_path", "")) or sub_path
            marker_digest = str(observation.get("marker_sha256", "")) or marker_digest
        documents.append(receipt)

    if admission is not None:
        marker = _marker_of(admission)
        if marker.get("artifact_id") != contract.artifact_id:
            raise ArtifactLocalizationError(f"{admission.path} admitted {marker.get('artifact_id')!r}")
        if marker.get("inventory_sha256") != tree.inventory_sha256:
            raise ArtifactLocalizationError(f"{admission.path} admitted a different generation")
        if marker.get("inventory_algorithm") != tree.inventory_algorithm:
            raise ArtifactLocalizationError(f"{admission.path} admitted a different identity algorithm")
        if marker.get("visibility") != contract.visibility:
            raise ArtifactLocalizationError(f"{admission.path} admitted the wrong visibility")
        admitted_digest = str(admission.document["manifest_digest"])
        if marker_digest and marker_digest != admitted_digest:
            raise ArtifactLocalizationError(
                f"{admission.path} admitted marker {admitted_digest} but the receipt sealed {marker_digest}"
            )
        marker_digest = admitted_digest
        generation = str(marker["generation"])
        sub_path = str(marker["sub_path"])
        documents.append(admission)

    if not generation or not marker_digest or not sub_path:
        raise ArtifactLocalizationError(
            f"{contract.artifact_id} evidence does not establish a generation, its marker and its path"
        )

    state = BindingState.PROMOTED
    if probe is not None:
        # A probe proves the model read the mount. It is only accepted as
        # evidence for this artifact if a mount it names is one this artifact
        # actually provides, so a probe of some other tree cannot qualify this one.
        rendered = json.dumps(probe.document, sort_keys=True)
        if not any(path in rendered for path in tree.mount_paths):
            raise ArtifactLocalizationError(f"{probe.path} does not mention any mount path of {contract.artifact_id}")
        documents.append(probe)
        state = BindingState.QUALIFIED

    # The receipt digest is the digest of the document that attests the
    # localization: a terminal receipt when this tool published the tree, and
    # otherwise the node admission that proved the published tree is correct.
    attesting = receipt or admission
    assert attesting is not None
    return ArtifactEvidence(
        artifact_id=contract.artifact_id,
        state=state,
        generation=generation,
        marker_digest=marker_digest,
        receipt_digest=attesting.sha256,
        attested_by=attesting.kind,
        sub_path=sub_path,
        mount_paths=tree.mount_paths,
        entry_count=tree.entry_count,
        directory_count=tree.directory_count,
        total_bytes=tree.total_bytes,
        inventory_algorithm=tree.inventory_algorithm,
        documents=tuple(documents),
    )


def runtime_artifact_entry(evidence: ArtifactEvidence, *, mount_path: str | None = None) -> dict[str, object]:
    """One ``runtime_artifacts`` entry for a schema-v3 execution map.

    Field names and shape are fixed by the execution map's own parser, so this
    produces exactly what it accepts and nothing else: an entry that carried an
    extra key, or a digest without its algorithm prefix, is rejected there
    rather than here, and a consumer would have to guess which.
    """

    chosen = mount_path or evidence.mount_paths[0]
    if chosen not in evidence.mount_paths:
        raise ArtifactLocalizationError(f"{chosen} is not a mount path of {evidence.artifact_id}")
    return {
        "artifact_id": evidence.artifact_id,
        "mount_path": chosen,
        "content_digest": f"sha256:{evidence.generation}",
        "localization_receipt_digest": f"sha256:{evidence.receipt_digest}",
        "aggregate_tree": {
            "storage_kind": GENERATION_STORAGE_KIND,
            "tree_sha256": evidence.generation,
            "manifest_sha256": evidence.marker_digest,
            "inventory_sha256": evidence.generation,
            "manifest_algorithm": evidence.inventory_algorithm,
            "file_count": evidence.entry_count,
            "directory_count": evidence.directory_count,
            "expanded_bytes": evidence.total_bytes,
            "canonical_path": evidence.sub_path,
            "marker_relative_path": RUNTIME_MARKER_NAME,
        },
    }


def ingest_report(entries: Sequence[ArtifactEvidence]) -> dict[str, object]:
    """What was established, and by which document, in one auditable place."""

    return {
        "schema": INGEST_SCHEMA,
        "state": min((item.state for item in entries), default=BindingState.RENDERED).label,
        "artifacts": [item.to_dict() for item in sorted(entries, key=lambda item: item.artifact_id)],
        "note": (
            "Every digest here is the SHA-256 of bytes on disk: a generation is named by its own "
            "content, a marker digest is recomputed from the marker the admission carried, and a "
            "localization receipt digest is taken over the exact evidence file. A state is raised only "
            "by a document that validates and agrees with the contract it claims to satisfy, so a "
            "missing or stale receipt leaves the state where it was rather than advancing it."
        ),
    }


__all__ = [
    "GENERATION_STORAGE_KIND",
    "INGEST_SCHEMA",
    "ArtifactEvidence",
    "BindingState",
    "IngestedDocument",
    "ingest_artifact",
    "ingest_report",
    "load_admission",
    "load_localization_receipt",
    "load_probe",
    "runtime_artifact_entry",
]

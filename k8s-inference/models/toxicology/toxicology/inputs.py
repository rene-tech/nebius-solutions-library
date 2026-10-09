"""Parse records without dropping bad molecules or silently changing chemistry."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .contracts import MAX_MOLECULES, ScreeningRequest


@dataclass(frozen=True)
class InputRow:
    index: int
    id: str
    smiles: str
    error: str | None = None


def parse_rows(request: ScreeningRequest) -> list[InputRow]:
    if request.smiles is not None:
        rows = [InputRow(0, "molecule-1", request.smiles)]
    elif request.molecules is not None:
        rows = [InputRow(i, item.id, item.smiles) for i, item in enumerate(request.molecules)]
    elif request.csv is not None:
        reader = csv.DictReader(io.StringIO(request.csv.removeprefix("\ufeff")))
        if request.smiles_column not in (reader.fieldnames or []):
            raise ValueError(f"CSV is missing SMILES column {request.smiles_column!r}")
        if request.id_column is not None and request.id_column not in (reader.fieldnames or []):
            raise ValueError(f"CSV is missing ID column {request.id_column!r}")
        rows = []
        for i, record in enumerate(reader):
            if i >= MAX_MOLECULES:
                raise ValueError(f"at most {MAX_MOLECULES} molecules per operation; split larger libraries into batches")
            identifier = record.get(request.id_column, "") if request.id_column else f"molecule-{i + 1}"
            smiles = record.get(request.smiles_column) or ""
            rows.append(InputRow(i, identifier or f"molecule-{i + 1}", smiles, "missing_smiles" if not smiles.strip() else None))
    else:
        from rdkit import Chem

        supplier = Chem.ForwardSDMolSupplier(io.BytesIO(request.sdf.encode("utf-8")), sanitize=True, removeHs=False)
        rows = []
        for i, mol in enumerate(supplier):
            if i >= MAX_MOLECULES:
                raise ValueError(f"at most {MAX_MOLECULES} molecules per operation; split larger libraries into batches")
            if mol is None:
                rows.append(InputRow(i, f"molecule-{i + 1}", "", "invalid_sdf_record"))
                continue
            field = request.id_column or "_Name"
            identifier = mol.GetProp(field).strip() if mol.HasProp(field) else ""
            rows.append(InputRow(i, identifier or f"molecule-{i + 1}", Chem.MolToSmiles(mol, isomericSmiles=True)))
    if not rows:
        raise ValueError("input contains no molecule records")
    identifiers = [row.id for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("molecule IDs must be unique; repeated structures with distinct IDs are supported")
    if any(len(row.id) > 256 or len(row.smiles) > 16384 for row in rows):
        raise ValueError("a molecule ID or SMILES exceeds the documented field length")
    return rows


def prepare_rows(rows: list[InputRow]) -> tuple[list[InputRow], dict[int, dict]]:
    from rdkit import Chem, rdBase

    valid, results = [], {}
    # Native parser diagnostics can contain the input structure. Return structured
    # per-row errors; do not leak structures into shared worker logs.
    with rdBase.BlockLogs():
        for row in rows:
            mol = None if row.error else Chem.MolFromSmiles(row.smiles)
            if mol is None or mol.GetNumAtoms() == 0:
                results[row.index] = {
                    "index": row.index, "id": row.id, "status": "error",
                    "error": {"code": row.error or "invalid_smiles", "message": "Molecule could not be parsed; no prediction was made."},
                }
                continue
            valid.append(row)
            results[row.index] = {
                "index": row.index, "id": row.id, "status": "pending",
                "input_smiles": row.smiles,
                "canonical_smiles": Chem.MolToSmiles(mol, isomericSmiles=True),
                "warnings": (["disconnected_fragments_present"] if len(Chem.GetMolFrags(mol)) > 1 else []),
            }
    return valid, results

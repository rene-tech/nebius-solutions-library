#!/usr/bin/env python3
"""Run public fixtures through the unmodified, isolated Protenix stage wrapper.

Execute inside the exact deployed image. The template is the public artifact
localization receipt retained by the previous platform acceptance test. Test
identities are local only; this script never contacts the customer control plane.
No scientific parameters, model code or output validation are bypassed.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


UBIQUITIN = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"
LYSOZYME = (
    "KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGR"
    "TPGSRNLCNIPCSALLSSDITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDVQAWIRGCRL"
)
CASES = (
    ("ubiquitin-76", UBIQUITIN, 1),
    ("lysozyme-129", LYSOZYME, 1),
    ("lysozyme-homodimer-258", LYSOZYME, 2),
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker-template", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    template = json.loads(args.marker_template.read_text())
    environment = dict(os.environ)
    environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    for variable in (
        "FS2_RUNTIME_LOCALIZATION_MARKER", "FS2_OPERATION_ID", "FS2_ATTEMPT_ID",
        "FS2_TENANT_ID", "FS2_VARIANT_ID", "FS2_STAGE_ID",
    ):
        if environment.get(variable):
            raise RuntimeError(f"isolated test must not inherit a customer identity: {variable}")
    results = args.directory / "attempts.jsonl"

    def record(row: dict) -> None:
        with results.open("a") as output:
            output.write(json.dumps(row, sort_keys=True) + "\n")
        print(json.dumps(row, sort_keys=True), flush=True)

    def call(command: list[str], path: Path, *, case: str, phase: str, repetition: int) -> dict:
        started = time.monotonic()
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with path.open("w") as log:
                done = subprocess.run(command, env=environment, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=1800, check=False)
            code, error = done.returncode, None
        except subprocess.TimeoutExpired:
            code, error = None, "1800s timeout"
        row = {
            "model_id": "protenix-v2", "variant": "current-conventional",
            "case": case, "phase": phase, "repetition": repetition,
            "created_at": created_at, "wall_seconds": time.monotonic() - started,
            "return_code": code, "error": error, "command": command,
            "log": str(path.relative_to(args.directory)), "status": "passed" if code == 0 else "failed",
        }
        return row

    prepared = []
    for case, sequence, copies in CASES:
        directory = args.directory / case
        directory.mkdir(exist_ok=True)
        raw = directory / "input.json"
        raw.write_text(json.dumps([{"name": case, "sequences": [{"proteinChain": {
            "sequence": sequence, "count": copies,
        }}]}]) + "\n")
        marker = copy.deepcopy(template)
        marker.update(operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bioir/" + case)),
                      attempt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bioir/prep/" + case)),
                      tenant_id="bioir-isolated-public-fixtures", stage_id="prepare-data")
        prep_marker = directory / "localization-prep.json"
        prep_marker.write_text(json.dumps(marker))
        marker.update(attempt_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "bioir/pred/" + case)),
                      stage_id="sample-structure")
        pred_marker = directory / "localization-pred.json"
        pred_marker.write_text(json.dumps(marker))
        processed, provenance = directory / "processed.json", directory / "provenance.json"
        command = [
            "/usr/local/bin/fs2-run-protenix", "prep", "--input", str(raw),
            "--output-dir", str(directory / "prepared"), "--processed-json", str(processed),
            "--provenance-marker", str(provenance), "--handoff-tar", str(directory / "handoff.tar"),
            "--output-artifact-id", "bioir.prepared." + case,
            "--raw-input-artifact-id", "bioir.raw." + case, "--raw-input-sha256", sha(raw),
            "--msa-mode", "none", "--reference-root", "/models/protenix-v2",
            "--reference-manifest", "/models/protenix-v2/manifest.json",
            "--runtime-localization-marker", str(prep_marker),
        ]
        row = call(command, directory / "prep.log", case=case, phase="cpu-prepare", repetition=0)
        record(row)
        if row["status"] != "passed":
            continue
        pred = [
            "/usr/local/bin/fs2-run-protenix", "pred", "--input", str(processed),
            "--input-marker", str(provenance), "--input-artifact-id", "bioir.prepared." + case,
            "--expected-raw-input-artifact-id", "bioir.raw." + case,
            "--expected-raw-input-sha256", sha(raw),
            "--checkpoint", "/models/protenix-v2/checkpoint/protenix-v2.pt",
            "--common-dir", "/models/protenix-v2/common", "--msa-mode", "none",
            "--seeds", "101", "--sample-count", "1", "--disable-templates", "--disable-rna-msa",
            "--runtime-localization-marker", str(pred_marker),
        ]
        (directory / "prediction-command.json").write_text(json.dumps(pred))
        prepared.append((case, directory, pred))
    if args.prepare_only:
        return
    # Interleaving shapes tests the actual batch use case while retaining one
    # cold model process per request, exactly as conventional production stages.
    for repetition in range(1, args.repetitions + 1):
        for case, directory, pred in prepared:
            output = directory / f"r{repetition:02d}"
            command = [*pred, "--output-dir", str(output)]
            row = call(command, directory / f"r{repetition:02d}.log",
                       case=case, phase="sample-structure", repetition=repetition)
            row["input_sha256"] = sha(directory / "input.json")
            row["artifacts"] = [
                {"path": str(file.relative_to(args.directory)), "bytes": file.stat().st_size,
                 "sha256": sha(file)}
                for file in sorted(output.rglob("*")) if file.is_file()
            ] if output.exists() else []
            if row["return_code"] == 0:
                # The unchanged wrapper already enforces sample cardinality,
                # provenance and confidence ranges; additionally parse atom sites.
                from Bio.PDB.MMCIF2Dict import MMCIF2Dict
                import math

                structures = list(output.rglob("*.cif"))
                try:
                    assert len(structures) == 1, f"expected one structure, got {len(structures)}"
                    data = MMCIF2Dict(str(structures[0]))
                    coordinates = [float(value) for field in ("x", "y", "z")
                                   for value in data["_atom_site.Cartn_" + field]]
                    assert coordinates and all(math.isfinite(value) for value in coordinates)
                    row["quality"] = {"finite_coordinates": True,
                                      "atom_count": len(data["_atom_site.Cartn_x"]),
                                      "scientific_equivalence": "not-assessed-until-paired"}
                except Exception as error:
                    row.update(status="failed", validation_error=str(error))
            record(row)


if __name__ == "__main__":
    main()

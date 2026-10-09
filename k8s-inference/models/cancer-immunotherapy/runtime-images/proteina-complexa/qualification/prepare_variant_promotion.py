"""Prepare only the reviewed Proteina successor against an exact live capture."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

from fs2_serve.scientific_batch.adapters.common import runtime_recipe_sha256

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "acceptance"))
from scientific_runtime_successor import digest, prepare  # noqa: E402

MODEL = "proteina-complexa"
OLD = "sha256:f4e06b6025a74c924749420f2fce01fb9511aba606a2266c85a9d9e92e3679ca"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/proteina-complexa@sha256:e5e075237a680dc01ace45b97ecfcfb9f95f5897aa51f36164591a74778a9cd1"
ORIGINAL_CASES = {
    "proteina-ligand-target-41_7bkc_ligand-s7", "proteina-ligand-target-41_7bkc_ligand-s42",
    "proteina-ame-m0584_1ldm-s7", "proteina-ame-m0584_1ldm-s42", "proteina-complexa-pdl1-s1-n1",
}


def candidate(profiles, execution, evidence, evidence_sha):
    if evidence["image"] != IMAGE or evidence["scope"] != "isolated_runtime_and_structural_measurements_not_public_or_biological_qualification":
        raise ValueError("Require exact candidate and explicitly bounded qualification")
    cases = {row["case_id"]: row for row in evidence["cases"]}
    if not ORIGINAL_CASES.issubset(cases):
        raise ValueError("Missing original variant/protein regression")
    required = ORIGINAL_CASES | {name + "-matched-n400" for name in (
        "proteina-ligand-target-41_7bkc_ligand-s7", "proteina-ligand-target-41_7bkc_ligand-s42", "proteina-ame-m0584_1ldm-s7")}
    if set(cases) != required or not evidence["cleanup"]["all_task_resources_absent"]:
        raise ValueError("Matched comparisons or task cleanup incomplete")
    for name, row in cases.items():
        if (len(row["stages"]) != 4 or any(s["returncode"] != 0 for s in row["stages"])
                or row["parameters"]["diffusion_steps"] != (400 if name.endswith("-matched-n400") else 100)):
            raise ValueError("Original or explicitly revised execution is incomplete")
    return prepare(profiles, execution, model_id=MODEL, previous_digest=OLD, candidate_image=IMAGE,
        recipe_sha256=runtime_recipe_sha256(ROOT, MODEL), semantic_receipt_sha256=evidence_sha,
        measured_at=evidence["recorded_at"], limitations=[
            "This image repairs actual Hydra multi-ligand parsing, CuEq/Torch compatibility, CPU JIT prerequisites and swallowed RF3 failures; model/checkpoint/scientific resource identities are unchanged.",
            "The hosted API requires an explicit diffusion_steps value. Pinned upstream baseline is400; shorter requested sampling remains supported and is never silently increased. Original100-step cases completed but all8raw designs failed basic C-alpha geometry. Refolded coordinates and raw generation are distinct artifacts, not interchangeable evidence.",
            "Three explicitly revised400-step cases cover two ligand seeds and one AME seed; inspect the linked independent geometry measurements. These sparse computational checks do not establish affinity, catalytic activity, experimental or clinical utility.",
            "The successor is active/unqualified pending ordinary public completion and scheduler receipts. No predecessor public or snapshot qualification is inherited.",
        ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    values = json.loads((args.baseline / "values.json").read_bytes())
    originals = json.loads((args.baseline / "profiles.json").read_bytes())
    old_execution = values["scientificBatch"]["executionMap"]
    if isinstance(old_execution, str):
        old_execution = json.loads(old_execution)
    raw = args.evidence.read_bytes()
    evidence = json.loads(raw)
    profiles, execution, report = candidate(originals, old_execution, evidence, hashlib.sha256(raw).hexdigest())
    if report["changed_stage_ids"] != ["generate", "filter", "evaluate", "analyze"]:
        raise ValueError("Unexpected Proteina stage topology")
    source = ROOT / "acceptance/openfold3-inline-20260918/promotion.py"
    spec = importlib.util.spec_from_file_location("proteina_existing_startup", source)
    validators = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validators)
    report["startup_validation"] = validators.validate_startup(profiles, execution, values,
        json.loads((args.baseline / "configmaps.json").read_bytes()), args.baseline / "scheduling.json",
        json.loads((args.baseline / "gateway-deployment.json").read_bytes()))
    complete_values = validators.merge_values(values, execution)
    rendered, report["helm_validation"] = validators.validate_helm(complete_values, execution)
    report.update(applied=False, snapshot_bundles_unchanged=execution.get("snapshot_bundles") == old_execution.get("snapshot_bundles"),
        baseline_sha256={name: hashlib.sha256((args.baseline / name).read_bytes()).hexdigest()
            for name in ("values.json", "profiles.json", "configmaps.json", "gateway-deployment.json", "scheduling.json")})
    profile = next(p for p in profiles["profiles"] if p["model_id"] == MODEL)
    documents = {"scientific-workload-profiles.json": profiles, "scientific-execution-map.json": execution,
        "source-execution-map.json": {k: v for k, v in execution.items() if k != "snapshot_bundles"},
        "workload-profile.json": {"schema": "fs2-serve.nebius.ai/scientific-workload-profile-projection/v1",
            "merge_target": "catalog/runtime/contracts/scientific-workload-profiles.json", "profile": profile},
        "validation.json": report, "values.json": complete_values, "rendered-execution-map.json": rendered,
        "rollback-values.json": values, "rollback-profiles.json": originals}
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in documents.items():
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"applied": False, "startup": "passed", "models": len(execution["models"]),
        "snapshot_bundles_preserved": report["snapshot_bundles_unchanged"], "execution_sha256": digest(execution)}))


if __name__ == "__main__":
    main()

"""Opt-in structured CXR research request; no inference, deployment or answer repair."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

MODEL = "nv-reason-cxr-3b"
CONTRACT = "nih-cxr-findings-json-schema-v1"
FINDINGS = (
    "Atelectasis", "Cardiomegaly", "Effusion", "Infiltration", "Mass", "Nodule",
    "Pneumonia", "Pneumothorax", "Consolidation", "Edema", "Emphysema", "Fibrosis",
    "Pleural_Thickening", "Hernia", "No Finding",
)


def response_format() -> dict:
    """Encode the existing prompt contract; reference labels never enter this schema.

    Avoid uniqueItems: the pinned vLLM 0.28 XGrammar backend rejects that keyword.
    Schema compliance constrains spelling/shape, not visible-image interpretation.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": CONTRACT,
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["findings", "limitations"],
                "properties": {
                    "findings": {
                        "anyOf": [
                            {"type": "array", "minItems": 1, "maxItems": 14,
                             "items": {"type": "string", "enum": list(FINDINGS[:-1])}},
                            {"type": "array", "minItems": 1, "maxItems": 1,
                             "items": {"type": "string", "const": "No Finding"}},
                        ],
                    },
                    "limitations": {"type": "string", "minLength": 1, "maxLength": 512},
                },
            },
        },
    }


def revised_case(original: dict) -> dict:
    """Change only response_format in inference arguments, preserving the evaluator."""
    if original["model_id"] != MODEL or original["expected"]["evaluator"] != "cxr_findings":
        raise ValueError("expected_cxr_findings_case")
    if tuple(original["expected"]["allowed_findings"]) != FINDINGS:
        raise ValueError("finding_vocabulary_differs_from_frozen_study")
    if original["arguments"].get("response_format") != {"type": "json_object"}:
        raise ValueError("expected_original_json_object_contract")
    result = copy.deepcopy(original)
    result["case_id"] += "-json-schema-v1"
    result["arguments"]["response_format"] = response_format()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    cases = [revised_case(c) for c in manifest["cases"] if c["model_id"] == MODEL]
    if len(cases) != 20:
        raise ValueError("expected_twenty_retained_cxr_cases")
    # The existing public runner resolves uploads relative to its manifest.
    # Preserve exact files when the generated manifest lives elsewhere.
    for case in cases:
        for field in case.get("preparation", {}).get("artifact_fields", []):
            field["local_path"] = str((args.manifest.parent / field["local_path"]).resolve())
    manifest.update(
        study_id=CONTRACT,
        cases=cases,
        contract_revision={
            "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "changed_inference_fields": ["response_format"],
            "source_requests_unchanged": True,
            "clinical_qualification": False,
            "status": "prepared_not_live_qualified",
        },
    )
    os.umask(0o077)
    with args.output.open("x") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"prepared_cases": len(cases), "inference_performed": False,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()

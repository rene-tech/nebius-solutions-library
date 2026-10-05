"""Rebuild the additive model registration from retained, non-secret sources.

This is an offline renderer, not another controller or a Terraform apply. It
merges only the eight explicitly adopted Apps and refuses different existing
registrations. The caller supplies its current base envelope/bundles and retains
all unrelated models, pools, and infrastructure settings.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from fs2_serve.model_deployment import (
    InfrastructureEnvelope,
    LegacyTemplateBundle,
    canonical_digest,
    canonical_json,
)

SOURCE = Path(__file__).with_name("managed-runtime-adoptions.json")


def merge_registration(envelope: dict, bundles: list, source: dict) -> tuple[dict, list]:
    """Preserve siblings and refuse accidental rollback of a newer registration."""
    assert source["schema"] == "fs2-serve.nebius.ai/retained-managed-adoptions/v1"
    expected = set(source["modelIds"])
    assert set(source["qualifications"]) == expected
    assert {row["modelRef"] for row in source["bundles"]} == expected
    assert len(source["bundles"]) == len(expected)
    envelope, bundles = copy.deepcopy((envelope, bundles))
    for row in source["bundles"]:
        typed = LegacyTemplateBundle.model_validate(row)
        assert canonical_digest(typed.resources) == typed.template_digest
        matches = [b for b in bundles if b["modelRef"] == row["modelRef"]]
        if matches and matches != [row]:
            raise ValueError("different existing runtime bundle: " + row["modelRef"])
        if not matches:
            bundles.append(copy.deepcopy(row))
    for model, qualification in source["qualifications"].items():
        current = envelope["qualifications"].get(model)
        if current is not None and current != qualification:
            raise ValueError("different existing model qualification: " + model)
        available = {p["acceleratorClass"] for p in envelope["pools"].values()}
        if not set(qualification["acceleratorClasses"]).issubset(available):
            raise ValueError("missing declared compatible pool: " + model)
        envelope["qualifications"][model] = copy.deepcopy(qualification)
    envelope.pop("revision", None)
    envelope["revision"] = canonical_digest(envelope)
    InfrastructureEnvelope.model_validate(envelope)
    return envelope, bundles


def configmaps(envelope: dict, bundles: list) -> dict:
    resources = []
    for suffix, key, value in (
        ("envelope", "infrastructure-envelope.json", envelope),
        ("bundles", "renderer-bundles.json", bundles),
    ):
        data = {key: canonical_json(value).decode()}
        digest = hashlib.sha256(canonical_json(data)).hexdigest()[:12]
        resources.append({
            "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": "fs2-idle-" + suffix + "-" + digest, "namespace": "fs2-system"},
            "data": data,
        })
    return {"apiVersion": "v1", "kind": "List", "items": resources}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--envelope", type=Path, required=True, help="raw base infrastructure-envelope.json")
    parser.add_argument("--bundles", type=Path, required=True, help="raw base renderer-bundles.json")
    args = parser.parse_args()
    merged = merge_registration(json.loads(args.envelope.read_text()),
                                json.loads(args.bundles.read_text()), json.loads(SOURCE.read_text()))
    print(json.dumps(configmaps(*merged), indent=2))

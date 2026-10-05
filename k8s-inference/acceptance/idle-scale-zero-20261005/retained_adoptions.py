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
ROOT = SOURCE.parents[2]


def retained_runtimes(source: dict) -> dict:
    """Resolve versioned catalog files; never invent deployment qualification."""
    expected = set(source["modelIds"])
    assert set(source["requiredPoolRefs"]) == set(source["runtimeSources"]) == expected
    runtimes = {}
    for model, reference in source["runtimeSources"].items():
        path = (ROOT / reference["path"]).resolve()
        if not path.is_relative_to(ROOT / "catalog/runtime") or path.name != model + ".json":
            raise ValueError("unexpected runtime source path: " + model)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != reference["sha256"]:
            raise ValueError("changed runtime source digest: " + model)
        runtime = json.loads(content)
        qualification = source["qualifications"][model]
        record = runtime["record"]
        assert record["model"]["id"] == model
        assert "sha256:" + record["cache"]["artifact"]["manifest_digest"] in qualification["artifactManifestDigests"]
        assert all(image.endswith("@" + record["runtime"]["image"]["digest"])
                   for image in qualification["runtimeImages"])
        if runtime["schema"] == "fs2-serve.nebius.ai/deployment-runtime/v1":
            runtimes[model] = runtime
        else:
            assert runtime["schema"] == "fs2-serve.nebius.ai/native-catalog-model/v1"
    return runtimes


def merge_registration(envelope: dict, bundles: list, source: dict) -> tuple[dict, list]:
    """Preserve siblings and refuse accidental rollback of a newer registration."""
    assert source["schema"] == "fs2-serve.nebius.ai/retained-managed-adoptions/v1"
    expected = set(source["modelIds"])
    assert set(source["qualifications"]) == expected
    assert {row["modelRef"] for row in source["bundles"]} == expected
    assert len(source["bundles"]) == len(expected)
    retained_runtimes(source)
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
        pool_refs = source["requiredPoolRefs"][model]
        if not pool_refs or not all(
            pool_ref in envelope["pools"] and
            envelope["pools"][pool_ref]["acceleratorClass"] in qualification["acceleratorClasses"]
            for pool_ref in pool_refs
        ):
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

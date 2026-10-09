"""Prepare only the independently qualified explicit-control adapter successor.

Runtime image, weights, model arguments, owner settings and historical templates
stay unchanged. This helper never writes Kubernetes resources or enables a
snapshot. The release coordinator merges the proposal with its current maps.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from fs2_serve.model_deployment import canonical_digest

SPEC = importlib.util.spec_from_file_location("cosmos_presets_base", Path(__file__).with_name("prepare.py"))
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)

ADAPTER_SHA256 = "91d90dcb3e70e47de10c0e3306e74bd7e01188bde777f80e24b6fb9578c064a4"
TEMPLATE = "cosmos3-nano.recorded-video-presets-20260918"


def validate_preset_evidence(report):
    rows = {row["case"]: row for row in report["different_gpu_cases"]}
    for kind, low, high in (("edge", "very_low", "very_high"), ("blur", "low", "high")):
        prefix = "transfer-" + kind + "-"
        values = [rows[prefix + suffix] for suffix in (low, high, low + "-repeat")]
        for row in values:
            if row["same_gpu"]["output_sha256"] != row["different_gpu"]["output_sha256"]:
                raise ValueError("preset output differs across measured GPUs")
        hashes = [row["same_gpu"]["output_sha256"] for row in values]
        if hashes[0] == hashes[1] or hashes[0] != hashes[2]:
            raise ValueError("explicit preset contrast or repeatability was not measured")


def extend(envelope, bundles, owner, source, report):
    if hashlib.sha256(source.encode()).hexdigest() != ADAPTER_SHA256:
        raise ValueError("adapter source differs from the frozen measured successor")
    validate_preset_evidence(report)
    current = owner["spec"]
    if current["modelRef"] != base.MODEL or current["runtime"]["image"] != base.NEW:
        raise ValueError("presets require the already qualified V4 image")
    envelope, bundles = copy.deepcopy((envelope, bundles))
    previous = next(item for item in bundles if item["templateDigest"] == current["runtime"]["templateRef"]["digest"])
    candidate = copy.deepcopy(previous)
    adapters = [
        resource
        for resource in candidate["resources"]
        if resource["kind"] == "ConfigMap" and "adapter.py" in resource.get("data", {})
    ]
    if len(adapters) != 1:
        raise ValueError("expected exactly one native adapter ConfigMap")
    old_name = adapters[0]["metadata"]["name"]
    new_name = "cosmos3-nano-adapter-" + ADAPTER_SHA256[:12]
    if old_name == new_name:
        raise ValueError("preset successor is already selected")
    adapters[0]["metadata"]["name"] = new_name
    adapters[0]["data"]["adapter.py"] = source
    changed = 0
    for resource in candidate["resources"]:
        if resource["kind"] == "Deployment":
            for volume in resource["spec"]["template"]["spec"]["volumes"]:
                if volume.get("configMap", {}).get("name") == old_name:
                    volume["configMap"]["name"] = new_name
                    changed += 1
    if changed != 1:
        raise ValueError("unexpected native adapter mount topology")
    candidate["templateDigest"] = base.existing._prior.terraform_digest(candidate["resources"])
    base.existing.LegacyTemplateBundle.model_validate(candidate)
    qualification = envelope["qualifications"][base.MODEL]
    if TEMPLATE in qualification["templateRefs"]:
        raise ValueError("preset successor already registered; inspect live state")
    qualification["templateRefs"][TEMPLATE] = candidate["templateDigest"]
    qualification["templateDigests"].append(candidate["templateDigest"])
    qualification["templateCacheTiers"][candidate["templateDigest"]] = copy.deepcopy(
        qualification["templateCacheTiers"][previous["templateDigest"]]
    )
    bundles.append(candidate)
    proposal = copy.deepcopy(current)
    proposal["runtime"]["templateRef"] = {"name": TEMPLATE, "digest": candidate["templateDigest"]}
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    return (
        envelope,
        bundles,
        {"name": owner["metadata"]["name"], "namespace": owner["metadata"]["namespace"], "spec": proposal},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("envelope", "bundles", "owner", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(Path(__file__).with_name("warmed-snapshot").joinpath("qualification.json").read_bytes())
    result = extend(
        *(json.loads(path.read_bytes()) for path in (args.envelope, args.bundles, args.owner)),
        base.adapter_source(),
        report,
    )
    args.output.mkdir(exist_ok=False)
    for name, value in zip(
        ("infrastructure-envelope.json", "renderer-bundles.json", "owner-proposal.json"), result, strict=True
    ):
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps({"status": "prepared-not-applied", "template": TEMPLATE}))


if __name__ == "__main__":
    main()

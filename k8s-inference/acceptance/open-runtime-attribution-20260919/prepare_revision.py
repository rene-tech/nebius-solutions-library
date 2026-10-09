"""Prepare the missing-revision metadata repair, retaining the exact wrapper image."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path

import prepare_promotion as prior


def extend(envelope, bundles, owner):
    spec = owner["spec"]
    if spec["runtime"]["image"] != prior.IMAGE or spec["cache"]["snapshotPreference"] != "Never":
        raise ValueError("expected exact qualified wrapper without inherited snapshots")
    old_digest = spec["runtime"]["templateRef"]["digest"]
    selected = [b for b in bundles if b["modelRef"] == "diffdock" and b["templateDigest"] == old_digest]
    if len(selected) != 1:
        raise ValueError("exact current template required")
    repaired = prior.candidate.complete_revision_template(selected[0], spec)
    result = copy.deepcopy(envelope)
    qualification = result["qualifications"]["diffdock"]
    name = prior.candidate.REVISION_TEMPLATE_NAME
    if name in qualification["templateRefs"]:
        raise ValueError("repair name is already registered; do not replace immutable history")
    qualification["templateDigests"].append(repaired["templateDigest"])
    qualification["templateRefs"][name] = repaired["templateDigest"]
    qualification["templateCacheTiers"][repaired["templateDigest"]] = copy.deepcopy(
        qualification["templateCacheTiers"][old_digest]
    )
    result.pop("revision")
    result["revision"] = prior.existing.canonical_digest(result)
    proposal = {
        "name": owner["metadata"]["name"],
        "namespace": owner["metadata"]["namespace"],
        "spec": copy.deepcopy(spec),
    }
    proposal["spec"]["runtime"]["templateRef"] = {"name": name, "digest": repaired["templateDigest"]}
    return result, [*copy.deepcopy(bundles), repaired], [proposal]


def prepare(baseline, owners_path, admin_owner_path):
    maps = prior.read(baseline / "configmaps.json")["items"]

    def one(key):
        matches = [m for m in maps if key in m.get("data", {})]
        if len(matches) != 1:
            raise ValueError("exact mounted map required:" + key)
        return matches[0]

    values = prior.read(baseline / "values.json")
    envelope_map, bundles_map = (one(key) for key in ("infrastructure-envelope.json", "renderer-bundles.json"))
    route_map, admin_map = (one(key) for key in ("deployment-runtimes.json", "admin-configuration.json"))
    originals = [envelope_map, bundles_map, route_map, admin_map]
    references = [
        values["modelController"]["infrastructureEnvelopeConfigMapName"],
        values["modelController"]["rendererBundlesConfigMapName"],
        values["catalog"]["leanRoutes"]["configMapName"],
        values["adminConfiguration"]["configMapName"],
    ]
    if references != [m["metadata"]["name"] for m in originals]:
        raise ValueError("captured Helm values and mounted maps differ")
    envelope = json.loads(envelope_map["data"]["infrastructure-envelope.json"])
    bundles = json.loads(bundles_map["data"]["renderer-bundles.json"])
    owners = prior.read(owners_path)["items"]
    owner = next(o for o in owners if o["spec"]["modelRef"] == "diffdock")
    admin_owner = prior.read(admin_owner_path)
    if prior.candidate.ModelDeploymentSpec.model_validate(
        owner["spec"]
    ) != prior.candidate.ModelDeploymentSpec.model_validate(admin_owner["spec"]) or not admin_owner.get("etag"):
        raise ValueError("admin/Kubernetes desired revisions differ")
    entries = json.loads(route_map["data"]["deployment-runtimes.json"])["models"]
    entry = entries["diffdock"]
    if (
        entry["record"]["runtime"]["image"]["digest"] != prior.IMAGE.rsplit("@", 1)[1]
        or entry["record"]["model"]["source"]["revision"] != owner["spec"]["artifact"]["revision"]
    ):
        raise ValueError("selected exact runtime and desired revision differ")
    new_envelope, new_bundles, proposals = extend(envelope, bundles, owner)
    validation = prior.existing.validate_candidate(envelope, new_envelope, new_bundles, owners, proposals)
    validation["gateway_bootstrap"] = asyncio.run(
        prior.existing.validate_admin_configuration(json.loads(admin_map["data"]["admin-configuration.json"]), entries)
    )
    validation["registry"] = prior.registry.validate_registry(route_map["data"], one("serving-bindings.json"))
    objects = [
        prior.existing.configmap(
            "fs2-science-envelope-",
            {"infrastructure-envelope.json": prior.existing.canonical_json(new_envelope).decode()},
        ),
        prior.existing.configmap(
            "fs2-science-bundles-", {"renderer-bundles.json": prior.existing.canonical_json(new_bundles).decode()}
        ),
        *copy.deepcopy([route_map, admin_map]),
    ]
    delta = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
            "rendererBundlesConfigMapName": objects[1]["metadata"]["name"],
        }
    }
    return {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
        "values.json": delta,
        "full-values.json": prior.merge_values(values, delta),
        "rollback-values.json": values,
        "app-proposals.json": proposals,
        "rollback-app-proposals.json": [
            {"name": owner["metadata"]["name"], "namespace": owner["metadata"]["namespace"], "spec": owner["spec"]}
        ],
        "captured-admin-owner.json": admin_owner,
        "validation.json": {
            "applied": False,
            "public_qualified": False,
            "validation": validation,
            "runtime_image_changed": False,
            "routes_admin_maps_changed": False,
            "all_prior_templates_preserved": new_bundles[:-1] == bundles,
            "all_sibling_qualifications_preserved": all(
                new_envelope["qualifications"][key] == value
                for key, value in envelope["qualifications"].items()
                if key != "diffdock"
            ),
            "all_other_envelope_fields_preserved": all(
                new_envelope[key] == value
                for key, value in envelope.items()
                if key not in {"revision", "qualifications"}
            ),
            "model_revision": owner["spec"]["artifact"]["revision"],
            "sources_sha256": {
                str(path): prior.sha(path)
                for path in (baseline / "configmaps.json", baseline / "values.json", owners_path, admin_owner_path)
            },
            "scope": "Template metadata repair only; original public attribution failures remain failures.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "owners", "admin-owner", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("new output directory required")
    outputs = prepare(args.baseline, args.owners, args.admin_owner)
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {"applied": False, "values": outputs["values.json"], "image": prior.IMAGE, "output": str(args.output)}
        )
    )


if __name__ == "__main__":
    main()

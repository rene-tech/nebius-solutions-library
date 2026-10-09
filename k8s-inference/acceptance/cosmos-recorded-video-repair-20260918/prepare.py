"""Prepare an immutable Cosmos fresh-load successor; never apply or publish."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import sys
from fractions import Fraction
from pathlib import Path

import yaml

from fs2_serve.deployment_runtimes import _canonical_service_contract, _record, deployment_runtime_model_schema
from fs2_serve.model_deployment import canonical_digest, canonical_json
from fs2_serve.qualification import _runtime_origin

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.util.spec_from_file_location(
    "cosmos_reviewed_promotion", ROOT / "acceptance/ct-evo2-runtime-repair-20260918/prepare.py"
)
shared = importlib.util.module_from_spec(loader)
sys.modules[loader.name] = shared
loader.loader.exec_module(shared)
existing = shared.existing
MODEL = "cosmos3-nano"
OLD = "6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587"
NEW = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/cosmos3-contract@sha256:"
    "5e2680aa1f8332413638ec1bc962c3796a79a314c1c84f5456d32aa916839e32"
)
SOURCE = "711590a103384931e5378c1b5d6cea76a6f0d3a3"
TEMPLATE = "cosmos3-nano.recorded-video-repair-20260918"


def adapter_source():
    documents = yaml.safe_load_all((ROOT / "models/general-media/k8s/cosmos3-nano.yaml").read_text())
    return next(d["data"]["adapter.py"] for d in documents if d["kind"] == "ConfigMap")


def template(previous):
    result = copy.deepcopy(previous)
    adapter = adapter_source()
    source_name = "cosmos3-nano-adapter"
    target_name = source_name + "-" + hashlib.sha256(adapter.encode()).hexdigest()[:12]
    maps = [r for r in result["resources"] if r["kind"] == "ConfigMap" and r["metadata"]["name"] == source_name]
    if len(maps) != 1:
        raise ValueError("expected one exact Cosmos adapter contract")
    maps[0]["metadata"]["name"] = target_name
    maps[0]["data"]["adapter.py"] = adapter
    images, volumes = 0, 0
    for resource in result["resources"]:
        if resource["kind"] != "Deployment":
            continue
        for metadata in (resource["metadata"], resource["spec"]["template"].get("metadata", {})):
            if "fs2.nebius/runtime-image-digest" in metadata.get("annotations", {}):
                metadata["annotations"]["fs2.nebius/runtime-image-digest"] = NEW.split("@")[1]
        pod = resource["spec"]["template"]["spec"]
        for container in [*pod["containers"], *pod.get("initContainers", [])]:
            if container["image"].endswith("@sha256:" + OLD):
                container["image"] = NEW
                images += 1
        for volume in pod["volumes"]:
            if volume.get("configMap", {}).get("name") == source_name:
                volume["configMap"]["name"] = target_name
                volumes += 1
    if (images, volumes) != (3, 1):
        raise ValueError("unexpected exact Cosmos image or adapter topology")
    result["templateDigest"] = existing._prior.terraform_digest(result["resources"])
    existing.LegacyTemplateBundle.model_validate(result)
    return result


def successor(base, row, evidence):
    record, qualification = copy.deepcopy((base, row))
    record["runtime"]["image"].update(reference=NEW, digest=NEW.split("@")[1], state="resolved")
    record["runtime"]["version"] = "cosmos3-recorded-video-contract-20260918-v4"
    record["resources"]["gpu"].update(
        {"class": "NVIDIA-H100-SXM5-80GB", "b300_state": "unverified", "alternatives": []}
    )
    limits = [
        "Same NVIDIA Cosmos3 checkpoint, native precision and model kernels; source-pinned transfer explicit-size fix.",
        "Isolated H100 recorded ALOHA V2V, edge transfer, T2V, I2V, audio and bounded queue tests; "
        "exact decoded frame count, dimensions and FPS verified.",
        "First-frame V2V can change motion; edge transfer better follows the input but is not guaranteed "
        "lighting-only or policy-safe augmentation.",
        "Fresh loading only. The historical r7 CUDA snapshot fails recorded-video VAE decoding "
        "and is not eligible for this runtime.",
        "New public MCP, LeRobot end-to-end, cold-start, elasticity, action-dynamics and GPU-snapshot "
        "qualification are not inherited.",
    ]
    record["support"]["limitations"] = limits
    record["evidence"].append(
        {
            "classification": "measured-platform",
            "hardware": "NVIDIA H100 80GB HBM3; driver580.159.04",
            "outcome": "live-qualified",
            "source_commit": SOURCE,
            "summary": "Isolated evidence SHA256 " + shared.evidence_sha256(evidence) + ". " + " ".join(limits),
        }
    )
    qualification["variant_id"] = None
    qualification["runtime_origin"] = _runtime_origin(shared.catalog(), MODEL, None)
    qualification["active_runtime"]["runtime_image_digest"] = NEW.split("@")[1]
    qualification["states"] = {
        key: key in {"registered", "runtime_ready", "semantic_qualified"} for key in qualification["states"]
    }
    qualification["evidence"] = {
        key: value if key == "audited_catalog_sha256" else None for key, value in qualification["evidence"].items()
    }
    qualification["evidence"]["retained_deployments_sha256"] = shared.evidence_sha256(evidence)
    _record(
        record,
        MODEL,
        None,
        shared.catalog(),
        deployment_runtime_model_schema(ROOT / "catalog/runtime"),
        canonical_contract=_canonical_service_contract(shared.catalog(), ROOT / "catalog/runtime", MODEL),
    )
    return {
        "schema": "fs2-serve.nebius.ai/deployment-runtime/v1",
        "model_id": MODEL,
        "variant_id": None,
        "record": record,
        "qualification": qualification,
    }


def extend(envelope, bundles, routes, admin, owners, entry):
    original = copy.deepcopy((envelope, bundles, routes, admin))
    envelope, bundles, routes, admin = copy.deepcopy(original)
    found = [owner for owner in owners if owner["metadata"]["name"] == MODEL and owner["spec"]["modelRef"] == MODEL]
    if len(found) != 1:
        raise ValueError("expected exact current Cosmos owner")
    owner = found[0]
    current = owner["spec"]
    if not current["runtime"]["image"].endswith("@sha256:" + OLD):
        raise ValueError("unexpected current Cosmos image")
    old_digest = current["runtime"]["templateRef"]["digest"]
    previous = [item for item in bundles if item["modelRef"] == MODEL and item["templateDigest"] == old_digest]
    if len(previous) != 1:
        raise ValueError("expected exact current Cosmos template")
    candidate = template(previous[0])
    qualification = envelope["qualifications"][MODEL]
    if NEW in qualification["runtimeImages"] or TEMPLATE in qualification["templateRefs"]:
        raise ValueError("candidate already present; inspect current state")
    qualification["runtimeImages"].append(NEW)
    qualification["templateDigests"].append(candidate["templateDigest"])
    qualification["templateRefs"][TEMPLATE] = candidate["templateDigest"]
    qualification["templateCacheTiers"][candidate["templateDigest"]] = qualification["templateCacheTiers"][old_digest]
    bundles.append(candidate)
    proposed = copy.deepcopy(current)
    proposed["runtime"].update(image=NEW, templateRef={"name": TEMPLATE, "digest": candidate["templateDigest"]})
    proposed["cache"]["snapshotPreference"] = "Never"
    proposed["cache"].pop("snapshotRef", None)
    proposed["fastStart"]["level"] = "Off"
    proposals = [{"name": MODEL, "namespace": owner["metadata"]["namespace"], "spec": proposed}]
    runtimes = json.loads(routes["deployment-runtimes.json"])
    before = existing.catalog_configuration_contracts(shared.catalog(), deployment_runtime_entries=runtimes["models"])
    runtimes["models"][MODEL] = copy.deepcopy(entry)
    after = existing.catalog_configuration_contracts(shared.catalog(), deployment_runtime_entries=runtimes["models"])
    artifact = admin["models"][MODEL]["artifact"]
    for field, attribute in shared.IDENTITIES.items():
        if artifact[field] != getattr(before[MODEL], attribute):
            raise ValueError("stale Cosmos admin identity:" + field)
        artifact[field] = getattr(after[MODEL], attribute)
    artifact["image_repository"] = NEW.split("@")[0]
    projection = json.loads(routes["qualification-projection.json"])
    rows = [row for row in projection["rows"] if row["model_id"] == MODEL]
    if len(rows) != 1:
        raise ValueError("expected one Cosmos qualification row")
    rows[0].clear()
    rows[0].update(copy.deepcopy(entry["qualification"]))
    routes["deployment-runtimes.json"] = canonical_json(runtimes).decode()
    routes["qualification-projection.json"] = canonical_json(projection).decode()
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    if bundles[:-1] != original[1] or any(
        envelope["qualifications"][m] != original[0]["qualifications"][m]
        for m in envelope["qualifications"]
        if m != MODEL
    ):
        raise ValueError("historical template or sibling changed")
    return envelope, bundles, routes, admin, proposals


def media_check(probe, request):
    stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
    width, height = map(int, request["size"].split("x"))
    if (stream["width"], stream["height"], int(stream["nb_read_frames"]), Fraction(stream["avg_frame_rate"])) != (
        width,
        height,
        request["num_frames"],
        Fraction(request["fps"]),
    ):
        raise ValueError("decoded media contract differs from request")
    if any(s["codec_type"] == "audio" for s in probe["streams"]) != request.get("generate_sound", False):
        raise ValueError("decoded audio contract differs from request")


def isolated_evidence(directory):
    read, sha = shared.read, shared.sha
    pod = read(directory / "pod-final.json")
    if {c["image"] for c in pod["spec"]["containers"]} != {NEW} or any(
        s["restartCount"] for s in pod["status"]["containerStatuses"]
    ):
        raise ValueError("candidate image or restart identity differs")
    receipt = read(directory / "response-receipt.json")
    if receipt["status_code"] != 200 or receipt["body_sha256"] != sha(directory / "output.mp4"):
        raise ValueError("frozen original replay failed or changed")
    media_check(read(directory / "output-ffprobe.json"), read(directory / "request-redacted.json"))
    summary = read(directory / "matrix/summary.json")
    rows = summary["cases"]
    if len(rows) != 11 or sum(r["status_code"] == 200 for r in rows) != 10:
        raise ValueError("candidate matrix incomplete or failed")
    receipts = []
    for row in rows:
        case = directory / "matrix" / row["case"]
        if row != read(case / "receipt.json"):
            raise ValueError("summary differs from authoritative case receipt")
        if row["status_code"] == 200:
            if not row["valid"] or row["sha256"] != sha(case / "output.mp4"):
                raise ValueError("candidate media changed or failed")
            media_check(read(case / "ffprobe.json"), read(case / "request.json"))
        elif row["status_code"] != 429 or not row["case"].startswith("queue-") or not row["headers"].get("retry-after"):
            raise ValueError("unexpected candidate error")
        receipts.append(
            {
                "case": row["case"],
                "status_code": row["status_code"],
                "receipt_sha256": sha(case / "receipt.json"),
                "output_sha256": row["sha256"],
            }
        )
    return {
        "model_id": MODEL,
        "image": NEW,
        "generation_successes": 11,
        "expected_queue_rejections": 1,
        "rows": receipts,
        "original_receipt_sha256": sha(directory / "response-receipt.json"),
        "pod_sha256": sha(directory / "pod-final.json"),
        "gpu_sha256": sha(directory / "gpu.csv"),
        "source_sha256": sha(directory / "source-sha256.txt"),
        "scope": "Isolated H100 media contract, not a public path, robotics-policy, "
        "cold-start or snapshot qualification.",
    }


def prepare(args):
    read, sha = shared.read, shared.sha
    capture = read(args.baseline / "capture.json")
    if capture["release"]["version"] != args.expected_release or capture["release"]["status"] != "deployed":
        raise ValueError("expected explicitly selected stable deployed release")
    for name, digest in capture["sha256"].items():
        if sha(args.baseline / name) != digest:
            raise ValueError("baseline capture changed:" + name)
    pins = [
        shared.source_pin(ROOT / name, SOURCE)
        for name in (
            "models/general-media/cosmos3_contract_patch.py",
            "models/general-media/k8s/cosmos3-nano.yaml",
            "models/general-media/Dockerfile.cosmos3-contract",
        )
    ]
    pins.append(shared.source_pin(Path(__file__), args.source_commit))
    evidence = isolated_evidence(args.evidence)
    maps = read(args.baseline / "live-configmaps.json")["items"]

    def document(key):
        return json.loads(next(item["data"][key] for item in maps if key in item.get("data", {})))

    original, old_bundles = document("infrastructure-envelope.json"), document("renderer-bundles.json")
    owners = read(args.baseline / "modeldeployments.json")["items"]
    routes = read(args.baseline / "live-routes.json")["data"]
    old_admin = json.loads(read(args.baseline / "live-admin-configuration.json")["data"]["admin-configuration.json"])
    row = next(row for row in json.loads(routes["qualification-projection.json"])["rows"] if row["model_id"] == MODEL)
    entry = successor(shared.catalog().model(MODEL).to_dict(), row, evidence)
    candidate, bundles, new_routes, admin, proposals = extend(original, old_bundles, routes, old_admin, owners, entry)
    checks = existing.validate_candidate(original, candidate, bundles, owners, proposals)
    checks["gateway_registry"] = shared.validate_registry(new_routes, read(args.serving_bindings))
    checks["gateway_bootstrap"] = asyncio.run(
        existing.validate_admin_configuration(admin, json.loads(new_routes["deployment-runtimes.json"])["models"])
    )
    objects = [
        existing.configmap(
            "fs2-science-envelope-", {"infrastructure-envelope.json": canonical_json(candidate).decode()}
        ),
        existing.configmap("fs2-science-bundles-", {"renderer-bundles.json": canonical_json(bundles).decode()}),
        existing.configmap("fs2-science-routes-", new_routes),
        existing.configmap("fs2-science-admin-", {"admin-configuration.json": canonical_json(admin).decode()}),
    ]

    def values(items, configuration):
        def name(key):
            return next(m["metadata"]["name"] for m in items if key in m.get("data", {}))

        return {
            "modelController": {
                "infrastructureEnvelopeConfigMapName": name("infrastructure-envelope.json"),
                "rendererBundlesConfigMapName": name("renderer-bundles.json"),
            },
            "catalog": {"leanRoutes": {"configMapName": name("deployment-runtimes.json")}},
            "adminConfiguration": {
                "configMapName": name("admin-configuration.json"),
                "sha256": hashlib.sha256(canonical_json(configuration)).hexdigest(),
            },
        }

    outputs = {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
        "values.json": values(objects, admin),
        "rollback-values.json": values(maps, old_admin),
        "app-proposals.json": proposals,
        "rollback-app-proposals.json": [
            {"name": o["metadata"]["name"], "namespace": o["metadata"]["namespace"], "spec": o["spec"]}
            for o in owners
            if o["metadata"]["name"] == MODEL
        ],
        "successor-runtime-entries.json": {MODEL: entry},
        "isolated-evidence.json": evidence,
        "validation.json": {
            "applied": False,
            "source_pins": pins,
            "baseline": capture,
            "validation": checks,
            "historical_bundles_preserved": True,
            "snapshot_restore_disabled": True,
            "new_public_cold_elastic_snapshot_qualification": False,
        },
    }
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    return {
        "applied": False,
        "values": outputs["values.json"],
        "current_owner_checks": len(checks["current_modeldeployments"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "evidence", "serving-bindings", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--expected-release", required=True, type=int)
    print(json.dumps(prepare(parser.parse_args())))

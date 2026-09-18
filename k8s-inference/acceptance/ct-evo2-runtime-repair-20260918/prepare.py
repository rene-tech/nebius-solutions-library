"""Prepare CT/Evo2 immutable contracts from fresh captures; never apply."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from fs2_serve.deployment_runtimes import _record, deployment_runtime_model_schema
from fs2_serve.model_deployment import canonical_digest, canonical_json
from fs2_serve.qualification import _runtime_origin
from fs2_serve.registry import Registry

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "acceptance/scientific-runtime-repair-20260918/prepare_promotion.py"
spec = importlib.util.spec_from_file_location("ct_evo2_existing_promotion", HELPER)
existing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = existing
spec.loader.exec_module(existing)

PREFIX = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/"
OLD = {
    "evo2-40b": "383f9979021bd3fe018c4dbba675610e0a5f7282b2164db1d8386611351de6f5",
    "nv-segment-ct": "834b6694b7e096c393193d12306ef9b3f0bb313efa806a9c253f49f1f47281fd",
}
NEW = {
    "evo2-40b": PREFIX + "evo2-runtime@sha256:8c1a5dca0c32b497e04afaf93215809499a5736666540f4eb5b344dd84b709ee",
    "nv-segment-ct": PREFIX + "nv-segment-ct@sha256:7018f64b933638412120b780ef9f45bd9239af3c45f27ab6885b55ee12c1b626",
}
SOURCE = {
    "evo2-40b": "8b2da90804a945cf2299b1f775a37bce6d44574a",
    "nv-segment-ct": "10aa18eb9c7f35deeda1f67b3acc458930291137",
}
CACHE_ENV = {
    "FS2_RUNTIME_CACHE_ROOT",
    "CUDA_CACHE_PATH",
    "TORCH_EXTENSIONS_DIR",
    "TORCHINDUCTOR_CACHE_DIR",
    "TRITON_CACHE_DIR",
}
IDENTITIES = {
    "artifact_manifest_sha256": "artifact_manifest_sha256",
    "acquisition_contract_sha256": "acquisition_contract_sha256",
    "provenance_sha256": "provenance_sha256",
    "semantic_health_contract_sha256": "semantic_health_contract_sha256",
    "image_digest": "runtime_image_digest",
    "model_revision": "model_revision",
}


def catalog():
    directory = ROOT / "catalog/runtime"
    packaged = directory / "packaged-repository"
    return existing.augment_native_catalog(
        existing.load_catalog(directory, repo_root=packaged), directory, repo_root=packaged
    )


def template(previous, model_id):
    candidate = copy.deepcopy(previous)
    changed_images, changed_paths = 0, 0
    for resource in candidate["resources"]:
        if resource["kind"] != "Deployment":
            continue
        annotations = resource["metadata"]["annotations"]
        old_cache_digest = annotations["fs2.nebius/runtime-image-digest"].removeprefix("sha256:")
        new_digest = NEW[model_id].split("@sha256:")[1]
        annotations["fs2.nebius/runtime-image-digest"] = "sha256:" + new_digest
        for metadata in (resource["spec"]["template"].get("metadata", {}),):
            if "fs2.nebius/runtime-image-digest" in metadata.get("annotations", {}):
                metadata["annotations"]["fs2.nebius/runtime-image-digest"] = "sha256:" + new_digest
        pod = resource["spec"]["template"]["spec"]
        for container in [*pod["containers"], *pod.get("initContainers", [])]:
            if container["image"].endswith("@sha256:" + OLD[model_id]):
                container["image"] = NEW[model_id]
                changed_images += 1
            for env in container.get("env", []):
                if env["name"] not in CACHE_ENV:
                    continue
                old_prefix = "/model-cache/.fs2/runtime/" + old_cache_digest + "/"
                if not env.get("value", "").startswith(old_prefix):
                    raise ValueError("unexpected image-keyed compiler path:" + model_id + ":" + env["name"])
                env["value"] = env["value"].replace(old_prefix, "/model-cache/.fs2/runtime/" + new_digest + "/", 1)
                changed_paths += 1
    if changed_images != {"evo2-40b": 4, "nv-segment-ct": 2}[model_id] or changed_paths != 5:
        raise ValueError("unexpected exact template image/cache topology:" + model_id)
    candidate["templateDigest"] = existing._prior.terraform_digest(candidate["resources"])
    existing.LegacyTemplateBundle.model_validate(candidate)
    return candidate


def successor(base, row, model_id, evidence_sha, source_commit):
    record, qualification = copy.deepcopy((base, row))
    image = NEW[model_id]
    record["runtime"]["image"].update(reference=image, digest=image.split("@")[1], state="resolved")
    record["runtime"]["version"] = "h100-scientific-repair-20260918-v2"
    record["resources"]["gpu"].update(
        {"class": "NVIDIA-H100-SXM5-80GB", "b300_state": "unverified", "alternatives": []}
    )
    limits = (
        [
            "Same Evo2 checkpoint and native precision; bounded modal FFT and responsive health on two H100s.",
            "96 exact-image plant requests, full-state/logit comparison and production-timing health observations "
            "are the isolated promotion gate.",
        ]
        if model_id == "evo2-40b"
        else [
            "Research-only, non-clinical NV-Segment-CT on one H100; label, point and combined prompts tested.",
            "54 requests cover nine expert-labelled spleen scans twice per prompt mode. Point prompts are derived "
            "from those masks, not independent clinical validation.",
        ]
    )
    limits.append(
        "Historical lightweight health validators remain available; new public MCP, cold-start, elasticity "
        "and GPU-snapshot qualification are not inherited."
    )
    record["support"]["limitations"] = limits
    record["evidence"].append(
        {
            "classification": "measured-platform",
            "hardware": "NVIDIA H100 80GB HBM3; driver580.159.04",
            "outcome": "live-qualified",
            "source_commit": source_commit,
            "summary": "Isolated exact-image acceptance SHA256 " + evidence_sha + ". " + " ".join(limits),
        }
    )
    variant = "evo2-40b-upstream-portable" if model_id == "evo2-40b" else "nv-segment-ct-upstream-portable"
    qualification["variant_id"] = variant
    qualification["runtime_origin"] = _runtime_origin(catalog(), model_id, variant)
    qualification["active_runtime"]["runtime_image_digest"] = image.split("@")[1]
    qualification["states"] = {
        key: key in {"registered", "runtime_ready", "semantic_qualified"} for key in qualification["states"]
    }
    qualification["evidence"] = {
        key: value if key == "audited_catalog_sha256" else None for key, value in qualification["evidence"].items()
    }
    qualification["evidence"]["retained_deployments_sha256"] = evidence_sha
    _record(record, model_id, variant, catalog(), deployment_runtime_model_schema(ROOT / "catalog/runtime"))
    return {
        "schema": "fs2-serve.nebius.ai/deployment-runtime/v1",
        "model_id": model_id,
        "variant_id": variant,
        "record": record,
        "qualification": qualification,
    }


def extend(envelope, bundles, routes, admin, owners, successors):
    if set(successors) != set(NEW):
        raise ValueError("both exact successors are required")
    original = copy.deepcopy((envelope, bundles, routes, admin))
    envelope, bundles, routes, admin = copy.deepcopy(original)
    models = set(envelope["qualifications"])
    if len(models) != 20 or not existing.VOICES <= models or not set(NEW) <= models:
        raise ValueError("expected complete current twenty-App contract")
    runtimes = json.loads(routes["deployment-runtimes.json"])
    projection = json.loads(routes["qualification-projection.json"])
    old_entries = copy.deepcopy(runtimes["models"])
    before_contracts = existing.catalog_configuration_contracts(catalog(), deployment_runtime_entries=old_entries)
    proposals = []
    for model_id, entry in successors.items():
        found = [
            owner for owner in owners if owner["metadata"]["name"] == model_id and owner["spec"]["modelRef"] == model_id
        ]
        if len(found) != 1:
            raise ValueError("expected one canonical owner:" + model_id)
        owner = found[0]
        current = owner["spec"]
        if not current["runtime"]["image"].endswith("@sha256:" + OLD[model_id]):
            raise ValueError("unexpected current owner image:" + model_id)
        if entry["record"]["runtime"]["image"]["reference"] != NEW[model_id]:
            raise ValueError("successor image mismatch:" + model_id)
        flags = entry["qualification"]["states"]
        if {key for key, enabled in flags.items() if enabled} != {"registered", "runtime_ready", "semantic_qualified"}:
            raise ValueError("unsupported successor qualification claim:" + model_id)
        old_digest = current["runtime"]["templateRef"]["digest"]
        previous = [item for item in bundles if item["modelRef"] == model_id and item["templateDigest"] == old_digest]
        if len(previous) != 1:
            raise ValueError("expected exact current owner template:" + model_id)
        candidate = template(previous[0], model_id)
        qualification = envelope["qualifications"][model_id]
        name = model_id + ".scientific-repair-20260918"
        if NEW[model_id] in qualification["runtimeImages"] or name in qualification["templateRefs"]:
            raise ValueError("candidate already present; inspect current state")
        qualification["runtimeImages"].append(NEW[model_id])
        qualification["templateDigests"].append(candidate["templateDigest"])
        qualification["templateRefs"][name] = candidate["templateDigest"]
        qualification["templateCacheTiers"][candidate["templateDigest"]] = qualification["templateCacheTiers"][
            old_digest
        ]
        bundles.append(candidate)
        proposed = copy.deepcopy(current)
        proposed["runtime"].update(
            image=NEW[model_id], templateRef={"name": name, "digest": candidate["templateDigest"]}
        )
        proposed["cache"]["snapshotPreference"] = "Never"
        proposed["cache"].pop("snapshotRef", None)
        proposed["fastStart"]["level"] = "Off"
        proposals.append({"name": model_id, "namespace": owner["metadata"]["namespace"], "spec": proposed})
        runtimes["models"][model_id] = copy.deepcopy(entry)
        rows = [row for row in projection["rows"] if row["model_id"] == model_id]
        if len(rows) != 1:
            raise ValueError("expected unique qualification row:" + model_id)
        rows[0].clear()
        rows[0].update(copy.deepcopy(entry["qualification"]))
    after_contracts = existing.catalog_configuration_contracts(catalog(), deployment_runtime_entries=runtimes["models"])
    for model_id in successors:
        artifact = admin["models"][model_id]["artifact"]
        for field, attribute in IDENTITIES.items():
            if artifact[field] != getattr(before_contracts[model_id], attribute):
                raise ValueError("stale admin identity:" + model_id + ":" + field)
            artifact[field] = getattr(after_contracts[model_id], attribute)
        artifact["image_repository"] = NEW[model_id].split("@")[0]
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    routes["deployment-runtimes.json"] = canonical_json(runtimes).decode()
    routes["qualification-projection.json"] = canonical_json(projection).decode()
    if bundles[:-2] != original[1] or any(
        envelope["qualifications"][m] != original[0]["qualifications"][m] for m in models - set(NEW)
    ):
        raise ValueError("historical template or sibling changed")
    return envelope, bundles, routes, admin, proposals


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence_sha256(value):
    """Qualification evidence uses bare SHA256, unlike renderer identities."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_registry(routes, serving_bindings):
    """Execute the gateway's real static startup path, without making requests.

    Use captured bindings, not a fabricated disabled binding set. Registry.load
    runs bind_deployment_runtimes and the lean-route checks in startup order.
    Retry settings below are inert: this preflight never admits an operation.
    """
    bindings = (
        json.loads(serving_bindings["data"]["serving-bindings.json"])
        if serving_bindings.get("kind") == "ConfigMap"
        else serving_bindings
    )
    variant_promotions = (
        serving_bindings["data"].get("model-variant-promotions.json")
        if serving_bindings.get("kind") == "ConfigMap"
        else None
    )
    directory = ROOT / "catalog/runtime"
    with tempfile.TemporaryDirectory(prefix="fs2-ct-evo2-registry-") as temporary:
        files = Path(temporary)
        (files / "serving-bindings.json").write_bytes(canonical_json(bindings))
        for name in ("deployment-runtimes.json", "lean-routes.json"):
            (files / name).write_text(routes[name])
        promotions_file = None
        if variant_promotions is not None:
            promotions_file = files / "model-variant-promotions.json"
            promotions_file.write_text(variant_promotions)
        registry = Registry.load(
            directory,
            files / "serving-bindings.json",
            repo_root=directory / "packaged-repository",
            evidence_root=None,
            variant_promotions_file=promotions_file,
            lean_routes_file=files / "lean-routes.json",
            deployment_runtime_records_file=files / "deployment-runtimes.json",
            max_attempts=1,
            max_gpu_seconds_per_attempt=1,
            retry_base_seconds=1,
        )
        selected = json.loads(routes["deployment-runtimes.json"])["models"]
        for model_id, entry in selected.items():
            if (
                registry.get(model_id, require_enabled=False).gateway.runtime_image_digest
                != entry["record"]["runtime"]["image"]["digest"]
            ):
                raise ValueError("registry selected a different runtime:" + model_id)
        return {
            "valid": True,
            "registry_models": len(registry.list()),
            "selected_runtime_models": sorted(selected),
            "serving_bindings_sha256": evidence_sha256(bindings),
            "variant_promotions_sha256": (
                hashlib.sha256(variant_promotions.encode()).hexdigest() if variant_promotions is not None else None
            ),
            "deployment_runtimes_sha256": hashlib.sha256(routes["deployment-runtimes.json"].encode()).hexdigest(),
            "lean_routes_sha256": hashlib.sha256(routes["lean-routes.json"].encode()).hexdigest(),
            "scope": "Offline Registry.load with deployment-runtime and static-route binding; no live health claim.",
        }


def source_pin(path, commit):
    path = path.resolve()
    relative = "k8s-inference/" + str(path.relative_to(ROOT))
    git = shutil.which("git")
    if git is None:
        raise ValueError("git unavailable")
    raw = subprocess.check_output([git, "-C", str(ROOT), "show", f"{commit}:{relative}"])  # noqa: S603 - fixed Git read
    if raw != path.read_bytes():
        raise ValueError("source differs from pin:" + relative)
    return {"commit": commit, "path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def ct_evidence(directory):
    rows = []
    cohorts = []
    for cohort in ("r1b", "r2"):
        paths = sorted((directory / cohort).glob("*/receipt.json"))
        if len(paths) != 27:
            raise ValueError("CT cohort incomplete:" + cohort)
        modes, cases = Counter(), set()
        for path in paths:
            receipt, evaluation = read(path), read(path.with_name("evaluation.json"))
            if (
                receipt["image"] != NEW["nv-segment-ct"]
                or receipt["http_status"] != 200
                or receipt["service_semantic_pass"] is not True
                or evaluation["service_semantic_pass"] is not True
                or receipt["result_sha256"] != sha(path.with_name("result.json"))
            ):
                raise ValueError("CT failed or mismatched receipt:" + str(path))
            modes[evaluation["prompt_mode"]] += 1
            cases.add(receipt["case_id"])
            rows.append(
                {
                    "cohort": cohort,
                    "case_id": receipt["case_id"],
                    "receipt_sha256": sha(path),
                    "evaluation_sha256": sha(path.with_name("evaluation.json")),
                    "result_sha256": receipt["result_sha256"],
                }
            )
        if modes != {"label": 9, "point": 9, "label-plus-point": 9} or len(cases) != 27:
            raise ValueError("CT mode or unique-case matrix mismatch")
        cohorts.append(cases)
    if cohorts[0] != cohorts[1]:
        raise ValueError("CT cohorts differ")
    return {
        "model_id": "nv-segment-ct",
        "image": NEW["nv-segment-ct"],
        "requests": 54,
        "rows": rows,
        "identity_sha256": sha(directory / "identity.json"),
        "scope": "Isolated H100. Expert-mask-derived points; research-only, not independent clinical validation.",
    }


def evo2_evidence(directory):
    report = read(directory / "v2-final-report.json")
    health = read(directory / "v2-health-observations.json")
    concurrency = read(directory / "v2-concurrency.json")
    pod = read(directory / "v2-pod-final.json")
    if (
        report["isolated_runtime_qualified"] is not True
        or report["requests"] != 96
        or report["passed"] != 96
        or not health["replay_completed"]
        or health["failed"] != 0
        or health["observations"] < 100
        or health["health_latency_max_seconds"] >= 3
        or not concurrency["passed"]
    ):
        raise ValueError("Evo2 isolated acceptance incomplete or failed")
    if {c["image"] for c in pod["spec"]["containers"]} != {NEW["evo2-40b"]}:
        raise ValueError("Evo2 evidence is not from exact v2 image")
    baseline_memory = directory / "v2-server-evidence"
    final_memory = directory / "v2-server-evidence-with-concurrency"
    before = sorted(baseline_memory.glob("http-memory-*.json"))
    after = sorted(final_memory.glob("http-memory-*.json"))
    if len(before) != 96 or len(after) != 98:
        raise ValueError("Evo2 concurrency did not execute exactly two additional model calls")
    for previous in before:
        if sha(previous) != sha(final_memory / previous.name):
            raise ValueError("Evo2 earlier server receipt changed")
    calls = [read(path) for path in after]
    ordered = sorted(calls, key=lambda row: row["started_unix_seconds"])
    if any(row["active_model_requests_at_start"] != 1 for row in calls) or any(
        a["finished_unix_seconds"] > b["started_unix_seconds"] for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("Evo2 GPU generations overlapped")
    files = ["v2-final-report.json", "v2-health-observations.json", "v2-concurrency.json", "v2-pod-final.json"]
    return {
        "model_id": "evo2-40b",
        "image": NEW["evo2-40b"],
        "requests": 98,
        "file_sha256": {name: sha(directory / name) for name in files},
        "serialized_memory_sha256": {path.name: sha(path) for path in after},
        "scope": "Isolated two-H100 plant DNA continuation; no public-path or cold/snapshot qualification.",
    }


def prepare(args):
    baseline = args.baseline
    capture = read(baseline / "capture.json")
    if capture["release"]["version"] != args.expected_release or capture["release"]["status"] != "deployed":
        raise ValueError("capture does not match the explicitly selected deployed release")
    for name, digest in capture["sha256"].items():
        if sha(baseline / name) != digest:
            raise ValueError("baseline capture changed:" + name)
    pins = [source_pin(ROOT / "models/general-media/nv_segment_ct_server.py", SOURCE["nv-segment-ct"])]
    for name in ("evo2_h100.py", "evo2_prefill.py", "evo2_serve.py", "Dockerfile.evo2-h100-adapter"):
        pins.append(source_pin(ROOT / "models/general-media" / name, SOURCE["evo2-40b"]))
    pins.append(source_pin(Path(__file__), args.source_commit))
    evidence = {"nv-segment-ct": ct_evidence(args.ct_evidence), "evo2-40b": evo2_evidence(args.evo2_evidence)}
    maps = read(baseline / "live-configmaps.json")["items"]

    def document(key):
        return json.loads(next(item["data"][key] for item in maps if key in item.get("data", {})))

    original, old_bundles = document("infrastructure-envelope.json"), document("renderer-bundles.json")
    owners = read(baseline / "modeldeployments.json")["items"]
    routes_map, admin_map = read(baseline / "live-routes.json"), read(baseline / "live-admin-configuration.json")
    routes, old_admin = routes_map["data"], json.loads(admin_map["data"]["admin-configuration.json"])
    runtimes = json.loads(routes["deployment-runtimes.json"])["models"]
    projection = json.loads(routes["qualification-projection.json"])["rows"]
    successors = {}
    for model_id in NEW:
        base = runtimes[model_id]["record"] if model_id in runtimes else catalog().model(model_id).to_dict()
        row = next(item for item in projection if item["model_id"] == model_id)
        successors[model_id] = successor(base, row, model_id, evidence_sha256(evidence[model_id]), SOURCE[model_id])
    candidate, bundles, new_routes, admin, proposals = extend(
        original, old_bundles, routes, old_admin, owners, successors
    )
    checks = existing.validate_candidate(original, candidate, bundles, owners, proposals)
    checks["gateway_registry"] = validate_registry(new_routes, read(args.serving_bindings))
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
            if o["metadata"]["name"] in NEW
        ],
        "successor-runtime-entries.json": successors,
        "isolated-evidence.json": evidence,
        "validation.json": {
            "applied": False,
            "source_pins": pins,
            "baseline": capture,
            "validation": checks,
            "preserved_sibling_models": 18,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "ct-evidence", "evo2-evidence", "serving-bindings", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--expected-release", required=True, type=int)
    print(json.dumps(prepare(parser.parse_args())))


if __name__ == "__main__":
    main()

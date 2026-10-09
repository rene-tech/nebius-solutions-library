"""Prepare four immutable maps for the qualified wrapper; never change live state."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import prepare_candidate as candidate

ROOT = candidate.ROOT
IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/diffdock"
    "@sha256:0c717984c438bb3cac1a139297a06dc39c5fe7fc6ab387c130c0c464a48ba4f9"
)
SOURCE = "7e8c0f0f830b071505c53756aa6644a4f36dda6d"


def load_helper(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


existing = load_helper(
    "open_http_previous_promotion", "acceptance/scientific-runtime-repair-20260918/prepare_promotion.py"
)
registry = load_helper("open_http_registry_validation", "acceptance/ct-evo2-runtime-repair-20260918/prepare.py")


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evidence(directory):
    receipt, pod = read(directory / "results/receipt.json"), read(directory / "results/pod.json")
    preparation = read(directory / "preparation.json")
    runs = receipt["runs"]
    source = subprocess.check_output(  # noqa: S603 - fixed source revision and read-only git command.
        ["git", "-C", str(ROOT), "show", SOURCE + ":k8s-inference/models/structure/runtime/common/server.py"]  # noqa: S607
    )
    if (
        receipt["schema"] != "fs2-diffdock-wrapper-http-regression/v1"
        or receipt["passed"] is not True
        or receipt["requests"] != 12
        or len(runs) != 12
        or len({r["case_id"] for r in runs}) != 12
        or receipt["coordinate_tolerance_angstrom"] != 0.01
        or receipt["confidence_tolerance"] != 0.001
        or receipt["wrapper_sha256"] != hashlib.sha256(source).hexdigest()
        or preparation["image"] != IMAGE
        or receipt["cases_sha256"] != sha(directory / "cases.json")
        or receipt["cases_sha256"] != preparation["cases_sha256"]
        or receipt["references_sha256"] != preparation["reference_results_sha256"]
        or pod["status"]["phase"] != "Succeeded"
        or receipt["pod_uid"] != pod["metadata"]["uid"]
        or [c["image"] for c in pod["spec"]["containers"]] != [IMAGE]
        or [c["imageID"] for c in pod["status"]["containerStatuses"]] != [IMAGE]
        or any(c["restartCount"] or c["state"]["terminated"]["exitCode"] for c in pod["status"]["containerStatuses"])
    ):
        raise ValueError("incomplete or mismatched exact-image evidence")
    poses = 0
    for run in runs:
        path = directory / "results" / run["result_file"]
        if path.parent != directory / "results" or not path.name.endswith(".json"):
            raise ValueError("unexpected retained result path")
        headers = run["identity_headers"]
        if (
            run["http_status"] != 200
            or run["numerical_repeatability_pass"] is not True
            or run["maximum_coordinate_difference_angstrom"] > 0.01
            or run["maximum_confidence_difference"] > 0.001
            or headers["X-FS2-Runtime-Pod-Uid"] != receipt["pod_uid"]
            or headers["X-FS2-Runtime-Attempt"] != "1"
            or not headers["X-FS2-Runtime-Operation-Id"]
            or sha(path) != run["result_sha256"]
        ):
            raise ValueError("failed HTTP, attribution or numerical case:" + run["case_id"])
        result = read(path)
        if len(result["ligand_positions"]) != 4 or any(
            "3D" not in sdf.splitlines()[1] for sdf in result["ligand_positions"]
        ):
            raise ValueError("missing four three-dimensional pose artifacts")
        poses += len(result["ligand_positions"])
    files = [
        "preparation.json",
        "cases.json",
        "launch-recovery.json",
        "results/receipt.json",
        "results/pod.json",
        "results/runtime.log",
        "results/events.json",
    ]
    return {
        "schema": "fs2-diffdock-http-wrapper-qualification/v1",
        "model_id": "diffdock",
        "image": IMAGE,
        "source_commit": SOURCE,
        "requests": 12,
        "poses": poses,
        "passed": True,
        "gpu": receipt["gpu"],
        "pod_uid": receipt["pod_uid"],
        "node": pod["spec"]["nodeName"],
        "started_at": pod["status"]["containerStatuses"][0]["state"]["terminated"]["startedAt"],
        "completed_at": pod["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"],
        "receipt": receipt,
        "file_sha256": {name: sha(directory / name) for name in files},
        "limitations": [
            "One ordered actual-HTTP pass on one H100 against frozen r6 first-pass outputs; "
            "not all-input byte identity.",
            "Isolated wrapper identity is proven; public multi-replica attribution, cold start, "
            "scaling and snapshots remain pending.",
            "Unchanged weights and model logic retain variable docking accuracy, warnings and "
            "the earlier distant low-confidence pose.",
            "Client-side ConfigMap apply exceeded the annotation limit; the exact missing ConfigMap "
            "was created without recreating the Pod. This launch failure remains retained.",
        ],
    }


def successor(previous, proof):
    entry = copy.deepcopy(previous)
    if entry["model_id"] != "diffdock" or entry["record"]["runtime"]["image"]["digest"] != candidate.BASE_DIGEST:
        raise ValueError("unexpected previous runtime record")
    record, qualification = entry["record"], entry["qualification"]
    record["runtime"]["image"].update(reference=IMAGE, digest=IMAGE.split("@")[1])
    record["runtime"]["version"] = "portable-h100-http-identity-20260919"
    proof_sha = hashlib.sha256(existing.canonical_json(proof)).hexdigest()
    record["evidence"].append(
        {
            "classification": "measured-platform",
            "hardware": proof["gpu"],
            "outcome": "live-qualified",
            "source_commit": SOURCE,
            "summary": "12 exact-image HTTP requests,48 generated 3D poses, all response identity headers passed. "
            "One matched-order pass equals frozen r6 reference coordinates/confidences; "
            "no general byte-identity claim. "
            "Wrapper-only derivative preserves weights and seeded model logic. "
            "Public multi-replica attribution remains pending. "
            "Portable evidence SHA256 " + proof_sha + ".",
        }
    )
    record["support"]["limitations"].append(
        "The HTTP-identity wrapper successor has isolated exact-image evidence only; "
        "no inherited public or snapshot qualification."
    )
    qualification["active_runtime"]["runtime_image_digest"] = IMAGE.split("@")[1]
    qualification["states"] = {
        k: k in {"registered", "runtime_ready", "semantic_qualified"} for k in qualification["states"]
    }
    qualification["evidence"] = {
        k: v if k == "audited_catalog_sha256" else None for k, v in qualification["evidence"].items()
    }
    qualification["evidence"]["retained_deployments_sha256"] = proof_sha
    return entry


def extend(envelope, bundles, routes, admin, owners, entry):
    old_entries = json.loads(routes["deployment-runtimes.json"])["models"]
    current = [o for o in owners if o["spec"]["modelRef"] == "diffdock"]
    if len(current) != 1:
        raise ValueError("exactly one DiffDock owner required")
    owner = current[0]
    old_template = owner["spec"]["runtime"]["templateRef"]["digest"]
    selected = [b for b in bundles if b["modelRef"] == "diffdock" and b["templateDigest"] == old_template]
    if len(selected) != 1:
        raise ValueError("exact owner template required")
    new_template = candidate.candidate_template(selected[0], IMAGE, owner["spec"]["artifact"]["revision"])
    result, new_bundles, new_routes, proposals = existing.extend(
        envelope,
        bundles,
        routes,
        owners,
        {"diffdock": entry},
        old_images={"diffdock": candidate.BASE_DIGEST.removeprefix("sha256:")},
        new_images={"diffdock": IMAGE.split("@sha256:")[1]},
    )
    new_bundles.append(new_template)
    qualification = result["qualifications"]["diffdock"]
    qualification["templateDigests"].append(new_template["templateDigest"])
    qualification["templateRefs"][candidate.TEMPLATE_NAME] = new_template["templateDigest"]
    qualification["templateCacheTiers"][new_template["templateDigest"]] = qualification["templateCacheTiers"][
        old_template
    ]
    proposals[0]["spec"] = candidate.proposal(owner["spec"], selected[0], new_template, IMAGE)
    result.pop("revision")
    result["revision"] = existing.canonical_digest(result)
    new_admin = existing.rebase_admin_configuration(admin, old_entries, {"diffdock": entry})
    if new_bundles[:-1] != bundles or new_routes["lean-routes.json"] != routes["lean-routes.json"]:
        raise ValueError("historical templates or service routes changed")
    for model in set(old_entries) - {"diffdock"}:
        if json.loads(new_routes["deployment-runtimes.json"])["models"][model] != old_entries[model]:
            raise ValueError("sibling runtime changed:" + model)
        if new_admin["models"].get(model) != admin["models"].get(model):
            raise ValueError("sibling admin configuration changed:" + model)
    return result, new_bundles, new_routes, new_admin, proposals


def merge_values(before, delta):
    result = copy.deepcopy(before)
    for key, value in delta.items():
        result[key] = merge_values(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def prepare(baseline, isolated):
    maps = read(baseline / "configmaps.json")["items"]

    def one(key):
        found = [m for m in maps if key in m.get("data", {})]
        if len(found) != 1:
            raise ValueError("capture exact mounted map:" + key)
        return found[0]

    envelope = json.loads(one("infrastructure-envelope.json")["data"]["infrastructure-envelope.json"])
    bundles = json.loads(one("renderer-bundles.json")["data"]["renderer-bundles.json"])
    routes = one("deployment-runtimes.json")["data"]
    admin = json.loads(one("admin-configuration.json")["data"]["admin-configuration.json"])
    owners = read(baseline / "modeldeployments.json")["items"]
    revision = read(baseline / "diffdock-owner.json")
    owner = next(o for o in owners if o["spec"]["modelRef"] == "diffdock")
    if candidate.ModelDeploymentSpec.model_validate(owner["spec"]) != candidate.ModelDeploymentSpec.model_validate(
        revision["spec"]
    ) or not revision.get("etag"):
        raise ValueError("captured Kubernetes and admin owner revisions disagree")
    proof = evidence(isolated)
    entry = successor(json.loads(routes["deployment-runtimes.json"])["models"]["diffdock"], proof)
    new_envelope, new_bundles, new_routes, new_admin, proposals = extend(
        envelope, bundles, routes, admin, owners, entry
    )
    checks = existing.validate_candidate(envelope, new_envelope, new_bundles, owners, proposals)
    checks["gateway_bootstrap"] = asyncio.run(
        existing.validate_admin_configuration(new_admin, json.loads(new_routes["deployment-runtimes.json"])["models"])
    )
    checks["registry"] = registry.validate_registry(new_routes, one("serving-bindings.json"))
    objects = [
        existing.configmap(
            "fs2-science-envelope-", {"infrastructure-envelope.json": existing.canonical_json(new_envelope).decode()}
        ),
        existing.configmap(
            "fs2-science-bundles-", {"renderer-bundles.json": existing.canonical_json(new_bundles).decode()}
        ),
        existing.configmap("fs2-science-routes-", new_routes),
        existing.configmap(
            "fs2-science-admin-", {"admin-configuration.json": existing.canonical_json(new_admin).decode()}
        ),
    ]
    delta = {
        "modelController": {
            "infrastructureEnvelopeConfigMapName": objects[0]["metadata"]["name"],
            "rendererBundlesConfigMapName": objects[1]["metadata"]["name"],
        },
        "catalog": {"leanRoutes": {"configMapName": objects[2]["metadata"]["name"]}},
        "adminConfiguration": {
            "configMapName": objects[3]["metadata"]["name"],
            "sha256": hashlib.sha256(existing.canonical_json(new_admin)).hexdigest(),
        },
    }
    values = read(baseline / "values.json")
    # Verify every reference against the capture, not merely a similarly named map.
    for actual, expected in [
        (
            values["modelController"]["infrastructureEnvelopeConfigMapName"],
            one("infrastructure-envelope.json")["metadata"]["name"],
        ),
        (values["modelController"]["rendererBundlesConfigMapName"], one("renderer-bundles.json")["metadata"]["name"]),
        (values["catalog"]["leanRoutes"]["configMapName"], one("lean-routes.json")["metadata"]["name"]),
        (values["adminConfiguration"]["configMapName"], one("admin-configuration.json")["metadata"]["name"]),
    ]:
        if actual != expected:
            raise ValueError("Helm and mounted-map references disagree")
    return {
        "configmaps.json": {"apiVersion": "v1", "kind": "List", "items": objects},
        "values.json": delta,
        "full-values.json": merge_values(values, delta),
        "rollback-values.json": values,
        "app-proposals.json": proposals,
        "rollback-app-proposals.json": [
            {"name": owner["metadata"]["name"], "namespace": owner["metadata"]["namespace"], "spec": owner["spec"]}
        ],
        "captured-admin-owner.json": revision,
        "successor-runtime-entries.json": {"diffdock": entry},
        "isolated-qualification.json": proof,
        "validation.json": {
            "applied": False,
            "public_qualified": False,
            "validation": checks,
            "preserved_sibling_models": len(envelope["qualifications"]) - 1,
            "all_prior_bundles_preserved": True,
            "snapshot_qualified": False,
            "baseline_sha256": {
                name: sha(baseline / name)
                for name in (
                    "configmaps.json",
                    "values.json",
                    "modeldeployments.json",
                    "diffdock-owner.json",
                    "capture-receipt.json",
                )
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--isolated", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("new output directory required; preserve prior candidates")
    outputs = prepare(args.baseline, args.isolated)
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    for name, value in outputs.items():
        (args.output / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {"applied": False, "public_qualified": False, "output": str(args.output), "values": outputs["values.json"]}
        )
    )


if __name__ == "__main__":
    main()

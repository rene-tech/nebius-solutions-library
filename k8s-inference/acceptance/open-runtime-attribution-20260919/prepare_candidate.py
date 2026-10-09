"""Prepare an immutable DiffDock HTTP-attribution candidate; never apply."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

from fs2_serve.model_deployment import LegacyTemplateBundle, ModelDeploymentSpec

ROOT = Path(__file__).resolve().parents[2]
MODEL = "diffdock"
ANNOTATION = "telemetry.fs2.nebius.ai/response-identity"
VERSION = "http-open-runtime-v1"
BASE_DIGEST = "sha256:9766b4fb2a22787874bd8d90306980a4cb1ff9808941f0f1ae429d8b8f7cc948"
TEMPLATE_BASE_DIGEST = "sha256:471db264f4c544e1798c090f13b6cf94b3fbd304fb2c91e9f63a3dd33c9d81d7"
COMMAND = ["python3", "/opt/fs2/runtime/common/server.py"]
TEMPLATE_NAME = "diffdock.http-response-identity-20260919"
REVISION_TEMPLATE_NAME = "diffdock.http-response-identity-revision-20260919"
UID_ENV = {"name": "FS2_RUNTIME_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}
PIN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def terraform_digest(resources):
    path = ROOT / "acceptance/cosmos-stockholm-deployment-20260917/prepare_contract.py"
    loader = importlib.util.spec_from_file_location("open_http_existing_digest", path)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module.terraform_digest(resources)


def _bind_revision(deployment, model_revision):
    if not isinstance(model_revision, str) or not model_revision.strip():
        raise ValueError("exact owner model revision required")
    for metadata in (deployment["metadata"], deployment["spec"]["template"]["metadata"]):
        annotations = metadata.setdefault("annotations", {})
        current = annotations.get("fs2.nebius/model-revision")
        if current is not None and current != model_revision:
            raise ValueError("template revision conflicts with exact owner")
        annotations["fs2.nebius/model-revision"] = model_revision


def candidate_template(previous, image, model_revision):
    if not PIN.fullmatch(image) or image.endswith("@" + BASE_DIGEST):
        raise ValueError("new immutable wrapper image required")
    if previous["modelRef"] != MODEL or previous["runtimeContainerName"] != "runtime":
        raise ValueError("unexpected DiffDock template")
    candidate = copy.deepcopy(previous)
    deployments = [resource for resource in candidate["resources"] if resource["kind"] == "Deployment"]
    if len(deployments) != 1:
        raise ValueError("expected one canonical Deployment template")
    deployment = deployments[0]
    pod = deployment["spec"]["template"]
    containers = [c for c in pod["spec"]["containers"] if c["name"] == "runtime"]
    if len(containers) != 1:
        raise ValueError("expected exact GPU runtime container")
    runtime = containers[0]
    if (
        not runtime["image"].endswith(("@" + BASE_DIGEST, "@" + TEMPLATE_BASE_DIGEST))
        or runtime.get("command", COMMAND) != COMMAND
        or runtime.get("args")
    ):
        raise ValueError("unreviewed base image or entrypoint")
    annotations = pod["metadata"].setdefault("annotations", {})
    if ANNOTATION in annotations or any(e["name"] == "FS2_RUNTIME_POD_UID" for e in runtime["env"]):
        raise ValueError("existing identity instrumentation requires review")
    ports = [e.get("value") for e in runtime["env"] if e["name"] == "FS2_PORT"]
    if ports != [str(previous["primaryServicePort"])]:
        raise ValueError("listener/Service port mismatch")
    annotations[ANNOTATION] = VERSION
    for metadata in (deployment["metadata"], pod["metadata"]):
        metadata.setdefault("annotations", {})["fs2.nebius/runtime-image-digest"] = image.rsplit("@", 1)[1]
    runtime["image"] = image
    runtime["command"] = list(COMMAND)
    runtime["env"].append(copy.deepcopy(UID_ENV))
    _bind_revision(deployment, model_revision)
    candidate["templateDigest"] = terraform_digest(candidate["resources"])
    LegacyTemplateBundle.model_validate(candidate)
    return candidate


def complete_revision_template(previous, owner):
    """Repair only absent revision metadata in an already instrumented template."""
    if (
        owner["modelRef"] != MODEL
        or owner["runtime"]["templateRef"]["digest"] != previous["templateDigest"]
        or previous["modelRef"] != MODEL
        or previous["runtimeContainerName"] != "runtime"
    ):
        raise ValueError("exact instrumented owner/template required")
    result = copy.deepcopy(previous)
    deployments = [r for r in result["resources"] if r["kind"] == "Deployment"]
    if len(deployments) != 1:
        raise ValueError("one existing Deployment template required")
    deployment = deployments[0]
    pod = deployment["spec"]["template"]
    [runtime] = [c for c in pod["spec"]["containers"] if c["name"] == "runtime"]
    if (
        runtime["image"] != owner["runtime"]["image"]
        or runtime.get("command") != COMMAND
        or runtime.get("args")
        or pod["metadata"]["annotations"].get(ANNOTATION) != VERSION
        or [e for e in runtime["env"] if e["name"] == "FS2_RUNTIME_POD_UID"] != [UID_ENV]
        or [e.get("value") for e in runtime["env"] if e["name"] == "FS2_PORT"] != [str(previous["primaryServicePort"])]
    ):
        raise ValueError("existing response-identity template differs from qualified wrapper")
    _bind_revision(deployment, owner["artifact"]["revision"])
    result["templateDigest"] = terraform_digest(result["resources"])
    if result["templateDigest"] == previous["templateDigest"]:
        raise ValueError("revision metadata already present; no repair needed")
    LegacyTemplateBundle.model_validate(result)
    return result


def proposal(original, previous, candidate, image):
    if (
        original["modelRef"] != MODEL
        or not original["runtime"]["image"].endswith("@" + BASE_DIGEST)
        or original["runtime"]["templateRef"]["digest"] != previous["templateDigest"]
    ):
        raise ValueError("owner no longer matches the reviewed baseline")
    if original["fastStart"]["level"] != "Off":
        raise ValueError("new image has no inherited fast-start qualification")
    result = copy.deepcopy(original)
    result["runtime"].update(image=image, templateRef={"name": TEMPLATE_NAME, "digest": candidate["templateDigest"]})
    result["cache"]["snapshotPreference"] = "Never"
    result["cache"].pop("snapshotRef", None)
    return result


def prepare(configmaps, owner, image):
    ModelDeploymentSpec.model_validate(owner["spec"])
    if "metadata" in owner:
        owner_metadata = owner["metadata"]
    elif owner.get("etag") and owner.get("revision"):
        # Admin desired state is also an authoritative captured owner. Keep
        # its complete revision/ETag in original rather than reconstructing it.
        owner_metadata = owner
    else:
        raise ValueError("expected captured ModelDeployment or admin desired revision")
    sets = [
        json.loads(c["data"]["renderer-bundles.json"])
        for c in configmaps["items"]
        if "renderer-bundles.json" in c.get("data", {})
    ]
    if len(sets) != 1:
        raise ValueError("capture only the exact mounted renderer map")
    templates = [
        b
        for b in sets[0]
        if b["modelRef"] == MODEL and b["templateDigest"] == owner["spec"]["runtime"]["templateRef"]["digest"]
    ]
    if len(templates) != 1:
        raise ValueError("exact current owner template not found")
    previous = templates[0]
    candidate = candidate_template(previous, image, owner["spec"]["artifact"]["revision"])
    proposed = proposal(owner["spec"], previous, candidate, image)
    ModelDeploymentSpec.model_validate(proposed)
    return {
        "schema": "fs2.open-http-attribution-candidate/v1",
        "applied": False,
        "qualified": False,
        "original": owner,
        "template": candidate,
        "proposal": {"name": owner_metadata["name"], "namespace": owner_metadata["namespace"], "spec": proposed},
        "required_gates": [
            "exact wrapper image build",
            "real-GPU numerical regression",
            "public two-replica response attribution",
            "four-map/registry bootstrap validation",
            "serialized owner promotion",
            "sibling regression",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configmaps", type=Path, required=True)
    parser.add_argument("--owner", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a new output directory; prior candidates are immutable")
    result = prepare(json.loads(args.configmaps.read_bytes()), json.loads(args.owner.read_bytes()), args.image)
    result["sources"] = [
        {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in (
            args.configmaps,
            args.owner,
            Path(__file__),
            ROOT / "models/structure/runtime/common/server.py",
            ROOT / "models/structure/runtime/common/Dockerfile.response-identity",
        )
    ]
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    (args.output / "candidate.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"applied": False, "qualified": False, "template_digest": result["template"]["templateDigest"]}))


if __name__ == "__main__":
    main()

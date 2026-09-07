#!/usr/bin/env python3
"""Run each original Bio/structure semantic pair over public HTTP and MCP.

Transport and durable operation checks reuse the medical/media harness.
Model inputs and validation remain the exact digest-bound qualification code.
Different models run concurrently; each model keeps its original pair order.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[3]
MEDIA = ROOT / "acceptance/h100-fleet/medical-media"
sys.path.insert(0, str(MEDIA))


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


public = load_module(MEDIA / "public_verify.py", "fs2_bio_public_transport")
MODELS = ("molmim", "genmol", "proteinmpnn", "diffdock", "boltz2", "msa-search-pdb70")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
NATIVE_VALIDATORS = {
    "proteinmpnn": (
        "proteinmpnn-native/validate_proteinmpnn.py",
        "2e3c21af0987f4b9c7da2cef3f3e4d210a7b223049f231c24e871e2a553b48d3",
        "proteinmpnn-faststart-semantic-v1",
    ),
    "diffdock": (
        "diffdock-native/validate_diffdock.py",
        "245ae98a98db09c34924cd7a499b99da9eb35742667043aaee3e497c33268008",
        "diffdock-faststart-semantic-v1",
    ),
}


@lru_cache
def entry(model):
    matches = [json.loads(path.read_text()) for path in (ROOT / "catalog/runtime/deployment-runtimes").glob("*.json")]
    return next(item for item in matches if item["model_id"] == model)


@lru_cache
def validator(model):
    binding = entry(model)["record"]["semantic_validator"]
    path = ROOT.parent / binding["source_path"]
    if not path.exists():
        path = ROOT / "catalog/runtime/packaged-repository" / binding["source_path"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != binding["source_sha256"]:
        raise ValueError("validator bytes differ from the qualified runtime")
    return load_module(path, "fs2_bio_public_validator_" + model)


@lru_cache
def native_validator(model):
    """Load the frozen validator for the native public response contract."""
    relative, expected_sha256, _contract = NATIVE_VALIDATORS[model]
    path = (
        ROOT
        / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2"
        / relative
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("native public validator bytes differ from the pinned contract")
    return load_module(path, "fs2_bio_native_public_validator_" + model)


def native_validator_contract(model):
    relative, source_sha256, contract = NATIVE_VALIDATORS[model]
    return {
        "kind": "repository-native-response-validator",
        "contract": contract,
        "source_path": (
            "k8s-inference/catalog/runtime/packaged-repository/"
            "nim-fast-start/faststart-v2/" + relative
        ),
        "source_sha256": source_sha256,
        "request_count": 2,
        "distinct_requests": True,
        "distinct_responses": True,
    }


@lru_cache
def expected_runtime(model):
    """Bind source revision, image and weights through the selected manifest."""
    candidate = entry(model)
    record = candidate["record"]
    profiles = json.loads((ROOT / "catalog/profiles/model-profiles.json").read_text())
    manifest_paths = profiles["model_artifacts"][model]["manifest_paths"]
    deployments = []
    for relative in manifest_paths:
        documents = yaml.safe_load_all((ROOT / relative).read_text())
        deployments.extend(item for item in documents if item and item.get("kind") == "Deployment")
    if len(deployments) != 1:
        raise ValueError("selected Bio runtime must resolve to exactly one Deployment")
    deployment = deployments[0]
    metadata_annotations = deployment["metadata"].get("annotations", {})
    pod_annotations = deployment["spec"]["template"]["metadata"].get("annotations", {})
    annotations = {**metadata_annotations, **pod_annotations}
    image = record["runtime"]["image"]["reference"]
    image_digest = record["runtime"]["image"]["digest"]
    content_digest = annotations.get("fs2.nebius/model-content-digest")
    if (
        not SHA256.fullmatch(content_digest or "")
        or image.split("@", 1)[1] != image_digest
        or annotations.get("fs2.nebius/runtime-image-digest") != image_digest
    ):
        raise ValueError("selected Bio manifest does not bind exact image and model content")
    revision = record["model"]["source"]["revision"]
    revision_values = {
        value
        for source in (metadata_annotations, pod_annotations)
        for key, value in source.items()
        if key.endswith("revision")
    }
    if revision not in revision_values:
        raise ValueError("selected Bio manifest does not bind the exact model revision")
    return {
        "image": image,
        "model_revision": revision,
        "model_content_digest": content_digest,
        "manifest_paths": manifest_paths,
    }


def runtime_binding(pods, operation, expected):
    """Bind a dynamic operation to exact selected code, weights, and route."""
    pod_uid = operation.get("runtime", {}).get("pod_uid")
    matches = [pod for pod in pods if pod["metadata"]["uid"] == pod_uid]
    if len(matches) != 1:
        raise public.AcceptanceError("runtime_pod_identity_missing")
    pod = matches[0]
    metadata = pod["metadata"]
    annotations = metadata.get("annotations", {})
    digest = expected["image"].split("@", 1)[1]
    image_ids = [
        item.get("imageID", "")
        for item in pod["status"].get("containerStatuses", [])
    ]
    route_revision = "dynamic:" + annotations.get(
        "fs2-serve.nebius.ai/spec-digest", ""
    )
    if (
        annotations.get("fs2.nebius/runtime-image-digest") != digest
        or annotations.get("fs2.nebius/model-content-digest")
        != expected["model_content_digest"]
        or not any(image.endswith("@" + digest) for image in image_ids)
        or operation.get("model_revision") != route_revision
    ):
        raise public.AcceptanceError("runtime_image_or_revision_mismatch")
    return {
        "namespace": metadata["namespace"],
        "pod": metadata["name"],
        "pod_uid": pod_uid,
        "model_revision": expected["model_revision"],
        "model_content_digest": expected["model_content_digest"],
        "route_revision": route_revision,
        "runtime_image_ids": image_ids,
        "expected_image": expected["image"],
        "source_revision_binding": "selected-manifest-plus-exact-image-and-model-content",
    }


def cases_for(model):
    record = entry(model)["record"]
    module = native_validator(model) if model in NATIVE_VALIDATORS else validator(model)
    if model == "msa-search-pdb70":
        fixture = ROOT / "catalog/runtime/packaged-repository" / record["semantic_validator"]["fixture_path"]
        template = module._read_fixture(fixture)
        payloads = [module._request_for_case(template, query) for query in (module.QUERY_1, module.QUERY_2)]
    elif model == "proteinmpnn":
        fixture = (
            ROOT
            / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/"
            "proteinmpnn-native/fixtures/1ubq-request.json"
        )
        template = module._read_fixture(fixture)
        payloads = [
            {**template, "random_seed": seed}
            for seed in (2370, 2371)
        ]
    elif model == "diffdock":
        fixture = (
            ROOT
            / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/"
            "diffdock-native/fixtures/1ubq-aspirin-request.json"
        )
        template = module._read_fixture(fixture)
        payloads = [
            {**template, "random_seed": seed}
            for seed in (2370, 2371)
        ]
    elif model == "boltz2":
        payloads = [probe.payload for probe in module.build_probes(("bio-public-a", "bio-public-b"))]
    else:
        fixture = ROOT / "catalog/runtime/packaged-repository" / record["semantic_validator"]["fixture_path"]
        payloads = [case["payload"] for case in module._read_fixture(fixture)]
    revision = record["model"]["source"]["revision"]
    operation = record["interface"]["policy"]["operations"][0]
    cases = [public.AcceptanceCase(model, revision, "native", operation, payload,
        hashlib.sha256(public.canonical_json(payload)).hexdigest(), "json-object",
        gpu_required=model != "msa-search-pdb70") for payload in payloads]
    if len(cases) != 2 or len({case.payload_sha256 for case in cases}) != 2:
        raise ValueError("original two distinct requests are required")
    contract = (
        native_validator_contract(model)
        if model in NATIVE_VALIDATORS
        else record["semantic_validator"]
    )
    return contract, cases


def validate_pair(model, contract, paths, directory):
    del directory
    module = native_validator(model) if model in NATIVE_VALIDATORS else validator(model)
    responses = [json.loads(path.read_bytes()) for path in paths]
    if len(responses) != 2 or paths[0].read_bytes() == paths[1].read_bytes():
        raise ValueError("two distinct responses are required")
    if model == "msa-search-pdb70":
        results = [module._validate_response(response, query)
            for response, query in zip(responses, (module.QUERY_1, module.QUERY_2), strict=True)]
    elif model == "proteinmpnn":
        results = [
            module._validate_response(response, seed)
            for response, seed in zip(responses, (2370, 2371), strict=True)
        ]
    elif model == "diffdock":
        fixture = (
            ROOT
            / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2/"
            "diffdock-native/fixtures/1ubq-aspirin-request.json"
        )
        template = module._read_fixture(fixture)
        results = [module._validate_response(response, template) for response in responses]
    elif model == "boltz2":
        probes = module.build_probes(("bio-public-a", "bio-public-b"))
        results = [module.validate_response(response, probe.sequence, probe.chain_id)
            for response, probe in zip(responses, probes, strict=True)]
    elif model == "genmol":
        results = [module._validate_response(response, scoring)
            for response, scoring in zip(responses, ("QED", "LogP"), strict=True)]
    else:
        results = [module._validate_response(response) for response in responses]
        if results[0]["smiles"] == results[1]["smiles"]:
            raise ValueError("MolMIM returned identical candidates for distinct seeds")
    return {"status": "PASS", "validator": contract, "results": results}


def actual_runtime(args, operation, model):
    pods = json.loads(subprocess.check_output([
        "kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
        "-n", "fs2-models", "get", "pods", "-o", "json",
    ]))
    candidate = entry(model)
    if model == "msa-search-pdb70":
        expected = {
            "image": candidate["record"]["runtime"]["image"]["reference"],
            "model_revision": candidate["record"]["model"]["source"]["revision"],
        }
        # This exact CPU lane is Terraform-owned rather than a dynamic GPU
        # publication. Its code and embedded database are bound by the image.
        service = candidate["qualification"]["active_runtime"]["service"]
        slices = json.loads(subprocess.check_output([
            "kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
            "-n", service["namespace"], "get", "endpointslices",
            "-l", "kubernetes.io/service-name=" + service["name"], "-o", "json",
        ]))
        ready = [endpoint for item in slices["items"] for endpoint in item["endpoints"]
            if endpoint.get("conditions", {}).get("ready") is True]
        if len(ready) != 1 or ready[0].get("targetRef", {}).get("kind") != "Pod":
            raise public.AcceptanceError("cpu_runtime_endpoint_ambiguous")
        uid = ready[0]["targetRef"]["uid"]
        matches = [pod for pod in pods["items"] if pod["metadata"]["uid"] == uid]
        if len(matches) != 1 or operation.get("model_revision") != expected["model_revision"]:
            raise public.AcceptanceError("cpu_runtime_pod_or_revision_mismatch")
        pod = matches[0]
        digest = expected["image"].split("@", 1)[1]
        image_ids = [container.get("imageID", "") for container in pod["status"].get("containerStatuses", [])]
        if (pod["metadata"].get("annotations", {}).get("fs2.nebius/runtime-image-digest") != digest
                or not any(image.endswith("@" + digest) for image in image_ids)):
            raise public.AcceptanceError("cpu_runtime_image_mismatch")
        return {"binding_basis": "single-ready-service-endpoint-not-per-operation-GPU-attribution",
            "namespace": pod["metadata"]["namespace"], "pod": pod["metadata"]["name"],
            "pod_uid": uid, "model_revision": expected["model_revision"],
            "route_revision": operation["model_revision"], "runtime_image_ids": image_ids,
            "expected_image": expected["image"], "gpu_count": 0}
    expected = expected_runtime(model)
    return runtime_binding(pods["items"], operation, expected)


async def main(args):
    async def verify_model(model):
        local = copy.copy(args)
        local.models = [model]
        local.output = args.output / model
        return await public.verify(local, case_factory=cases_for, runtime_resolver=actual_runtime,
            pair_validator=validate_pair, report_schema="fs2-h100-bionemo-structure-public-verification/v1")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    results = await asyncio.gather(*(verify_model(model) for model in args.models))
    return all(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", default="k8s-inference-h100")
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(0 if asyncio.run(main(parser.parse_args())) else 1)

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


def cases_for(model):
    record = entry(model)["record"]
    module = validator(model)
    if model == "msa-search-pdb70":
        fixture = ROOT / "catalog/runtime/packaged-repository" / record["semantic_validator"]["fixture_path"]
        template = module._read_fixture(fixture)
        payloads = [module._request_for_case(template, query) for query in (module.QUERY_1, module.QUERY_2)]
    elif model in {"proteinmpnn", "diffdock"}:
        payloads = module._requests(model)
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
    return record["semantic_validator"], cases


def validate_pair(model, contract, paths, directory):
    del directory
    module = validator(model)
    responses = [json.loads(path.read_bytes()) for path in paths]
    if len(responses) != 2 or paths[0].read_bytes() == paths[1].read_bytes():
        raise ValueError("two distinct responses are required")
    if model == "msa-search-pdb70":
        results = [module._validate_response(response, query)
            for response, query in zip(responses, (module.QUERY_1, module.QUERY_2), strict=True)]
    elif model in {"proteinmpnn", "diffdock"}:
        if any(not response.get("backend_id") for response in responses):
            raise ValueError("response omits the original runtime backend identity")
        results = [module._validate(model, response) for response in responses]
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
    expected = {"image": candidate["record"]["runtime"]["image"]["reference"],
        "model_revision": candidate["record"]["model"]["source"]["revision"]}
    if model == "msa-search-pdb70":
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
    return public.runtime_binding(pods["items"], operation, expected)


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

#!/usr/bin/env python3
"""Two original semantic inputs per model over public HTTP and generic MCP.

Run only after the release owner confirms these routes are hot. Credentials
are read from the private bundle in memory, never passed as subprocess args.
SDXL's first case changes only the public response envelope to b64_json;
generation parameters and decoded H100 PNG oracles remain exact.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import dataclasses
import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "components/control-plane/src"), str(ROOT / "catalog/runtime")]
from fs2_serve.live_acceptance import (AcceptanceCase, AcceptanceError, AcceptanceRunner,
    Client, MCP_PROTOCOL_VERSION, _mcp_result, _safe_failure, canonical_json,
    streamable_http_client, utc_now, write_evidence)
from probe import module

DEFAULT_MODELS = ("sdxl", "nv-segment-ct", "nv-reason-cxr-3b")
MODELS = {"sdxl": ("native", "generate-image"), "nv-segment-ct": ("native", "segment-ct"),
          "nv-reason-cxr-3b": ("openai-chat", "analyze-image"), "evo2-40b": ("native", "generate-sequence")}
PNG_ORACLES = ((450575, "3c64ef106b879f166315d8d9ed82187bf3d4925888b7ac5453096914cc4bad04"),
               (517982, "4e99817ef37c5a36caca78add181ac73c7d6d4f0d77ef67ff06f12ee86106413"))
TERMINAL = {"succeeded", "failed", "cancelled", "preempted", "expired"}


def runtime_binding(pods, operation, expected):
    """Bind the public operation to its actual model image and weight revision."""
    pod_uid = operation.get("runtime", {}).get("pod_uid")
    matches = [pod for pod in pods if pod["metadata"]["uid"] == pod_uid]
    if len(matches) != 1:
        raise AcceptanceError("runtime_pod_identity_missing")
    pod = matches[0]
    metadata = pod["metadata"]
    annotations = metadata.get("annotations", {})
    digest = expected["image"].split("@", 1)[1]
    image_ids = [item.get("imageID", "") for item in pod["status"].get("containerStatuses", [])]
    route_revision = "dynamic:" + annotations.get("fs2-serve.nebius.ai/spec-digest", "")
    if (annotations.get("fs2.nebius/model-revision") != expected["model_revision"]
            or not any(image.endswith("@" + digest) for image in image_ids)
            or operation.get("model_revision") != route_revision):
        raise AcceptanceError("runtime_image_or_revision_mismatch")
    return {"namespace": metadata["namespace"], "pod": metadata["name"], "pod_uid": pod_uid,
        "model_revision": expected["model_revision"], "route_revision": route_revision,
        "runtime_image_ids": image_ids, "expected_image": expected["image"]}


def actual_runtime(args, operation, model):
    result = subprocess.run(["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
        "-n", "fs2-models", "get", "pods", "-o", "json"], capture_output=True, text=True, check=True)
    expected = json.loads(Path(__file__).with_name("integration.json").read_text())["models"][model]
    return runtime_binding(json.loads(result.stdout)["items"], operation, expected)


def evo_validator():
    name = "fs2_mm_original_evo_validator"
    if name not in sys.modules:
        path = Path(__file__).with_name("evo2_hopper_validate.py")
        spec = importlib.util.spec_from_file_location(name, path)
        validator = importlib.util.module_from_spec(spec)
        sys.modules[name] = validator
        spec.loader.exec_module(validator)
    return sys.modules[name]


def evo_cases(model_revision):
    probes = evo_validator().build_probes(("fs2-mm-public-evo-a", "fs2-mm-public-evo-b"))
    return {"contract": "two-pinned-deterministic-DNA20-sequences"}, [
        AcceptanceCase("evo2-40b", model_revision, "native", "generate-sequence", probe.payload,
            hashlib.sha256(canonical_json(probe.payload)).hexdigest(), "json-object") for probe in probes]


def cases_for(model):
    identity = json.loads(Path(__file__).with_name("integration.json").read_text())["models"][model]
    if model == "evo2-40b":
        return evo_cases(identity["model_revision"])
    contract = json.loads((ROOT / "catalog/runtime/validators/assets" / (model + ".json")).read_text())
    cases = []
    for item in contract["requests"]:
        payload = copy.deepcopy(item.get("wire_request", item["request"]))
        if model == "nv-segment-ct":
            nifti = module("validate_nv_segment_ct").nifti_bytes(item["generator"])
            # The pinned fixture was serialized by Python 3.12/Linux. Python
            # 3.13 changes gzip's OS byte to 255; restore only that header byte,
            # then require the complete original payload SHA below to match.
            nifti = nifti[:9] + b"\x03" + nifti[10:]
            payload["input_nifti_base64"] = base64.b64encode(nifti).decode()
        original_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
        if "payload_sha256" in item and original_hash != item["payload_sha256"]:
            raise AcceptanceError("original_fixture_hash_mismatch")
        if model == "sdxl":
            payload["response_format"] = "b64_json"
        protocol, operation = MODELS[model]
        cases.append(AcceptanceCase(model, identity["model_revision"], protocol, operation,
            payload, hashlib.sha256(canonical_json(payload)).hexdigest(), "json-object"))
    return contract, cases


def validate_pair(model, contract, paths, directory):
    if model == "evo2-40b":
        validator = evo_validator()
        probes = validator.build_probes(("fs2-mm-public-evo-a", "fs2-mm-public-evo-b"))
        return {"status": "PASS", "contract": contract["contract"], "results": [
            validator.validate_response(json.loads(path.read_bytes()), probe)
            for path, probe in zip(paths, probes, strict=True)]}
    if model == "nv-reason-cxr-3b":
        return module("validate_response").validate(contract, paths)
    if model == "nv-segment-ct":
        validator = module("validate_nv_segment_ct")
        outputs = [validator.validate_response(path.read_bytes(), contract, item) for path, item in zip(paths, contract["requests"], strict=True)]
        if len({item["mask_sha256"] for item in outputs}) != 2:
            raise AcceptanceError("segmentation_outputs_not_distinct")
        return {"status": "PASS", "results": outputs}
    portable = copy.deepcopy(contract)
    for item, (size, digest) in zip(portable["requests"], PNG_ORACLES, strict=True):
        item["oracle"].update(expected_bytes=size, expected_sha256=digest)
    first = json.loads(paths[0].read_bytes())
    expected = contract["model"]
    if (first.get("model") != expected["id"] or first.get("repository") != expected["repository"]
            or first.get("revision") != expected["revision"] or first.get("mime_type") != "image/png"
            or not isinstance(first.get("request_id"), str) or not first["request_id"]
            or len(first.get("data", [])) != 1):
        raise AcceptanceError("sdxl_first_envelope_invalid")
    decoded = base64.b64decode(first["data"][0]["b64_json"], validate=True)
    if first.get("png_sha256") != hashlib.sha256(decoded).hexdigest() or first.get("png_bytes") != len(decoded):
        raise AcceptanceError("sdxl_first_envelope_digest_invalid")
    decoded_path = directory / "decoded-first.png"
    decoded_path.write_bytes(decoded)
    # Existing mixed validator enforces both PNG identities, dimensions,
    # nonconstant pixels, second envelope identity and distinct outputs.
    return module("validate_sdxl").validate(portable, [decoded_path, paths[1]])


async def http_case(runner, case, index):
    path = "/v1/chat/completions" if case.protocol == "openai-chat" else f"/v1/models/{case.model_id}:invoke"
    payload = case.payload if case.protocol == "openai-chat" else {"operation": case.operation, "payload": case.payload}
    status, headers, _, raw = await runner._json(runner.authorized, "POST", path, payload=payload,
        headers={"idempotency-key": f"fs2-mm-http-{runner.nonce}-{case.model_id}-{index}",
                 "x-fs2-wait-seconds": "30", "x-fs2-deadline-seconds": str(int(runner.remaining()))})
    if status not in (200, 202) or not headers.get("x-fs2-operation-id"):
        raise AcceptanceError("http_admission_failed")
    operation_id = headers["x-fs2-operation-id"]
    operation = await runner._wait_operation(operation_id)
    write_evidence(runner.evidence_root / "http" / case.model_id / f"operation-{index}.json", operation, forbidden=runner.forbidden)
    identity = runner._operation_summary(operation, case, operation_id)
    if status == 202:
        status, _, _, raw = await runner._json(runner.authorized, "GET", f"/v1/operations/{operation_id}/result")
        if status != 200:
            raise AcceptanceError("http_result_failed")
    return raw, identity, operation


async def mcp_case(runner, client, case, index):
    admitted = _mcp_result(await client.call_tool("invoke_model", {"model_id": case.model_id,
        "protocol": case.protocol, "payload": case.payload, "wait_seconds": 0,
        "idempotency_key": f"fs2-mm-mcp-{runner.nonce}-{case.model_id}-{index}"}))
    operation_id = admitted.get("id")
    if not isinstance(operation_id, str):
        raise AcceptanceError("mcp_operation_invalid")
    operation = admitted
    while operation.get("status") not in TERMINAL:
        await asyncio.sleep(min(1, runner.remaining()))
        operation = _mcp_result(await client.call_tool("get_operation", {"operation_id": operation_id}))
    write_evidence(runner.evidence_root / "mcp" / case.model_id / f"operation-{index}.json", operation, forbidden=runner.forbidden)
    identity = runner._operation_summary(operation, case, operation_id)
    result = _mcp_result(await client.call_tool("get_operation_result", {"operation_id": operation_id})).get("result")
    return canonical_json(result), identity, operation


async def verify(args, *, case_factory=cases_for, runtime_resolver=actual_runtime,
                 pair_validator=validate_pair, report_schema="fs2-h100-medical-media-public-verification/v1"):
    credentials = json.loads(args.credential_bundle.read_text())["credentials"]
    token, mcp_token = credentials["inference_access_token"], credentials["mcp_inference_token"]
    forbidden = tuple(value for value in credentials.values() if isinstance(value, str))
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    runner = AcceptanceRunner(origin=args.origin, token=token, release=None, cases=(),
        timeout_seconds=args.timeout, concurrency=1)
    runner.mcp_http.headers["authorization"] = "Bearer " + mcp_token
    runner.evidence_root, runner.forbidden = args.output, forbidden
    report = {"schema": report_schema, "started_at": utc_now(),
        "origin": args.origin, "tls_mode": "verified", "status": "RUNNING", "mcp_tool": "invoke_model",
        "startup_boundary": "routes already hot; public request-to-validated-result is not cold activation",
        "attempts": []}
    if "sdxl" in args.models:
        report["sdxl_transport_adaptation"] = "first original case changes only response_format image/png to b64_json; both decoded PNG oracles pinned to four direct H100 trials"
    discovered = {}

    async def cohort(surface, client=None):
        for model in args.models:
            contract, cases = case_factory(model)
            model_revision = cases[0].revision
            cases = [dataclasses.replace(case, revision=discovered[model]) for case in cases]
            directory = args.output / surface / model
            directory.mkdir(parents=True, mode=0o700)
            record = {"surface": surface, "model": model, "route_revision": discovered[model],
                "model_revision": model_revision, "status": "RUNNING", "requests": []}
            report["attempts"].append(record)
            try:
                paths = []
                for index, case in enumerate(cases, 1):
                    started_at, started = utc_now(), time.monotonic()
                    raw, operation, full_operation = await (http_case(runner, case, index) if client is None else mcp_case(runner, client, case, index))
                    completed_at, duration = utc_now(), time.monotonic() - started
                    runtime = runtime_resolver(args, full_operation, model)
                    path = directory / f"response-{index}.json"
                    path.write_bytes(raw)
                    paths.append(path)
                    record["requests"].append({"started_at": started_at, "completed_at": completed_at,
                        "duration_seconds": duration, "request_sha256": case.payload_sha256,
                        "response_sha256": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
                        "operation": operation, "runtime": runtime})
                record["semantic"] = pair_validator(model, contract, paths, directory)
                record["status"] = "PASS"
            except Exception as error:
                record.update(status="FAIL", failure_code=_safe_failure(error), failure_type=type(error).__name__)
            write_evidence(args.output / "report.json", report, forbidden=forbidden)
            print(json.dumps({"surface": surface, "model": model, "status": record["status"]}), flush=True)

    try:
        async with Client(streamable_http_client(args.origin + "/mcp", http_client=runner.mcp_http), mode=MCP_PROTOCOL_VERSION) as client:
            tools = await client.list_tools()
            if tools.ttl_ms != 0 or tools.cache_scope != "private" or "invoke_model" not in {tool.name for tool in tools.tools}:
                raise AcceptanceError("generic_mcp_discovery_invalid")
            models = _mcp_result(await client.call_tool("list_models", {}))
            discovered.update({item["id"]: item["revision"] for item in models["data"]})
            if any(model not in discovered for model in args.models):
                raise AcceptanceError("requested_public_model_not_discovered")
            report["discovered_route_revisions"] = {model: discovered[model] for model in args.models}
            await cohort("http")
            await cohort("mcp", client)
        report["status"] = "PASS" if len(report["attempts"]) == 2 * len(args.models) and all(record["status"] == "PASS" for record in report["attempts"]) else "FAIL"
    except Exception as error:
        report.update(status="FAIL", failure_code=_safe_failure(error), failure_type=type(error).__name__)
        def failure_leaves(exception):
            if isinstance(exception, BaseExceptionGroup):
                return [leaf for child in exception.exceptions for leaf in failure_leaves(child)]
            response = getattr(exception, "response", None)
            return [{"type": type(exception).__name__, "code": _safe_failure(exception),
                "http_status": getattr(response, "status_code", None)}]
        report["failure_causes"] = failure_leaves(error)
    finally:
        await runner.close()
        report["finished_at"] = utc_now()
        write_evidence(args.output / "report.json", report, forbidden=forbidden)
    return report["status"] == "PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(DEFAULT_MODELS))
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--kubeconfig", type=Path, default=Path("/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig"))
    parser.add_argument("--context", default="k8s-inference-h100")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(0 if asyncio.run(verify(args)) else 1)

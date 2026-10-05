"""Qualify a cold typed-MCP visual request using only the existing system QA key."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import httpx
import httpx2
import jsonschema
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


VISUAL = module("idle_visual_validation", ROOT / "acceptance/visual-science-20260919/run_public.py")
FIXTURES = module("fixtures", ROOT / "acceptance/wan2-sam2-20260920/fixtures.py")
MEDIA = module("idle_media_validation", ROOT / "acceptance/wan2-sam2-20260920/qualify.py")
MUSIC = module("idle_music_validation", ROOT / "acceptance/ace-step-20260920/qualify.py")
MINDGUARD = module("idle_mindguard_validation", ROOT / "models/mindguard/lifecycle_fixtures.py")


async def main(args):
    env = dict(line.split("=", 1) for line in args.env_file.read_text().splitlines() if "=" in line and not line.startswith("#"))
    token = env["SCIENTIFIC_MODELS_API_KEY"].strip().strip('"').strip("'")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = {"model": args.model, "run": args.run, "public_path": "typed-MCP", "status": "started"}
    async with (httpx.AsyncClient(base_url=args.origin, headers={"authorization": "Bearer " + token},
                                 timeout=180, trust_env=False) as http,
                httpx2.AsyncClient(headers={"authorization": "Bearer " + token, "origin": args.origin},
                                  trust_env=False, timeout=180) as transport,
                Client(streamable_http_client(args.origin + "/mcp", http_client=transport), mode="2026-07-28") as mcp):
        policy_response = await http.get("/v1/me")
        policy_response.raise_for_status()
        policy = policy_response.json()
        if policy["tenant_id"] != "system" or policy["principal_id"] not in {"qa", "development"}:
            raise RuntimeError("internal system QA/development key is required")
        receipt["identity"] = policy
        if args.model not in policy["models"]:
            raise RuntimeError("existing internal key lacks the exact model grant")
        tools = await VISUAL.list_tools(mcp)
        prefix = "idle-zero-20261005-" + args.model + "-" + args.run
        if args.model == "scvi-scanvi":
            artifact = await VISUAL.upload(http, args.model, args.fixture, "application/x-hdf5", prefix + "-upload")
            arguments = {"anndata_base64": artifact, "filename": args.fixture.name,
                         "method": args.method, "batch_key": "batch",
                         "labels_key": "cell_type" if args.method == "scanvi" else None,
                         "unlabeled_category": "Unknown", "max_epochs": 2, "n_latent": 4,
                         "seed": 17 if args.method == "scvi" else 18, "research_only": True}
            tool = "integrate_single_cell_native"
        elif args.model == "cellpose-cpsam-v2":
            artifact = await VISUAL.upload(http, args.model, args.fixture, "image/png", prefix + "-upload")
            arguments = {"image_base64": artifact, "media_type": "image/png", "diameter": None, "research_only": True}
            tool = "segment_cells_native"
        elif args.model == "ace-step-1-5":
            arguments = dict(MUSIC.REQUESTS[0])
            tool = "generate_music_native"
        elif args.model.startswith("wan2-"):
            arguments = (FIXTURES.wan_t2v_requests() if "-t2v-" in args.model
                         else FIXTURES.wan_i2v_requests())[0][1]
            tool = "generate_video_native" if "-t2v-" in args.model else "animate_image_native"
        elif args.model.startswith("mindguard-"):
            arguments = MINDGUARD.requests(args.model)[args.variant]
            tool = "assess_" + args.model.replace("-", "_") + "_native"
        else:
            arguments = FIXTURES.sam_requests()[0][1]
            tool = "segment_track_media_native"
        arguments.update(idempotency_key=prefix, wait_seconds=0)
        assert tool in tools, "typed tool not published: " + tool
        jsonschema.validate(arguments, tools[tool].input_schema)
        receipt["tool_schema_sha256"] = hashlib.sha256(
            json.dumps(tools[tool].input_schema, sort_keys=True).encode()).hexdigest()
        if args.preflight_only:
            receipt.update(status="preflight-only", tool=tool, inference_submitted=False)
            (args.output / (prefix + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps({"model": args.model, "tool": tool, "status": "preflight-only",
                              "tool_schema_sha256": receipt["tool_schema_sha256"]}), flush=True)
            return
        started = time.monotonic()
        try:
            if args.resume_operation:
                accepted = {"id": args.resume_operation}
                receipt.update(public_path="typed-MCP recovery", inference_submitted=False,
                               recovery_of_existing_operation=True)
            elif args.direct:
                if not args.model.startswith("mindguard-"):
                    raise RuntimeError("direct compatibility route applies only to MindGuard")
                body = {key: value for key, value in arguments.items() if key not in {"idempotency_key", "wait_seconds"}}
                response = await http.post("/v1/mindguard/assess", json=body,
                    headers={"idempotency-key": prefix, "x-fs2-wait-seconds": "0"})
                assert response.status_code in {200, 202}, response.status_code
                accepted = (response.json() if response.status_code == 202 else
                            {"id": response.headers["x-fs2-operation-id"]})
                receipt.update(public_path="REST assess + typed-MCP poll", admission_status=response.status_code)
            else:
                accepted = VISUAL.data(await mcp.call_tool(tool, arguments))
            receipt["operation_id"] = accepted["id"]
            (args.output / (prefix + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps({"model": args.model, "operation_id": accepted["id"], "status": "accepted"}), flush=True)
            if args.resume_operation:
                replay_id = accepted["id"]
            elif args.direct:
                repeated = await http.post("/v1/mindguard/assess", json=body,
                    headers={"idempotency-key": prefix, "x-fs2-wait-seconds": "0"})
                repeated.raise_for_status()
                replay = repeated.json()
                # A warm operation may complete between idempotent POSTs. The
                # compatibility route returns the unchanged classification at
                # 200 and keeps operation identity in the response header.
                replay_id = (replay.get("id") if repeated.status_code == 202 else
                             repeated.headers.get("x-fs2-operation-id"))
            else:
                replay = VISUAL.data(await mcp.call_tool(tool, arguments))
                replay_id = replay["id"]
            assert replay_id == accepted["id"]
            operation, states = await VISUAL.poll(mcp, accepted["id"], args.timeout)
            receipt.update(operation=operation, states=states, elapsed_seconds=time.monotonic()-started)
            if operation["status"] != "succeeded":
                raise RuntimeError("operation did not succeed: " + str(operation.get("error_code")))
            if args.model.startswith("mindguard-"):
                envelope = VISUAL.data(await mcp.call_tool("get_operation_result", {"operation_id": accepted["id"]}))
                raw = json.dumps(envelope["result"]).encode()
            else:
                envelope, raw = await VISUAL.result_bytes(mcp, http, accepted["id"])
            receipt["result"] = envelope
            if args.model == "scvi-scanvi":
                semantic = VISUAL.validate_scvi(raw, hashlib.sha256(args.fixture.read_bytes()).hexdigest(), args.method,
                                               args.output, prefix)
            elif args.model == "cellpose-cpsam-v2":
                semantic = VISUAL.validate_cellpose(raw, hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
                                                   args.output, prefix)
            elif args.model == "ace-step-1-5":
                semantic = MUSIC.inspect_wav(raw)
                assert abs(semantic["duration_seconds"] - arguments["duration_seconds"]) < 0.1
            elif args.model.startswith("wan2-"):
                semantic = MEDIA.probe_mp4(raw)
                stream = semantic["streams"][0]
                assert (stream["width"], stream["height"]) == (832, 480)
                assert int(stream["nb_frames"]) == 61
            elif args.model.startswith("mindguard-"):
                semantic = MINDGUARD.validate(json.loads(raw), args.model)
            else:
                semantic = MEDIA.validate_sam(raw, arguments["mode"])
            receipt.update(status="passed", semantic=semantic, idempotency_verified=not args.resume_operation)
            print(json.dumps({"model": args.model, "operation_id": accepted["id"], "status": "passed",
                              "elapsed_seconds": receipt["elapsed_seconds"]}), flush=True)
        except Exception as error:
            receipt.update(status="failed", elapsed_seconds=time.monotonic()-started,
                           client_error={"type": type(error).__name__, "message": str(error),
                                         "code": getattr(error, "code", None),
                                         "data": getattr(error, "data", None)})
            print(json.dumps({"model": args.model, "operation_id": receipt.get("operation_id"),
                              "status": "failed", "client_error_type": type(error).__name__,
                              "client_error_code": getattr(error, "code", None)}), flush=True)
            raise
        finally:
            (args.output / (prefix + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("scvi-scanvi", "cellpose-cpsam-v2", "sam2-1-hiera-large",
                                           "ace-step-1-5", "wan2-2-t2v-nim", "wan2-2-i2v-nim",
                                           "mindguard-4b", "mindguard-8b"), required=True)
    parser.add_argument("--direct", action="store_true", help="Check compatibility assess route with202 + polling")
    parser.add_argument("--preflight-only", action="store_true", help="Validate discovery/schema without model admission")
    parser.add_argument("--resume-operation", help="Recover/poll an existing operation without submitting inference")
    parser.add_argument("--variant", type=int, choices=(0, 1), default=0)
    parser.add_argument("--method", choices=("scvi", "scanvi"), default="scvi")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--run", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    asyncio.run(main(parser.parse_args()))

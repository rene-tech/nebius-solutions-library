#!/usr/bin/env python3
"""Bounded Cosmos-only public HTTP/MCP canary; plan by default, never full readiness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
from jsonschema import Draft202012Validator
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/stockholm-customer-20260917"))
from collect_live import (  # noqa: E402
    POD_READ,
    check,
    digest,
    kube,
    now,
    policy,
    private_json,
    require_release,
    write_private,
)

spec = importlib.util.spec_from_file_location(
    "cosmos_public_helpers", ROOT / "acceptance/cosmos3-customer-20260915/run_acceptance.py"
)
assert spec and spec.loader
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)

MODEL = "cosmos3-nano"
SOURCE_URL = (
    "https://cdn.jsdelivr.net/gh/NVIDIA/cosmos@b0e54e88c322695dab188e6ed160c4d6d071c39d/"
    "cookbooks/cosmos3/generator/action/assets/videos/umi.mp4"
)
SOURCE_SHA256 = "9880133da0e4da3411e38a38069686b187a7bbb1893513f7d0b523accf5ddce4"
SOURCE_ADAPTER_SHA256 = "8b5c283086fbb00405889d861adcead0cc5461091dc036e7e5af4c668b5fb6dd"
# The approved renderer bundle omits the source YAML's single terminal newline.
# Pin those exact deployed bytes; do not normalize live content or accept drift.
ADAPTER_SHA256 = "cbdea972edc77cc9735eea0d30dfc70d733464fb39f8cdc7889c12c8a435363e"
RUNTIME_DIGEST = "sha256:6d2630c7d637b699557573f2c3fee8df5d4d0cd718977aa22549ed6a6ef30587"
PREFIX = "robotics-media-canary-"


def collect(args) -> dict:
    config = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-system",
        "exec",
        "-i",
        "deploy/fs2-serve-control-plane",
        "-c",
        "control-plane",
        "--",
        "python",
        "-",
        script=POD_READ.replace("tenant_id=stockholm", "tenant_id=robotics"),
    )
    rows = [row for row in config["tokens"] if row["name"] == "timmothy-cosmos3" and not row["revoked_at"]]
    check(len(rows) == 1, "exact_robotics_source_policy_required")
    reference = policy(rows[0])
    check(
        reference["tenant_id"] == "robotics" and reference["models"] == [MODEL] and reference["max_concurrency"] == 1,
        "robotics_cosmos_only_policy_changed",
    )
    deployment = kube(
        args.kubeconfig, args.context, "-n", "fs2-system", "get", "deployment", "fs2-serve-control-plane", "-o", "json"
    )
    pods = kube(args.kubeconfig, args.context, "-n", "fs2-system", "get", "pods", "-o", "json")
    d = deployment
    snapshot = {
        "public_endpoint": config["public_endpoint"],
        "team_policy": reference,
        "token_metadata": config["tokens"],
        "mounted_configuration_sha256": config["mounted_configuration_sha256"],
        "deployments": [
            {
                "name": d["metadata"]["name"],
                "generation": d["metadata"]["generation"],
                "observed_generation": d.get("status", {}).get("observedGeneration"),
                "replicas": d["spec"]["replicas"],
                "ready_replicas": d.get("status", {}).get("readyReplicas", 0),
                "images": {c["name"]: c["image"] for c in d["spec"]["template"]["spec"]["containers"]},
            }
        ],
        "pods": [
            {
                "name": p["metadata"]["name"],
                "containers": [
                    {
                        "name": c["name"],
                        "image_id": c.get("imageID"),
                        "ready": c["ready"],
                        "restarts": c["restartCount"],
                    }
                    for c in p.get("status", {}).get("containerStatuses", [])
                ],
            }
            for p in pods["items"]
        ],
    }
    require_release(snapshot, args.expected_cp_image)
    cm = kube(
        args.kubeconfig, args.context, "-n", "fs2-models", "get", "configmap", "cosmos3-nano-adapter", "-o", "json"
    )
    snapshot["adapter_sha256"] = hashlib.sha256(cm["data"]["adapter.py"].encode()).hexdigest()
    serving = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-models",
        "get",
        "deployments",
        "-l",
        "fs2-serve.nebius.ai/model-id=cosmos3-nano",
        "-o",
        "json",
    )
    snapshot["serving"] = [
        {
            "name": d["metadata"]["name"],
            "replicas": d["spec"].get("replicas", 0),
            "ready": d.get("status", {}).get("readyReplicas", 0),
            "spec_digest": d["spec"]["template"]["metadata"]
            .get("annotations", {})
            .get("fs2-serve.nebius.ai/spec-digest"),
            "runtime_images": [c["image"] for c in d["spec"]["template"]["spec"]["containers"]],
            "snapshot": d["spec"]["template"]["metadata"]
            .get("annotations", {})
            .get("fs2-serve.nebius.ai/snapshot-bundle"),
        }
        for d in serving["items"]
    ]
    serving_pods = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-models",
        "get",
        "pods",
        "-l",
        "fs2-serve.nebius.ai/model-id=cosmos3-nano",
        "-o",
        "json",
    )
    snapshot["serving_pods"] = [
        {
            "name": p["metadata"]["name"],
            "uid": p["metadata"]["uid"],
            "phase": p["status"]["phase"],
            "node": p["spec"].get("nodeName"),
            "containers": [
                {"name": c["name"], "image_id": c.get("imageID"), "ready": c["ready"], "restarts": c["restartCount"]}
                for c in p["status"].get("containerStatuses", [])
            ],
        }
        for p in serving_pods["items"]
        if p["status"]["phase"] not in {"Succeeded", "Failed"}
    ]
    return snapshot


def release(snapshot: dict) -> dict:
    return {
        "image": snapshot["deployments"][0]["images"],
        "config": snapshot["mounted_configuration_sha256"],
        "policy": snapshot["team_policy"],
        "adapter": snapshot["adapter_sha256"],
        "serving": [{k: v for k, v in row.items() if k not in {"replicas", "ready"}} for row in snapshot["serving"]],
    }


def validate_key(key: dict, snapshot: dict) -> str:
    check(key.get("schema") == "fs2-customer-key/v1" and key.get("disposable") is True, "disposable_canary_required")
    token = key.get("secret")
    check(isinstance(token, str) and token, "canary_secret_missing")
    rows = [r for r in snapshot["token_metadata"] if r["fingerprint"] == hashlib.sha256(token.encode()).hexdigest()]
    check(len(rows) == 1, "canary_not_in_robotics_tenant")
    row = rows[0]
    check(
        row["name"].startswith(PREFIX) and row["principal_id"].startswith(PREFIX) and row["revoked_at"] is None,
        "existing_user_key_refused",
    )
    check(
        row["expires_at"] and datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) > datetime.now(UTC),
        "canary_expired_or_unbounded",
    )
    check(row["id"] == key["token_id"] and policy(row) == snapshot["team_policy"], "canary_policy_mismatch")
    return token


def checkpoint(path: Path, document: object, token: str) -> None:
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".pending")
    write_private(temporary, document, (token,))
    os.replace(temporary, path)


def issue(args) -> None:
    check(not args.key_file.exists(), "existing_key_file_refused")
    before = collect(args)
    principal = PREFIX + str(uuid4())
    payload = dict(
        before["team_policy"],
        principal_id=principal,
        name=principal,
        expires_at=(datetime.now(UTC) + timedelta(hours=6)).isoformat(),
    )
    setup = POD_READ[: POD_READ.index("print(json.dumps(")].replace("tenant_id=stockholm", "tenant_id=robotics")
    code = (
        setup
        + "\npayload="
        + repr(payload)
        + """
request=urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens',
 data=json.dumps(payload).encode(),method='POST',headers={'Authorization':'Bearer '+secret,
 'Content-Type':'application/json','Host':urlsplit(origin).netloc})
with urllib.request.urlopen(request,timeout=15) as response: print(response.read().decode())
"""
    )
    issued = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-system",
        "exec",
        "-i",
        "deploy/fs2-serve-control-plane",
        "-c",
        "control-plane",
        "--",
        "python",
        "-",
        script=code,
    )
    write_private(
        args.key_file,
        {
            "schema": "fs2-customer-key/v1",
            "disposable": True,
            "token_id": issued.get("id"),
            "principal_id": principal,
            "secret": issued.get("token"),
            "issued": issued,
            "policy": before["team_policy"],
        },
    )
    print(json.dumps({"created_disposable_canary": principal, "existing_keys_changed": False}))


def revoke(args) -> None:
    snapshot = collect(args)
    key = private_json(args.key_file)
    validate_key(key, snapshot)
    identifier = str(old.UUID(key["token_id"]))
    setup = POD_READ[: POD_READ.index("print(json.dumps(")].replace("tenant_id=stockholm", "tenant_id=robotics")
    code = (
        setup
        + "\nidentifier="
        + repr(identifier)
        + """
request=urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens/'+identifier,
 method='DELETE',headers={'Authorization':'Bearer '+secret,'Host':urlsplit(origin).netloc})
with urllib.request.urlopen(request,timeout=15) as response:
 value=json.load(response)
 print(json.dumps({'token_id':value['id'],'revoked_at':value['revoked_at']}))
"""
    )
    result = kube(
        args.kubeconfig,
        args.context,
        "-n",
        "fs2-system",
        "exec",
        "-i",
        "deploy/fs2-serve-control-plane",
        "-c",
        "control-plane",
        "--",
        "python",
        "-",
        script=code,
    )
    check(result["token_id"] == identifier and result["revoked_at"], "canary_revocation_not_confirmed")
    write_private(args.key_file.with_suffix(".revocation.json"), result)
    print(json.dumps(result))


async def wait_http(http, operation_id: str, timeout: float) -> dict:
    deadline, observed = time.monotonic() + timeout, []
    while time.monotonic() < deadline:
        response = await http.get(f"/v1/operations/{operation_id}")
        check(response.status_code == 200, "http_operation_read_failed")
        operation = response.json()
        if not observed or observed[-1]["status"] != operation["status"]:
            observed.append({"at": now(), "status": operation["status"]})
        if operation["status"] in old.TERMINAL:
            check(operation["status"] == "succeeded", "http_operation_terminal_failure")
            return {"operation": operation, "observed_statuses": observed}
        await asyncio.sleep(3)
    raise TimeoutError("bounded_public_operation_timeout")


def payload(mode: str, reference: object) -> dict:
    body = {
        "prompt": (
            "A first-person robot gripper moves around a wooden office desk. "
            "Keep the scene and motion; use warm afternoon lighting."
        ),
        "negative_prompt": "missing objects, temporal jitter, blur",
        "input_reference": reference,
        "num_frames": 33,
        "fps": 20,
        "seed": 20260917,
        "num_inference_steps": 35,
        "guidance_scale": 6.0,
    }
    if mode == "video-to-video":
        body.update(size="256x256", condition_frame_indexes_vision=[0, 1], condition_video_keep="first")
    else:
        check(mode == "transfer-video", "unsupported_mode")
        body.update(
            resolution=256,
            controls=[{"control_type": "edge", "control_weight": 1.0}],
            num_video_frames_per_chunk=33,
            num_conditional_frames=1,
            num_first_chunk_conditional_frames=0,
            share_vision_temporal_positions=True,
            emphasize_control_in_prompt=True,
        )
    return body


def operation_id(value: dict) -> str:
    # A direct OperationView uses `operation` for the method name, not an envelope.
    candidate = value if value.get("id") or value.get("operation_id") else value.get("operation")
    check(isinstance(candidate, dict), "operation_envelope_invalid")
    try:
        return str(old.UUID(str(candidate.get("id", candidate.get("operation_id")))))
    except (TypeError, ValueError):
        raise old.AcceptanceError("operation_id_invalid") from None


def http_invocation(mode: str, body: dict) -> dict:
    return {
        "operation": "generate-media",
        "payload": body | {"mode": mode, "output_format": "mp4", "output_delivery": "artifact"},
    }


def validate_public_contract(models: dict, schema: dict, tools: dict, uploaded: dict) -> None:
    rows = [row for row in models["data"] if row["id"] == MODEL]
    check(len(rows) == 1 and rows[0]["operations"] == ["generate-media"], "published_media_operation_changed")
    generic = [row for row in schema["contracts"] if row["tool_name"] == "cosmos3_nano_generate_media_native"]
    check(len(generic) == 1, "native_media_contract_missing")
    for reference in (SOURCE_URL, uploaded):
        for mode in ("video-to-video", "transfer-video"):
            body = payload(mode, reference)
            Draft202012Validator(generic[0]["input_schema"]).validate(http_invocation(mode, body)["payload"])
            tool = "cosmos3_nano_" + mode.replace("-", "_")
            Draft202012Validator(tools[tool]["inputSchema"]).validate(
                body | {"idempotency_key": PREFIX + "contract-check", "wait_seconds": 0}
            )


async def run_case(args, public, tools, snapshot, token, source, reference, mode, transport, reference_kind):
    name = f"{args.phase}-{transport}-{reference_kind}-{mode}"
    path = args.output / (name + ".json")
    body = payload(mode, reference)
    tool = "cosmos3_nano_" + mode.replace("-", "_")
    identity = digest({"release": release(snapshot), "token_id": args.token_id})
    if path.exists():
        row = private_json(path)
        check(row["identity"] == identity and row["payload_sha256"] == digest(body), "resume_identity_changed")
    else:
        row = {
            "identity": identity,
            "payload_sha256": digest(body),
            "idempotency_key": PREFIX + str(uuid4()),
            "case": name,
            "started_at": now(),
            "state": "prepared",
        }
        checkpoint(path, row, token)
    if row["state"] == "passed":
        return row
    controls = {"idempotency_key": row["idempotency_key"], "wait_seconds": 0}
    Draft202012Validator(tools[tool]["inputSchema"]).validate(body | controls)

    async def submit():
        if transport == "mcp":
            return operation_id(await public.call(tool, body | controls))
        request = http_invocation(mode, body)
        response = await public.http.post(
            f"/v1/models/{MODEL}:invoke",
            json=request,
            headers={"Idempotency-Key": row["idempotency_key"], "X-FS2-Wait-Seconds": "0"},
        )
        check(response.status_code in {200, 202}, "http_admission_failed")
        return str(response.headers["x-fs2-operation-id"])

    try:
        if not row.get("operation_id"):
            current = await asyncio.to_thread(collect, args)
            check(release(current) == release(snapshot), "release_changed_before_submission")
            row["serving_before"] = current["serving"]
            row["pods_before"] = current["serving_pods"]
            row["observed_start_state"] = "cold" if sum(d["replicas"] for d in current["serving"]) == 0 else "hot"
            if args.phase == "cold" and not args.first_admitted:
                check(
                    sum(d["replicas"] for d in current["serving"]) == 0 and not current["serving_pods"],
                    "cold_request_not_scale_from_zero",
                )
            elif args.phase == "hot" or args.first_admitted:
                check(sum(d["ready"] for d in current["serving"]) > 0, "hot_runtime_not_ready")
            row["operation_id"] = await submit()
            row["state"] = "admitted"
            checkpoint(path, row, token)
        args.first_admitted = True
        row["replay_operation_id"] = await submit()
        check(row["replay_operation_id"] == row["operation_id"], "inflight_replay_changed_operation")
        started = time.monotonic()
        if transport == "http":
            row["terminal"] = await wait_http(public.http, row["operation_id"], args.timeout_seconds)
            response = await public.http.get(f"/v1/operations/{row['operation_id']}/result")
            check(response.status_code == 200, "http_result_failed")
            envelope = response.json()
        else:
            row["terminal"] = await public.wait(row["operation_id"], args.timeout_seconds)
            envelope = await public.call("get_operation_result", {"operation_id": row["operation_id"]})
        result = envelope.get("result", envelope)
        check(result.get("content_type") == "video/mp4", "result_not_video")
        artifact = result["artifact"]
        destination = args.output / (name + ".mp4")
        if not destination.exists():
            if transport == "http":
                response = await public.http.get(f"/v1/artifacts/{artifact['artifact_id']}/content")
                check(response.status_code == 200, "http_artifact_download_failed")
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(response.content)
            else:
                await public.download("download_model_artifact", artifact, destination)
        measured = old.mp4_probe(destination)
        check(
            measured["sha256"] == artifact["sha256"] and measured["size_bytes"] == artifact["size_bytes"],
            "artifact_identity_mismatch",
        )
        check(measured["frames"] == 33 and measured["fps"] == 20, "output_timeline_mismatch")
        check(measured["decoded_rgb_sha256"] != source["decoded_rgb_sha256"], "unchanged_output")
        if mode == "video-to-video":
            check((measured["width"], measured["height"]) == (256, 256), "v2v_geometry_mismatch")
        row.update(
            artifact=artifact,
            decoded=measured,
            elapsed_poll_seconds=time.monotonic() - started,
            terminal_replay_operation_id=await submit(),
            completed_at=now(),
            state="passed",
        )
        check(row["terminal_replay_operation_id"] == row["operation_id"], "terminal_replay_changed_operation")
    except Exception as error:
        row.update(state="failed", failure_type=type(error).__name__, failure_code=getattr(error, "code", None))
        if isinstance(error, old.AcceptanceError):
            row["failure_code"] = str(error)
        if row.get("operation_id"):
            try:
                response = await public.http.get(f"/v1/operations/{row['operation_id']}")
                if response.status_code == 200:
                    row["last_operation"] = response.json()
            except Exception:
                row["failure_operation_readback"] = "unavailable"
        checkpoint(path, row, token)
        raise
    checkpoint(path, row, token)
    print(json.dumps({"case": name, "operation_id": row["operation_id"], "state": row["state"]}), flush=True)
    return row


async def execute(args) -> None:
    before = await asyncio.to_thread(collect, args)
    check(before["adapter_sha256"] == ADAPTER_SHA256, "shared_adapter_cutover_not_complete")
    check(
        before["serving"]
        and all(
            all(image.endswith("@" + RUNTIME_DIGEST) for image in row["runtime_images"]) for row in before["serving"]
        ),
        "runtime_identity_mismatch",
    )
    key = private_json(args.key_file)
    token = validate_key(key, before)
    args.token_id, args.first_admitted = key["token_id"], False
    args.output.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    origin = before["public_endpoint"].rstrip("/")
    check(origin.startswith("https://"), "verified_https_required")
    async with httpx2.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as fetcher:
        response = await fetcher.get(SOURCE_URL)
        check(
            response.status_code == 200
            and response.headers.get("content-type", "").split(";", 1)[0] == "video/mp4"
            and hashlib.sha256(response.content).hexdigest() == SOURCE_SHA256,
            "pinned_https_fixture_changed",
        )
        source_path = args.output / "input.mp4"
        if not source_path.exists():
            descriptor = os.open(source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(response.content)
    source = old.mp4_probe(source_path)
    check(source["sha256"] == SOURCE_SHA256, "local_input_changed")
    record = {
        "scope": "public-cosmos-media-http-mcp-only",
        "customer_ready": False,
        "started_at": now(),
        "phase": args.phase,
        "source": source,
        "calls": [],
        "outcome": "incomplete",
    }
    async with httpx2.AsyncClient(
        base_url=origin,
        timeout=60,
        trust_env=False,
        follow_redirects=False,
        headers={"Authorization": "Bearer " + token, "Origin": origin},
    ) as http:
        async with Client(
            streamable_http_client(origin + "/mcp", http_client=http), mode=old.MCP_PROTOCOL_VERSION
        ) as mcp:
            public = old.PublicClient(mcp, http)
            tools, catalog_digest = await old.inventory(mcp)
            check(
                {"cosmos3_nano_video_to_video", "cosmos3_nano_transfer_video"} <= tools.keys(),
                "typed_media_tools_missing",
            )
            record["tool_catalog_sha256"] = catalog_digest
            uploaded_path = args.output / "uploaded.json"
            if uploaded_path.exists():
                uploaded = private_json(uploaded_path)
            else:
                uploaded = await public.upload(
                    MODEL, source_path, "video/mp4", "none", PREFIX + args.phase + "-upload-" + args.token_id
                )
                write_private(uploaded_path, uploaded, (token,))
            models = await public.call("list_models", {})
            schema = await public.call("get_model_schema", {"model_id": MODEL, "protocol": "native"})
            validate_public_contract(models, schema, tools, uploaded)
            record["public_contract_sha256"] = digest({"models": models, "schema": schema})
            for transport in args.transports:
                for reference_kind in args.reference_kinds:
                    reference = SOURCE_URL if reference_kind == "https" else uploaded
                    for mode in args.modes:
                        row = await run_case(
                            args, public, tools, before, token, source, reference, mode, transport, reference_kind
                        )
                        record["calls"].append(row)
                        checkpoint(args.output / "partial-receipt.json", record, token)
            after = await asyncio.to_thread(collect, args)
            check(release(before) == release(after), "release_changed_during_run")
            _, after_catalog = await old.inventory(mcp)
            check(catalog_digest == after_catalog, "tool_catalog_changed")
            record.update(outcome="partial_scope_passed", completed_at=now(), release=release(after))
            record["serving_pods_after"] = after["serving_pods"]
            checkpoint(args.output / "partial-receipt.json", record, token)
    print(json.dumps({"outcome": record["outcome"], "customer_ready": False, "output": str(args.output)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "issue", "run", "revoke"), default="plan")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--expected-cp-image")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase", choices=("cold", "hot"), default="cold")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--transports", nargs="+", choices=("mcp", "http"), default=["mcp", "http"])
    parser.add_argument("--reference-kinds", nargs="+", choices=("https", "upload"), default=["https", "upload"])
    parser.add_argument(
        "--modes", nargs="+", choices=("video-to-video", "transfer-video"), default=["video-to-video", "transfer-video"]
    )
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "model": MODEL,
                    "scenarios": "2 transports x 2 reference kinds x 2 modes",
                    "cold": "only first request is cold; subsequent requests are verified hot",
                    "source_url": SOURCE_URL,
                    "source_sha256": SOURCE_SHA256,
                    "policy": "exact current timmothy-cosmos3 policy; disposable principal only",
                    "customer_ready": False,
                },
                indent=2,
            )
        )
        return
    check(args.kubeconfig and args.context and args.expected_cp_image and args.key_file, "live_arguments_required")
    if args.action == "issue":
        issue(args)
    elif args.action == "revoke":
        revoke(args)
    else:
        check(args.output and 0 < args.timeout_seconds <= 7200, "bounded_run_arguments_required")
        try:
            asyncio.run(execute(args))
        except Exception as error:
            print(
                json.dumps(
                    {
                        "outcome": "failed",
                        "failure_type": type(error).__name__,
                        "failure_code": getattr(error, "code", None),
                        "customer_ready": False,
                    }
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()

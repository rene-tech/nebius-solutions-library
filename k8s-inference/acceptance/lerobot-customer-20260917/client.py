#!/usr/bin/env python3
"""Local LeRobot directory -> public batch -> reopened output datasets.

No administrative API, cloud credential, key mutation or automatic POST retry.
HTTP selects the submission transport; artifact discovery/download uses the
advertised public MCP tools for both submission modes.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4
from builtins import BaseExceptionGroup

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "lerobot_existing_harness",
    HERE.parent / "cosmos3-customer-20260915/run_acceptance.py",
)
assert spec and spec.loader
harness = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = harness
spec.loader.exec_module(harness)
check, now, digest = harness.check, harness.now, harness.digest
MODEL = "cosmos3-lerobot-augmentation"
TOOL = "submit_cosmos3_lerobot_augmentation"
TERMINAL = harness.TERMINAL


def read_key(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        check(
            stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600,
            "key_file_requires_regular_0600",
        )
        raw = stream.read(65537)
    check(0 < len(raw) <= 65536, "key_file_size_invalid")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw.strip()
    if isinstance(value, dict):
        value = value.get("secret") or value.get("token")
    check(
        isinstance(value, str) and value and not any(c.isspace() for c in value),
        "key_file_invalid",
    )
    return value


def save(path: Path, state, token=""):
    encoded = json.dumps(state, sort_keys=True, indent=2, allow_nan=False) + "\n"
    check(not token or token not in encoded, "receipt_contains_secret")
    descriptor, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, path)


def reader(python: Path, *arguments):
    completed = subprocess.run(
        [str(python), str(HERE / "dataset_io.py"), *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode:
        try:
            code = json.loads(completed.stdout.strip().splitlines()[-1])["code"]
        except (ValueError, KeyError, IndexError):
            code = "validation_failed"
        check(
            False,
            "pinned_reader_"
            + (code if re.fullmatch(r"[a-zA-Z0-9_]+", code) else "validation_failed"),
        )


def artifact_view(value):
    keys = ("artifact_id", "sha256", "size_bytes", "media_type", "compression")
    check(
        isinstance(value, dict) and all(k in value for k in keys),
        "artifact_reference_invalid",
    )
    str(UUID(value["artifact_id"]))
    check(
        re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
        and isinstance(value["size_bytes"], int)
        and value["size_bytes"] > 0,
        "artifact_identity_invalid",
    )
    return {k: value[k] for k in keys}


def operation(value):
    nested = value.get("operation")
    return nested if isinstance(nested, dict) else value


def safe_error_code(error):
    # MCP context managers can wrap our terminal assertion in ExceptionGroup.
    # Preserve a safe semantic code without printing nested transport URLs/data.
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            code = safe_error_code(child)
            if re.fullmatch(r"[a-z0-9_]+", code):
                return code
        return type(error).__name__
    return (
        str(error) if re.fullmatch(r"[a-z0-9_]+", str(error)) else type(error).__name__
    )


def published_variant(variant, entry, operation_id, index, seed):
    """Join the worker-local output label to its finalized public manifest ref."""
    public_ref = artifact_view(entry["artifact"])
    local_ref = variant.get("artifact", {})
    check(
        entry["semantic_type"] == "lerobot-v3-augmented-bundle/v1"
        and variant.get("variant_index") == index
        and variant.get("seed") == seed
        and local_ref.get("artifact_id")
        in {f"{operation_id}.variant-{index:02d}", public_ref["artifact_id"]}
        and all(
            local_ref.get(k) == public_ref[k]
            for k in ("sha256", "size_bytes", "media_type", "compression")
        ),
        "variant_manifest_identity_mismatch",
    )
    return public_ref


def settled(value, expected):
    check(operation(value).get("status") == expected, "operation_terminal_failure")
    batch = value.get("batch", {})
    return batch.get("status", expected) == expected and all(
        attempt.get("resource_released") is True
        for stage in batch.get("stages", [])
        for attempt in stage.get("attempts", [])
    )


class DatasetClient(harness.PublicClient):
    def __init__(self, mcp, http, state, output, token):
        super().__init__(mcp, http)
        self.state, self.output, self.token = state, output, token

    def persist(self):
        save(self.output / "run.json", self.state, self.token)

    async def response(self, method, path, *, expected=200, **kwargs):
        # Deliberately no transport retry, including all mutating requests.
        response = await self.http.request(method, path, **kwargs)
        check(response.status_code == expected, f"public_http_{response.status_code}")
        return response.json()

    async def upload_file(self, label, path, media_type, compression):
        measured = harness.file_identity(path)
        entry = {
            "idempotency_key": self.state["run_id"] + "-" + label,
            "identity": measured,
            "phase": "begin_pending",
        }
        self.state.setdefault("uploads", {})[label] = entry
        self.persist()
        begun = await self.response(
            "POST",
            "/v1/scientific-artifacts/uploads",
            expected=201,
            headers={"Idempotency-Key": entry["idempotency_key"]},
            json={
                "model_id": MODEL,
                **measured,
                "media_type": media_type,
                "compression": compression,
            },
        )
        op_id, upload_id = harness.operation_id(begun), str(UUID(begun["upload_id"]))
        entry.update(operation_id=op_id, upload_id=upload_id, phase="content_pending")
        self.persist()  # Durable IDs before sending any bytes.
        content_path = begun["content_path"]
        address = urlsplit(content_path)
        check(
            not address.scheme
            and not address.netloc
            and not address.fragment
            and address.path == f"/v1/scientific-artifacts/uploads/{upload_id}/content"
            and parse_qs(address.query) == {"operation_id": [op_id]},
            "upload_content_path_invalid",
        )
        check(
            measured["size_bytes"] <= begun["max_content_bytes"],
            "upload_limit_exceeded",
        )

        async def chunks():
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk

        stored = await self.response(
            "PUT",
            content_path,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(measured["size_bytes"]),
            },
            content=chunks(),
        )
        check(
            all(stored[k] == measured[k] for k in measured), "uploaded_bytes_mismatch"
        )
        entry["phase"] = "finalize_pending"
        self.persist()
        artifact = artifact_view(
            await self.response(
                "POST",
                f"/v1/scientific-artifacts/uploads/{upload_id}:finalize",
                json={"operation_id": op_id},
            )
        )
        check(
            all(artifact[k] == measured[k] for k in measured)
            and artifact["media_type"] == media_type
            and artifact["compression"] == compression,
            "finalized_artifact_mismatch",
        )
        entry.update(phase="finalized", artifact=artifact)
        self.persist()
        return artifact

    async def submit(self, request, key, protocol):
        if protocol == "mcp":
            return await self.call(TOOL, {**request, "idempotency_key": key})
        return await self.response(
            "POST",
            f"/v1/models/{MODEL}:submit",
            expected=202,
            headers={"Idempotency-Key": key},
            json=request,
        )

    async def wait_existing(self, timeout, expected="succeeded"):
        deadline = time.monotonic() + timeout
        op_id = self.state["operation_id"]
        while time.monotonic() < deadline:
            value = await self.response("GET", f"/v1/operations/{op_id}")
            status = operation(value).get("status")
            progress = {
                "status": status,
                "stages": [
                    {
                        "stage_id": stage["stage_id"],
                        "status": stage["status"],
                        "phases": [
                            attempt.get("last_phase")
                            for attempt in stage.get("attempts", [])
                        ],
                    }
                    for stage in value.get("batch", {}).get("stages", [])
                ],
            }
            history = self.state.setdefault("progress", [])
            if not history or history[-1]["progress"] != progress:
                history.append({"at": now(), "progress": progress})
                print(json.dumps({"operation_id": op_id, **progress}), flush=True)
            self.state["last_status"] = value
            self.persist()
            if status in TERMINAL and settled(value, expected):
                return value
            await asyncio.sleep(3)
        raise harness.AcceptanceError("operation_timeout_keep_existing_id")

    async def download_file(self, artifact, destination, max_bytes):
        artifact = artifact_view(artifact)
        check(artifact["size_bytes"] <= max_bytes, "download_size_limit_exceeded")
        if destination.exists():
            check(
                not destination.is_symlink()
                and harness.file_identity(destination)
                == {k: artifact[k] for k in ("sha256", "size_bytes")},
                "existing_download_mismatch",
            )
            return
        value = await self.call(
            "download_scientific_artifact", {"artifact_id": artifact["artifact_id"]}
        )
        check(
            artifact_view(value["artifact"]) == artifact, "download_reference_mismatch"
        )
        handle = value["handle"]
        address = urlsplit(handle["url"])
        check(
            handle["method"] == "GET"
            and address.scheme == "https"
            and address.hostname
            and not address.username
            and not address.password,
            "download_handle_invalid",
        )
        check(
            datetime.fromisoformat(handle["expires_at"].replace("Z", "+00:00"))
            > datetime.now(UTC),
            "download_handle_expired",
        )
        # Gateway bearer is never passed to the object-store downloader.
        check(
            self.token not in json.dumps(handle), "download_handle_contains_gateway_key"
        )
        temporary = destination.with_suffix(destination.suffix + ".partial")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        total, hasher = 0, hashlib.sha256()
        with os.fdopen(descriptor, "wb") as stream:
            async with httpx2.AsyncClient(
                timeout=300, follow_redirects=False, trust_env=False
            ) as downloader:
                async with downloader.stream(
                    "GET", handle["url"], headers=handle.get("headers", {})
                ) as response:
                    check(response.status_code == 200, "artifact_download_failed")
                    async for chunk in response.aiter_raw():
                        total += len(chunk)
                        check(
                            total <= artifact["size_bytes"],
                            "artifact_download_oversized",
                        )
                        hasher.update(chunk)
                        stream.write(chunk)
        check(
            total == artifact["size_bytes"]
            and hasher.hexdigest() == artifact["sha256"],
            "artifact_download_identity_mismatch",
        )
        temporary.rename(destination)


@asynccontextmanager
async def connect(endpoint, token, state, output):
    async with httpx2.AsyncClient(
        base_url=endpoint,
        timeout=300,
        follow_redirects=False,
        trust_env=False,
        headers={"Authorization": "Bearer " + token, "Origin": endpoint},
    ) as http:
        async with Client(
            streamable_http_client(endpoint + "/mcp", http_client=http),
            mode=harness.MCP_PROTOCOL_VERSION,
        ) as mcp:
            yield DatasetClient(mcp, http, state, output, token)


async def collect_outputs(client, args):
    state, output = client.state, client.output
    published = await client.response(
        "GET", f"/v1/operations/{state['operation_id']}/result"
    )
    check(
        published.get("terminal_status") == "succeeded",
        "scientific_result_not_succeeded",
    )
    manifest_ref = artifact_view(published["output_manifest"])
    inspected = await client.call(
        "inspect_scientific_artifact_manifest",
        {"artifact_id": manifest_ref["artifact_id"], "limit": 32},
    )
    check(
        inspected.get("artifact") == manifest_ref
        and inspected.get("truncated") is False,
        "output_manifest_invalid",
    )
    entries = inspected.get("entries", [])
    names = {row["name"]: row for row in entries}
    count = state["request"]["parameters"]["variants"]["count"]
    expected = {"result"} | {f"variant-{i:02d}" for i in range(count)}
    check(
        set(names) == expected and len(entries) == len(expected),
        "output_manifest_entries_invalid",
    )
    check(
        names["result"]["semantic_type"] == "lerobot-augmentation-result/v1",
        "result_semantic_type_invalid",
    )
    await client.download_file(manifest_ref, output / "output-manifest.json", 1024**2)
    await client.download_file(
        names["result"]["artifact"], output / "result.json", 1024**2
    )
    result = harness.private_json(output / "result.json")
    check(
        result.get("schema")
        == "fs2-serve.nebius.ai/cosmos3-lerobot-augmentation-result/v1"
        and result.get("operation_id") == state["operation_id"]
        and result.get("status") == "succeeded"
        and result.get("failures") == []
        and len(result.get("variants", [])) == count,
        "augmentation_result_invalid",
    )
    validations = []
    for index, variant in enumerate(result["variants"]):
        label = f"variant-{index:02d}"
        public_ref = published_variant(
            variant,
            names[label],
            state["operation_id"],
            index,
            state["request"]["parameters"]["variants"]["seeds"][index],
        )
        archive, meta, receipt = (
            output / (label + ".tar.zst"),
            output / (label + ".json"),
            output / (label + "-validation.json"),
        )
        await client.download_file(public_ref, archive, args.max_bytes)
        if not meta.exists():
            harness.write_private(meta, variant, (client.token,))
        if not receipt.exists():
            reader(
                args.reader_python,
                "validate",
                "--source",
                output / "source",
                "--archive",
                archive,
                "--destination",
                output / label,
                "--parameters",
                output / "parameters.json",
                "--variant",
                meta,
                "--receipt",
                receipt,
                "--max-bytes",
                args.max_bytes,
                "--max-expanded-bytes",
                args.max_expanded_bytes,
            )
        validations.append(harness.private_json(receipt))
    state.update(
        scientific_result=published,
        output_manifest=inspected,
        validations=validations,
        outcome="dataset_integrity_passed",
        physical_alignment_verified=False,
        customer_ready=False,
        completed_at=now(),
    )
    client.persist()


async def execute(args, state, token):
    async with connect(state["endpoint"], token, state, args.output) as public:
        if args.command == "run":
            tools, catalog_hash = await harness.inventory(public.mcp)
            required = {
                "download_scientific_artifact",
                "inspect_scientific_artifact_manifest",
            }
            if args.protocol == "mcp":
                required.add(TOOL)
            check(required <= set(tools), "required_public_tool_missing")
            catalog = await public.response("GET", "/v1/scientific-models")
            check(
                any(
                    row.get("id", row.get("model_id")) == MODEL
                    for row in catalog.get("data", [])
                ),
                "lerobot_not_publicly_discoverable",
            )
            state.update(tool_catalog_sha256=catalog_hash, scientific_catalog=catalog)
            public.persist()
            prepared = harness.private_json(args.output / "prepared.json")
            bundle = await public.upload_file(
                "dataset", args.output / "dataset.tar.zst", "application/x-tar", "zstd"
            )
            manifest = {
                "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
                "manifest_id": state["run_id"],
                "entries": [
                    {
                        "name": "lerobot-dataset",
                        "semantic_type": "lerobot-v3-bundle/v1",
                        "artifact": bundle,
                    }
                ],
            }
            harness.write_private(
                args.output / "input-manifest.json", manifest, (token,)
            )
            input_manifest = await public.upload_file(
                "manifest",
                args.output / "input-manifest.json",
                "application/vnd.fs2.scientific-manifest+json",
                "none",
            )
            parameters = prepared["parameters"]
            parameters["source"] = {"kind": "uploaded-bundle", **bundle}
            harness.write_private(args.output / "parameters.json", parameters, (token,))
            request = {
                "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
                "operation": "augment-lerobot-dataset",
                "service_class": "customer-batch",
                "input_manifest": input_manifest,
                "parameters": parameters,
            }
            state.update(request=request, phase="admission_pending")
            public.persist()
            accepted = await public.submit(
                request, state["idempotency_key"], args.protocol
            )
            state.update(
                operation_id=harness.operation_id(accepted),
                accepted=accepted,
                phase="admitted",
            )
            public.persist()
            if args.check_replay:
                replay = await public.submit(
                    request, state["idempotency_key"], args.protocol
                )
                check(
                    harness.operation_id(replay) == state["operation_id"],
                    "inflight_replay_changed_operation",
                )
                state["inflight_replay_verified"] = True
                public.persist()
            if args.submit_only:
                return
        else:
            check(
                state.get("operation_id"), "no_parent_id_do_not_resubmit_with_new_key"
            )
        if args.command == "cancel":
            state["cancel_requested_at"] = now()
            public.persist()
            state["cancellation_response"] = await public.response(
                "POST", f"/v1/operations/{state['operation_id']}:cancel"
            )
            public.persist()
        expected = "cancelled" if state.get("cancel_requested_at") else "succeeded"
        state["terminal"] = await public.wait_existing(args.timeout_seconds, expected)
        public.persist()
        if expected == "cancelled":
            state.update(outcome="cancelled_and_released", completed_at=now())
            public.persist()
            return
        if state.get("check_replay") and not state.get("terminal_replay_verified"):
            replay = await public.submit(
                state["request"], state["idempotency_key"], state["protocol"]
            )
            check(
                harness.operation_id(replay) == state["operation_id"],
                "terminal_replay_changed_operation",
            )
            state["terminal_replay_verified"] = True
            public.persist()
        await collect_outputs(public, args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "resume", "cancel"))
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--reader-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol", choices=("http", "mcp"), default="mcp")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024**3)
    parser.add_argument("--max-expanded-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--check-replay", action="store_true")
    parser.add_argument("--submit-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    token, state = "", {}
    try:
        check(
            0 < args.timeout_seconds <= 7200
            and 0 < args.max_bytes <= 5 * 1024**3
            and 0 < args.max_expanded_bytes <= 8 * 1024**3,
            "bounded_limits_required",
        )
        token = read_key(args.key_file)
        if args.command == "run":
            check(
                args.dataset and args.request and args.endpoint,
                "local_dataset_request_endpoint_required",
            )
            endpoint = args.endpoint.rstrip("/")
            parsed = urlsplit(endpoint)
            check(
                parsed.scheme == "https"
                and parsed.netloc
                and parsed.path in ("", "/")
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment,
                "https_origin_required",
            )
            check(
                not args.output.resolve().is_relative_to(args.dataset.resolve()),
                "output_inside_dataset_refused",
            )
            args.output.mkdir(mode=0o700, parents=True)
            identifier = "lerobot-" + str(uuid4())
            state = {
                "schema": "fs2-serve.nebius.ai/lerobot-directory-client/v1",
                "run_id": identifier,
                "idempotency_key": identifier,
                "started_at": now(),
                "endpoint": endpoint,
                "protocol": args.protocol,
                "key_fingerprint": hashlib.sha256(token.encode()).hexdigest(),
                "check_replay": args.check_replay,
                "outcome": "in_progress",
                "customer_ready": False,
            }
            save(args.output / "run.json", state, token)
        else:
            state = harness.private_json(args.output / "run.json")
            check(
                hashlib.sha256(token.encode()).hexdigest() == state["key_fingerprint"],
                "resume_principal_changed",
            )
            check(
                not args.endpoint or args.endpoint.rstrip("/") == state["endpoint"],
                "resume_endpoint_changed",
            )
        descriptor = os.open(
            args.output / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                if args.command == "run":
                    reader(
                        args.reader_python,
                        "prepare",
                        "--dataset",
                        args.dataset,
                        "--request",
                        args.request,
                        "--output",
                        args.output,
                        "--max-bytes",
                        args.max_bytes,
                        "--max-expanded-bytes",
                        args.max_expanded_bytes,
                    )
                asyncio.run(execute(args, state, token))
            except Exception as error:
                code = safe_error_code(error)
                state.setdefault("failures", []).append(
                    {"at": now(), "code": code, "type": type(error).__name__}
                )
                state["outcome"] = "failed_keep_existing_ids"
                save(args.output / "run.json", state, token)  # Still owns the lock.
                raise
        print(
            json.dumps(
                {
                    "outcome": state["outcome"],
                    "operation_id": state.get("operation_id"),
                    "output": str(args.output),
                    "customer_ready": False,
                }
            )
        )
        return 0
    except Exception as error:
        code = safe_error_code(error)
        print(
            json.dumps(
                {
                    "outcome": "failed",
                    "code": code,
                    "operation_id": state.get("operation_id"),
                    "automatic_resubmission": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a file-backed starter recipe through the public, typed MCP surface.

The same materializer is used for offline contract checks and live execution.
Local filenames and large bytes never pass through an LLM. Receipts retain an
accepted operation for resumption; ambiguous admissions are not resubmitted.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from jsonschema import Draft202012Validator


def encoded(value):
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def download_verified(http, url, target, reference):
    """Stream a large immutable result, publishing only complete verified bytes."""
    temporary = target.with_name(target.name + ".partial")
    digest, size = hashlib.sha256(), 0
    async with http.stream("GET", url) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            async for block in response.aiter_bytes():
                size += len(block)
                if size > reference["size_bytes"]:
                    raise ValueError("download_size_mismatch")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    if size != reference["size_bytes"] or digest.hexdigest() != reference["sha256"]:
        raise ValueError("download_checksum_mismatch")
    os.replace(temporary, target)


async def download_with_retry(http, url, target, reference, *, deadline):
    """Retry only immutable GETs; never duplicate a model submission."""
    import httpx2

    for attempt in range(6):
        try:
            await download_verified(http, url, target, reference)
            return
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code not in {429, 502, 503, 504}:
                raise
            if attempt == 5 or time.monotonic() >= deadline:
                raise
        except (httpx2.TransportError, httpx2.TimeoutException):
            if attempt == 5 or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(min(2**attempt, 10))


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class ToolRejected(RuntimeError):
    def __init__(self, details):
        self.details = details if isinstance(details, dict) else {}
        super().__init__(self.details.get("code", "mcp_tool_error"))


def unpack(response):
    value = response.model_dump(mode="json", by_alias=True)
    if value.get("structuredContent") is not None:
        payload = value["structuredContent"]
    else:
        texts = [
            item["text"]
            for item in value.get("content", [])
            if item.get("type") == "text"
        ]
        if len(texts) != 1:
            raise ValueError("unexpected_mcp_response")
        payload = json.loads(texts[0])
    if value.get("isError"):
        raise ToolRejected(payload.get("error", {}))
    return payload


async def call_with_capacity_wait(client, tool, arguments, *, deadline):
    from mcp import MCPError

    while True:
        try:
            try:
                response = await client.call_tool(tool, arguments)
            except MCPError as exc:
                data = exc.data if isinstance(exc.data, dict) else {}
                # Preserve structured transport-level non-admission as well
                # as ordinary tool errors. An unstructured transport error
                # remains unknown and must be reconciled before resubmission.
                if data.get("durable_admission") is False:
                    raise ToolRejected(
                        {
                            **data,
                            "code": data.get("code", data.get("type", "mcp_rejected")),
                        }
                    ) from exc
                raise
            unpack(response)
            return response
        except ToolRejected as exc:
            error = exc.details
            if (
                error.get("code")
                not in {"concurrency_exceeded", "admission_limit_reached"}
                or error.get("durable_admission") is not False
                or time.monotonic() >= deadline
            ):
                raise
            await asyncio.sleep(5)


def retryable_rejection(response):
    """Only explicit non-admission backpressure is safe to retry automatically."""
    if response.status_code != 429:
        return False
    try:
        return response.json().get("error", {}).get("type") == "concurrency_exceeded"
    except (ValueError, AttributeError):
        return False


async def request_with_capacity_wait(http, method, url, *, deadline, **kwargs):
    while True:
        response = await http.request(method, url, **kwargs)
        if not retryable_rejection(response) or time.monotonic() >= deadline:
            return response
        await asyncio.sleep(5)


async def read_with_retry(http, url, *, deadline):
    """Retry only side-effect-free reads across bounded gateway interruptions."""
    import httpx2

    for attempt in range(6):
        try:
            response = await http.get(url)
            if response.status_code not in {429, 502, 503, 504}:
                return response
        except (httpx2.TransportError, httpx2.TimeoutException):
            if attempt == 5 or time.monotonic() >= deadline:
                raise
        else:
            if attempt == 5 or time.monotonic() >= deadline:
                return response
        await asyncio.sleep(min(2**attempt, 10))
    raise RuntimeError("read_retry_exhausted")


class Inputs:
    def __init__(self, root, manifest, upload):
        self.root, self.upload = root.resolve(), upload
        self.objects = {item["path"]: item for item in manifest["objects"]}

    def read(self, relative):
        metadata = self.objects[relative]
        path = self.root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("input_outside_pack")
        data = path.read_bytes()
        if len(data) != metadata["size_bytes"] or sha(data) != metadata["sha256"]:
            raise ValueError("input_changed")
        return data

    async def materialize(self, value):
        if isinstance(value, list):
            return [await self.materialize(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$file" in value:
            data = self.read(value["$file"])
            kind = value["encoding"]
            if kind == "text":
                return data.decode()
            if kind == "json":
                return json.loads(data)
            if kind == "base64":
                return base64.b64encode(data).decode()
            if kind == "data-url":
                return (
                    "data:"
                    + value["media_type"]
                    + ";base64,"
                    + base64.b64encode(data).decode()
                )
            if kind != "artifact":
                raise ValueError("unknown_file_encoding")
            return await self.upload(
                data, value["media_type"], value.get("compression", "none")
            )
        for selector, media in [
            ("$manifest", "application/vnd.fs2.scientific-manifest+json"),
            ("$json_artifact", "application/json"),
        ]:
            if selector in value:
                content = await self.materialize(json.loads(self.read(value[selector])))
                return await self.upload(encoded(content), media, "none")
        result = {
            key: await self.materialize(item)
            for key, item in value.items()
            if key != "$merge_file"
        }
        if "$merge_file" in value:
            result.update(await self.materialize(value["$merge_file"]))
        return result


async def offline_upload(data, media, compression):
    return {
        "artifact_id": str(uuid5(NAMESPACE_URL, sha(data))),
        "sha256": sha(data),
        "size_bytes": len(data),
        "media_type": media,
        "compression": compression,
    }


def validate_arguments(recipe, contract, arguments):
    if sha(encoded(contract)) != recipe["contract_sha256"]:
        raise ValueError("published_contract_changed_refresh_pack")
    arguments = {**arguments, "idempotency_key": "starter-offline-validation"}
    if "wait_seconds" in contract["input_schema"]["properties"]:
        arguments["wait_seconds"] = 0
    Draft202012Validator(contract["input_schema"]).validate(arguments)


async def validate_manifest_roles(recipe, discovery, inputs):
    """The outer tool schema cannot express a referenced manifest's roles."""
    contract = discovery.get("input_artifact_contract")
    if not contract or recipe["protocol"] != "scientific-batch-v1":
        return
    request = recipe["arguments"]
    reference = request["input_manifest"]
    manifest = await inputs.materialize(json.loads(inputs.read(reference["$manifest"])))
    expected = contract.get("entry")
    if "operations" in contract:
        expected = contract["operations"].get(request["operation"])
    if "source_kinds" in contract:
        expected = contract["source_kinds"].get(request["parameters"]["source"]["kind"])
    if expected is None:
        raise ValueError("scientific_manifest_role_undiscovered")
    entries = manifest["entries"]
    if len(entries) != 1:
        raise ValueError("scientific_manifest_entry_count")
    entry, artifact = entries[0], entries[0]["artifact"]
    if (
        entry["name"] != expected["name"]
        or entry["semantic_type"] != expected["semantic_type"]
        or artifact["media_type"] != expected["media_type"]
        or artifact["compression"]
        not in expected.get("allowed_compressions", [expected["compression"]])
        or not 1 <= artifact["size_bytes"] <= expected["maximum_bytes"]
    ):
        raise ValueError("scientific_manifest_role_mismatch")


async def validate_all(root, contracts):
    manifest = json.loads((root / "manifest.json").read_bytes())
    inputs = Inputs(root, manifest, offline_upload)
    results = []
    for case in manifest["cases"]:
        for recipe in json.loads(inputs.read(case["recipes"]))["recipes"]:
            discovery = json.loads(
                (contracts / (recipe["model_id"] + ".json")).read_bytes()
            )
            matching = [
                c
                for c in discovery["contracts"]
                if c["tool_name"] == recipe["tool_name"]
            ]
            assert len(matching) == 1
            try:
                arguments = await inputs.materialize(recipe["arguments"])
                validate_arguments(recipe, matching[0], arguments)
                await validate_manifest_roles(recipe, discovery, inputs)
                results.append(
                    {
                        "case_id": case["id"],
                        "model_id": recipe["model_id"],
                        "state": "schema-validated",
                    }
                )
            except Exception as exc:  # noqa: BLE001 -- retain bounded per-recipe validation failure
                results.append(
                    {
                        "case_id": case["id"],
                        "model_id": recipe["model_id"],
                        "state": "failed",
                        "error": str(exc).split("\n")[0][:300],
                    }
                )
    return results


async def run(root, case_id, model, output, *, endpoint, key, observe_seconds=None):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("verified_https_mcp_endpoint_required")
    origin = parsed.scheme + "://" + parsed.netloc
    manifest = json.loads((root / "manifest.json").read_bytes())
    case = next(item for item in manifest["cases"] if item["id"] == case_id)
    inputs = Inputs(root, manifest, offline_upload)
    recipes = json.loads(inputs.read(case["recipes"]))["recipes"]
    if model is None:
        model = recipes[0]["model_id"]
    recipe = next(item for item in recipes if item["model_id"] == model)
    if observe_seconds is None:
        observe_seconds = max(
            1800,
            recipe["arguments"].get("parameters", {}).get("max_wall_seconds", 1800),
        )
    download_budget = recipe.get("download_budget_bytes", 128 * 1024 * 1024)
    download_count = recipe.get("download_max_artifacts", 256)
    if not isinstance(download_budget, int) or not 0 < download_budget <= 8 * 1024**3:
        raise ValueError("invalid_recipe_download_budget")
    if not isinstance(download_count, int) or not 0 < download_count <= 4096:
        raise ValueError("invalid_recipe_artifact_budget")
    offline_arguments = await inputs.materialize(recipe["arguments"])
    input_sha256 = sha(encoded(offline_arguments))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = {
        "case_id": case_id,
        "model_id": model,
        "recipe_sha256": sha(encoded(recipe)),
        "endpoint": endpoint,
        "caller_sha256": sha(key.encode()),
    }
    receipt_path = output / "receipt.json"
    record = (
        json.loads(receipt_path.read_bytes())
        if receipt_path.exists()
        else {
            "identity": identity,
            "state": "prepared",
            "idempotency_key": "starter-" + uuid4().hex,
            "started_at": datetime.now(UTC).isoformat(),
            "uploads": {},
            "artifacts": [],
        }
    )
    if record["identity"] != identity:
        raise ValueError("output_belongs_to_different_run")
    if record.get("input_sha256", input_sha256) != input_sha256:
        raise ValueError("output_belongs_to_different_inputs")
    record.setdefault("input_sha256", input_sha256)
    if record["state"] in {"submitting", "admission_unknown"} and not record.get(
        "operation_id"
    ):
        previous = output / "submission.json"
        saved = json.loads(previous.read_bytes()) if previous.exists() else {}
        error = (saved.get("structuredContent") or {}).get("error", {})
        if saved.get("isError") and error.get("durable_admission") is False:
            record["state"] = "rejected"
            record["recovered_nonadmission_code"] = error.get("code")
            save(receipt_path, record)
        else:
            raise RuntimeError(
                "admission_unknown_do_not_resubmit_inspect_saved_submission"
            )
    if record["state"] in {"succeeded", "failed", "cancelled", "expired", "preempted"}:
        for artifact in record["artifacts"]:
            path = output / artifact["local_path"]
            if not path.is_file() or file_sha(path) != artifact["sha256"]:
                raise ValueError("retained_result_artifact_missing_or_changed")
        return record
    save(receipt_path, record)
    started = time.monotonic()
    headers = {"authorization": "Bearer " + key, "origin": origin}
    async with httpx2.AsyncClient(
        headers=headers, timeout=120, trust_env=False
    ) as http:

        async def upload(data, media, compression):
            identity = sha(encoded([sha(data), media, compression]))
            if identity in record["uploads"]:
                return record["uploads"][identity]
            metadata = {
                "model_id": model,
                "sha256": sha(data),
                "size_bytes": len(data),
                "media_type": media,
                "compression": compression,
            }
            response = await request_with_capacity_wait(
                http,
                "POST",
                origin + "/v1/scientific-artifacts/uploads",
                deadline=started + observe_seconds,
                json=metadata,
                headers={
                    "Idempotency-Key": record["idempotency_key"] + "-upload-" + identity
                },
            )
            response.raise_for_status()
            begun = response.json()
            upload_id, operation_id = (
                str(UUID(begun["upload_id"])),
                str(UUID(begun["operation_id"])),
            )
            # Never persist the signed direct-upload handle. Retain the durable
            # identity before validation/I/O so interruption cannot orphan it.
            record.setdefault("pending_uploads", {})[identity] = {
                "operation_id": operation_id,
                "upload_id": upload_id,
                "max_content_bytes": begun["max_content_bytes"],
            }
            save(receipt_path, record)
            if len(data) > begun["max_content_bytes"]:
                cancelled = await http.post(
                    origin + "/v1/operations/" + operation_id + ":cancel"
                )
                cancelled.raise_for_status()
                record["pending_uploads"][identity]["state"] = (
                    "cancelled_input_too_large"
                )
                save(receipt_path, record)
                raise ValueError("upload_limit")
            path = f"/v1/scientific-artifacts/uploads/{upload_id}/content?operation_id={operation_id}"
            response = await http.put(
                origin + path, content=data, headers={"Content-Type": media}
            )
            response.raise_for_status()
            response = await http.post(
                origin + f"/v1/scientific-artifacts/uploads/{upload_id}:finalize",
                json={"operation_id": operation_id},
            )
            response.raise_for_status()
            artifact = response.json()
            if any(
                artifact[name] != metadata[name]
                for name in ("sha256", "size_bytes", "media_type", "compression")
            ):
                raise ValueError("uploaded_artifact_mismatch")
            record["uploads"][identity] = artifact
            record["pending_uploads"].pop(identity, None)
            save(receipt_path, record)
            return artifact

        async def download_artifacts(value):
            pending = [value]
            retained = {a["artifact_id"] for a in record["artifacts"]}
            expanded, local_references = set(), []
            total = sum(a["size_bytes"] for a in record["artifacts"])
            while pending:
                item = pending.pop()
                if isinstance(item, list):
                    pending.extend(item)
                elif isinstance(item, dict):
                    if {
                        "artifact_id",
                        "sha256",
                        "size_bytes",
                        "media_type",
                    } <= item.keys():
                        try:
                            artifact_id = str(UUID(item["artifact_id"]))
                        except ValueError:
                            # Model-native provenance can name a logical output
                            # before the publisher assigned its public UUID.
                            # It must still resolve to a checksum-verified public
                            # output; never silently ignore an unavailable file.
                            local_references.append(item)
                            continue
                        if artifact_id in retained:
                            if (
                                artifact_id not in expanded
                                and item["media_type"]
                                in {
                                    "application/json",
                                    "application/vnd.fs2.scientific-manifest+json",
                                }
                                and item.get("compression", "none") == "none"
                            ):
                                stored = next(
                                    a
                                    for a in record["artifacts"]
                                    if a["artifact_id"] == artifact_id
                                )
                                data = (output / stored["local_path"]).read_bytes()
                                if (
                                    sha(data) != item["sha256"]
                                    or len(data) != item["size_bytes"]
                                ):
                                    raise ValueError(
                                        "retained_result_artifact_missing_or_changed"
                                    )
                                pending.append(json.loads(data))
                                expanded.add(artifact_id)
                            continue
                        if (
                            total + item["size_bytes"] > download_budget
                            or len(retained) >= download_count
                        ):
                            raise ValueError("result_download_budget_exceeded")
                        name = artifact_id + ".bin"
                        await download_with_retry(
                            http,
                            origin + "/v1/artifacts/" + artifact_id + "/content",
                            output / name,
                            item,
                            deadline=started + observe_seconds,
                        )
                        record["artifacts"].append({**item, "local_path": name})
                        retained.add(artifact_id)
                        total += item["size_bytes"]
                        save(receipt_path, record)
                        if (
                            item["media_type"]
                            in {
                                "application/json",
                                "application/vnd.fs2.scientific-manifest+json",
                            }
                            and item.get("compression", "none") == "none"
                        ):
                            pending.append(json.loads((output / name).read_bytes()))
                            expanded.add(artifact_id)
                    else:
                        pending.extend(item.values())
            for reference in local_references:
                if not any(
                    all(
                        reference[field] == stored[field]
                        for field in ("sha256", "size_bytes", "media_type")
                    )
                    for stored in record["artifacts"]
                ):
                    raise ValueError("logical_output_reference_unresolved")

        async with Client(
            streamable_http_client(endpoint, http_client=http), mode="2026-07-28"
        ) as client:
            if not record.get("operation_id"):
                contract_value = unpack(
                    await client.call_tool(
                        "get_model_schema",
                        {"model_id": model, "tool_name": recipe["tool_name"]},
                    )
                )
                contract = contract_value["contracts"][0]
                await validate_manifest_roles(recipe, contract_value, inputs)
                arguments = await inputs.materialize(recipe["arguments"])
                validate_arguments(
                    recipe, contract, arguments
                )  # Before any upload/admission.
                inputs.upload = upload
                arguments = await inputs.materialize(recipe["arguments"])
                arguments["idempotency_key"] = record["idempotency_key"]
                if "wait_seconds" in contract["input_schema"]["properties"]:
                    arguments["wait_seconds"] = 0
                record["state"] = "submitting"
                save(receipt_path, record)
                try:
                    raw = await call_with_capacity_wait(
                        client,
                        recipe["tool_name"],
                        arguments,
                        deadline=started + observe_seconds,
                    )
                    save(
                        output / "submission.json",
                        raw.model_dump(mode="json", by_alias=True),
                    )
                    admitted = unpack(raw)
                    operation = (
                        admitted["operation"]
                        if isinstance(admitted.get("operation"), dict)
                        else admitted
                    )
                    record.update(
                        operation_id=str(UUID(operation["id"])),
                        state=operation["status"],
                        admitted_at=datetime.now(UTC).isoformat(),
                    )
                    save(receipt_path, record)
                except ToolRejected as exc:
                    record["state"] = (
                        "rejected"
                        if exc.details.get("durable_admission") is False
                        else "admission_unknown"
                    )
                    record["error_code"] = str(exc)
                    save(receipt_path, record)
                    raise
                except Exception:
                    record["state"] = "admission_unknown"
                    save(receipt_path, record)
                    raise
            while True:
                response = await read_with_retry(
                    http,
                    origin + "/v1/operations/" + record["operation_id"],
                    deadline=started + observe_seconds,
                )
                response.raise_for_status()
                status = response.json()
                save(output / "operation.json", status)
                operation = (
                    status["operation"]
                    if isinstance(status.get("operation"), dict)
                    else status
                )
                state = operation["status"]
                record["state"] = "result_pending" if state == "succeeded" else state
                save(receipt_path, record)
                if state == "succeeded" and operation.get("result_available"):
                    response = await read_with_retry(
                        http,
                        origin + "/v1/operations/" + record["operation_id"] + "/result",
                        deadline=started + observe_seconds,
                    )
                    response.raise_for_status()
                    result = response.json()
                    save(output / "result.json", result)
                    await download_artifacts(result)
                    record.update(
                        state="succeeded",
                        completed_at=datetime.now(UTC).isoformat(),
                        observation_seconds=round(time.monotonic() - started, 3),
                        semantic_validation="pending",
                    )
                    save(receipt_path, record)
                    return record
                if (
                    state in {"failed", "cancelled", "expired", "preempted"}
                    or time.monotonic() - started >= observe_seconds
                ):
                    return record
                await asyncio.sleep(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--contracts", type=Path)
    parser.add_argument(
        "--observe-seconds",
        type=int,
        help="Override the recipe's wait budget; rerun the same output path to resume.",
    )
    args = parser.parse_args()
    if args.validate_only:
        if args.contracts is None:
            parser.error("--contracts is required for offline validation")
        results = asyncio.run(validate_all(args.root, args.contracts))
        failed = [r for r in results if r["state"] == "failed"]
        print(json.dumps({"recipes": len(results), "failed": failed}, indent=2))
        raise SystemExit(bool(failed))
    if not args.case or not args.output:
        parser.error(
            "case and --output are required; reuse --output to resume an accepted operation"
        )
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = asyncio.run(
            run(
                args.root,
                args.case,
                args.model,
                args.output,
                endpoint=os.environ["SCIENTIFIC_MODELS_MCP_URL"],
                key=os.environ["SCIENTIFIC_MODELS_API_KEY"],
                observe_seconds=args.observe_seconds,
            )
        )
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("state", "operation_id", "semantic_validation")
                if k in result
            }
        )
    )
    raise SystemExit(
        0
        if result["state"] == "succeeded"
        else 1
        if result["state"] in {"failed", "cancelled", "expired", "preempted"}
        else 2
    )


if __name__ == "__main__":
    main()

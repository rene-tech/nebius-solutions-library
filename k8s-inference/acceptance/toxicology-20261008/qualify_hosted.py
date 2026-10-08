"""Public REST and typed MCP qualification using only the existing system/qa key.

Runs two concurrent operations at a time. Exercises ordinary request admission,
immutable CSV/JSON uploads, published-set parity, large-result externalization,
idempotency, mixed invalid rows, method selection, bounded batches, and SDF.
Does not mutate replica settings or use a customer key. Retain failed receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fs2_serve.live_acceptance import MCP_PROTOCOL_VERSION, _mcp_result

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models/toxicology"))
from fixtures import MOLECULES  # noqa: E402

MODELS = ("admet-ai", "ctoxpred2")
TERMINAL = {"succeeded", "failed", "expired", "cancelled", "preempted"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def save(args, name, value):
    (args.output / (name + ".json")).write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n"
    )


async def run(args):
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    key = json.loads(args.key_file.read_text())
    receipts = []
    async with httpx2.AsyncClient(
        base_url=args.origin,
        timeout=60,
        trust_env=False,
        headers={"Authorization": "Bearer " + key["secret"], "Origin": args.origin},
    ) as http:
        response = await http.get("/v1/me")
        response.raise_for_status()
        me = response.json()
        assert (me["tenant_id"], me["principal_id"]) == ("system", "qa")
        assert me["max_concurrency"] == 2
        async with Client(
            streamable_http_client(args.origin + "/mcp", http_client=http),
            mode=MCP_PROTOCOL_VERSION,
        ) as mcp:

            async def tool(name, arguments):
                raw = await mcp.call_tool(name, arguments)
                return _mcp_result(raw)

            async def upload(model, raw, media_type, name):
                response = await http.post(
                    "/v1/scientific-artifacts/uploads",
                    json={
                        "model_id": model,
                        "sha256": digest(raw),
                        "size_bytes": len(raw),
                        "media_type": media_type,
                    },
                    headers={
                        "Idempotency-Key": f"tox-{args.cohort}-{model}-upload-{name}"
                    },
                )
                response.raise_for_status()
                reservation = response.json()
                response = await http.put(
                    reservation["content_path"],
                    content=raw,
                    headers={"Content-Type": media_type},
                )
                response.raise_for_status()
                response = await http.post(
                    f"/v1/scientific-artifacts/uploads/{reservation['upload_id']}:finalize",
                    json={"operation_id": reservation["operation_id"]},
                )
                response.raise_for_status()
                artifact = response.json()
                assert artifact["sha256"] == digest(raw) and artifact[
                    "size_bytes"
                ] == len(raw)
                save(args, model + "-input-" + name, artifact)
                return artifact

            async def case(
                model,
                name,
                payload,
                path="rest",
                *,
                count=1,
                failure=False,
                partial=False,
                reference=None,
            ):
                idem = f"tox-{args.cohort}-{model}-{name}"
                submitted = time.monotonic()
                trace = {
                    "model": model,
                    "case": name,
                    "path": path,
                    "request": payload,
                    "cohort": args.cohort,
                    "expected_count": count,
                    "expected_failure": failure,
                    "status_transitions": [],
                }
                save(args, model + "-" + name + "-trace", trace)

                async def submit():
                    if path == "mcp":
                        return await tool(
                            "infer_" + model.replace("-", "_") + "_native",
                            {**payload, "idempotency_key": idem, "wait_seconds": 0},
                        )
                    response = await http.post(
                        f"/v1/models/{model}:invoke",
                        json={"operation": "screen-molecules", "payload": payload},
                        headers={
                            "Idempotency-Key": idem,
                            "x-fs2-wait-seconds": "0",
                            "x-fs2-deadline-seconds": "900",
                        },
                    )
                    response.raise_for_status()
                    if response.status_code == 200:
                        response = await http.get(
                            "/v1/operations/" + response.headers["x-fs2-operation-id"]
                        )
                        response.raise_for_status()
                    return response.json()

                try:
                    accepted = await submit()
                    operation_id = accepted["id"]
                    trace["operation_id"] = operation_id
                    replay = await submit()
                    assert replay["id"] == operation_id
                    trace["idempotency_passed"] = True
                    print(
                        json.dumps(
                            {
                                "model": model,
                                "case": name,
                                "operation": operation_id,
                                "status": accepted["status"],
                            }
                        ),
                        flush=True,
                    )
                    deadline = time.monotonic() + 960
                    while time.monotonic() < deadline:
                        response = await http.get(f"/v1/operations/{operation_id}")
                        response.raise_for_status()
                        operation = response.json()
                        status = operation["status"]
                        if (
                            not trace["status_transitions"]
                            or trace["status_transitions"][-1]["status"] != status
                        ):
                            trace["status_transitions"].append(
                                {
                                    "status": status,
                                    "elapsed_seconds": time.monotonic() - submitted,
                                }
                            )
                            save(args, model + "-" + name + "-trace", trace)
                            print(
                                json.dumps(
                                    {"model": model, "case": name, "status": status}
                                ),
                                flush=True,
                            )
                        if status in TERMINAL:
                            break
                        await asyncio.sleep(2)
                    trace["operation"] = operation
                    assert (
                        operation["tenant_id"] == "system"
                        and operation["principal_id"] == "qa"
                    )
                    if failure:
                        assert operation["status"] == "failed", operation
                        trace["passed"] = True
                        return trace
                    assert operation["status"] == "succeeded", operation
                    response = await http.get(f"/v1/operations/{operation_id}/result")
                    response.raise_for_status()
                    result = response.json()
                    if path == "mcp":
                        mcp_result = await tool(
                            "get_operation_result", {"operation_id": operation_id}
                        )
                        assert mcp_result["operation"]["id"] == operation_id
                        assert mcp_result["result"] == result
                    native = result.get("result", result)
                    trace["artifact_output"] = (
                        native.get("schema")
                        == "fs2-serve.nebius.ai/operation-artifact-result/v1"
                    )
                    if trace["artifact_output"]:
                        artifact = native["artifact"]
                        response = await http.get(
                            f"/v1/artifacts/{artifact['artifact_id']}/content"
                        )
                        response.raise_for_status()
                        assert (
                            len(response.content) == artifact["size_bytes"]
                            and digest(response.content) == artifact["sha256"]
                        )
                        native = response.json()
                        trace["result_artifact"] = artifact
                    save(args, model + "-" + name + "-result", native)
                    assert (
                        native["model_id"] == model
                        and native["molecule_count"] == count
                    )
                    assert len(native["results"]) == count
                    assert native["status"] == ("partial" if partial else "succeeded")
                    for row in native["results"]:
                        if row["status"] == "failed":
                            assert partial and row["error"]["code"] in {
                                "invalid_smiles",
                                "missing_smiles",
                            }
                            continue
                        for value in row["predictions"].values():
                            if model == "admet-ai":
                                assert math.isfinite(value)
                            else:
                                assert 0 <= value["positive_class_score"] <= 1
                    if reference:
                        differences = []
                        for row, expected in zip(
                            native["results"], reference, strict=True
                        ):
                            for endpoint, expected_value in expected[
                                "predictions"
                            ].items():
                                actual = row["predictions"][endpoint]
                                if model == "ctoxpred2":
                                    differences.append(
                                        abs(
                                            actual["positive_class_score"]
                                            - expected_value["positive_class_score"]
                                        )
                                    )
                                    assert (
                                        actual["predicted_class"]
                                        == expected_value["predicted_class"]
                                    )
                                else:
                                    differences.append(abs(actual - expected_value))
                        assert max(differences) < 1e-4
                        trace["reference_max_absolute_difference"] = max(differences)
                    trace["passed"] = True
                    return trace
                except Exception as error:
                    trace["passed"] = False
                    trace["error_type"] = type(error).__name__
                    raise
                finally:
                    trace["elapsed_seconds"] = time.monotonic() - submitted
                    receipts.append(trace)
                    save(args, model + "-" + name + "-trace", trace)
                    save(args, "receipts", receipts)
                    print(
                        json.dumps(
                            {
                                "model": model,
                                "case": name,
                                "passed": trace.get("passed", False),
                                "seconds": trace["elapsed_seconds"],
                            }
                        ),
                        flush=True,
                    )

            for model in MODELS:
                schema = await tool(
                    "get_model_schema", {"model_id": model, "protocol": "native"}
                )
                save(args, model + "-schema", schema)
                dataset = json.loads(
                    (
                        Path(__file__).parent / f"{model}-public-evaluation.json"
                    ).read_text()
                )["datasets"][0]
                source = dataset["inputs_and_labels"]
                text = (
                    "id,smiles\n"
                    + "\n".join(
                        f"published-{i},{row['SMILES']}" for i, row in enumerate(source)
                    )
                    + "\n"
                )
                csv_ref = await upload(model, text.encode(), "text/csv", "published250")
                bulk = [
                    {"id": str(i), "smiles": MOLECULES[i % len(MOLECULES)]["smiles"]}
                    for i in range(1000)
                ]
                json_ref = await upload(
                    model, encoded(bulk), "application/json", "batch1000"
                )
                await asyncio.gather(
                    case(
                        model,
                        "published250-csv",
                        {"csv": csv_ref, "id_column": "id"},
                        "rest",
                        count=250,
                        reference=dataset["predictions"],
                    ),
                    case(
                        model,
                        "batch1000-json",
                        {"molecules": json_ref},
                        "mcp",
                        count=1000,
                    ),
                )
                await asyncio.gather(
                    case(model, "single", {"smiles": MOLECULES[0]["smiles"]}, "mcp"),
                    case(
                        model,
                        "partial",
                        {
                            "molecules": [
                                {"id": "valid", "smiles": "CCO"},
                                {"id": "bad", "smiles": "not-a-molecule"},
                            ]
                        },
                        "rest",
                        count=2,
                        partial=True,
                    ),
                )
                sdf = "ethanol\n  FS2\n\n  3  2  0  0  0  0  0  0  0  0999 V2000\n    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n    1.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n    2.0000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n  1  2  1  0\n  2  3  1  0\nM  END\n$$$$\n"
                await case(model, "sdf", {"sdf": sdf}, "mcp")
                if model == "ctoxpred2":
                    await case(
                        model,
                        "neural",
                        {"smiles": "CCO", "method": "dl-sl", "seed": 17},
                        "mcp",
                    )
                await case(
                    model, "invalid", {"smiles": "not-a-molecule"}, "rest", failure=True
                )
                response = await http.post(
                    f"/v1/models/{model}:invoke",
                    json={
                        "operation": "screen-molecules",
                        "payload": {"smiles": "CCO", "csv": "smiles\nCCO"},
                    },
                    headers={
                        "Idempotency-Key": f"tox-{args.cohort}-{model}-invalid-contract"
                    },
                )
                assert response.status_code in {400, 422}, response.status_code
                save(
                    args,
                    model + "-invalid-contract",
                    {"status": response.status_code, "body": response.json()},
                )
    save(
        args,
        "cohort",
        {
            "cohort": args.cohort,
            "passed": all(row["passed"] for row in receipts),
            "receipts": receipts,
            "scope": "Public REST + typed MCP, not LibreChat/LLM or clinical validation",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohort", required=True)
    asyncio.run(run(parser.parse_args()))

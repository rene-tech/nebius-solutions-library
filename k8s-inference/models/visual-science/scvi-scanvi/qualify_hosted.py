"""Exercise hosted REST/MCP, replay, status and verified artifact downloads.

Uses only the existing system/qa credential. Input request references are
already finalized; no customer data or customer key is used.
"""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import httpx


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def checked(response):
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code}; request {response.headers.get('x-request-id')}; {response.text[:2000]}")
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--cohort", default="a")
    parser.add_argument("--protocol", choices=("rest", "mcp"), default="rest")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    os.umask(0o077)
    env = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
    with httpx.Client(base_url=args.origin, headers={"Authorization": "Bearer " + env["SCIENTIFIC_MODELS_API_KEY"]},
                      timeout=httpx.Timeout(300, connect=15), trust_env=False) as client:
        me = checked(client.get("/v1/me"))
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Internal qualification requires system/qa")
        save(args.output / "caller.json", me)
        counter = 0
        session = {}

        def rpc(method, params):
            nonlocal counter
            counter += 1
            response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream", **session},
                                   json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
            if response.is_error:
                checked(response)
            if response.headers.get("mcp-session-id"):
                session["mcp-session-id"] = response.headers["mcp-session-id"]
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                items = [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]
                value = next(item for item in items if item.get("id") == counter)
            else:
                value = response.json()
            if value.get("error"):
                raise ValueError(f"MCP failed: {value['error']}")
            return value["result"]

        def tool(name, arguments):
            value = rpc("tools/call", {"name": name, "arguments": arguments})
            if value.get("isError"):
                raise ValueError(f"MCP tool failed: {value}")
            if "structuredContent" in value:
                return value["structuredContent"]
            return json.loads(next(item["text"] for item in value["content"] if item["type"] == "text"))

        if args.protocol == "mcp":
            rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "fs2-internal-scvi-qualification", "version": "1"}})
            response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream", **session},
                                   json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            response.raise_for_status()
            tools = rpc("tools/list", {})
            selected = [item for item in tools["tools"] if item["name"] == "submit_scvi_scanvi"]
            if len(selected) != 1 or "parameters" not in selected[0]["inputSchema"]["properties"]:
                raise ValueError("Typed single-cell tool is absent")
            save(args.output / "mcp-tool.json", selected[0])
        if args.discover_only:
            print(json.dumps({"discovery": "passed", "protocol": args.protocol}))
            return
        operation_id = args.operation_id
        if args.request:
            body = json.loads(args.request.read_text())
            key = "scvi-hosted-20261006-" + args.cohort
            def submit():
                if args.protocol == "mcp":
                    return tool("submit_scvi_scanvi", {**body, "idempotency_key": key})
                return checked(client.post("/v1/models/scvi-scanvi:submit", json=body, headers={"Idempotency-Key": key}))
            admitted = submit()
            save(args.output / "admission.json", admitted)
            operation_id = admitted["operation"]["id"]
            replay = submit()
            save(args.output / "replay.json", replay)
            if replay["operation"]["id"] != operation_id or not replay["operation"]["reused"]:
                raise ValueError("Idempotency replay launched different work")
        if not operation_id:
            raise ValueError("Supply an existing operation or a finalized request")
        deadline = time.monotonic() + args.timeout
        previous = None
        while time.monotonic() < deadline:
            status = (tool("get_operation", {"operation_id": operation_id}) if args.protocol == "mcp"
                      else checked(client.get(f"/v1/operations/{operation_id}")))
            save(args.output / "status.json", status)
            state = status["batch"]["status"]
            if state != previous:
                print(json.dumps({"operation_id": operation_id, "protocol": args.protocol, "status": state}), flush=True)
                previous = state
            if state in {"succeeded", "failed", "cancelled"} and status["batch"]["result_published"]:
                break
            time.sleep(10)
        else:
            raise TimeoutError("Operation retained; do not duplicate work")
        result = checked(client.get(f"/v1/operations/{operation_id}/result"))
        save(args.output / "result.json", result)

        def download(pointer):
            destination = args.output / "artifacts" / pointer["artifact_id"]
            destination.parent.mkdir(exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with client.stream("GET", f"/v1/artifacts/{pointer['artifact_id']}/content",
                               headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                with destination.open("wb") as output:
                    for block in response.iter_bytes(1024**2):
                        digest.update(block)
                        size += len(block)
                        if size > pointer["size_bytes"]:
                            raise ValueError("Artifact exceeds its immutable byte count")
                        output.write(block)
            if (digest.hexdigest(), size) != (pointer["sha256"], pointer["size_bytes"]):
                raise ValueError("Downloaded artifact digest/size mismatch")
            return destination

        if result.get("output_manifest"):
            manifest = json.loads(download(result["output_manifest"]).read_text())
            save(args.output / "output-manifest.json", manifest)
            verified = []
            for entry in manifest["entries"]:
                pointer = entry["artifact"]
                path = download(pointer)
                verified.append({"artifact": pointer, "local_path": str(path)})
            save(args.output / "verified-artifacts.json", verified)
        # Keep transport failures/results visible even when the run failed.
        if state != "succeeded":
            raise ValueError(f"Hosted operation ended {state}; inspect the retained result")
        print(json.dumps({"operation_id": operation_id, "status": state, "result_saved": True}), flush=True)


if __name__ == "__main__":
    main()

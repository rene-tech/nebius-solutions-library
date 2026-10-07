"""Exercise hosted REST/MCP, replay, status and verified artifact downloads.

Uses only the existing system/qa credential. Input request references are
already finalized; no customer data or customer key is used.
"""

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import httpx


class AdmissionCapacityError(RuntimeError):
    """An explicit rejection before durable admission; safe to wait unchanged."""

    def __init__(self, error):
        super().__init__('API key concurrent-operation slots are occupied; no job was admitted')
        try:
            self.delay = min(30, max(1, float(error.get('retry_after_seconds', 5))))
        except (TypeError, ValueError):
            self.delay = 5


def capacity_rejection(error):
    return isinstance(error, dict) and error.get('code', error.get('type')) in {
        'concurrency_exceeded', 'admission_limit_reached'
    } and error.get('durable_admission') is not True and error.get('retryable') is not False


def submit_with_capacity_wait(submit, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        try:
            return submit()
        except AdmissionCapacityError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Admission wait expired; no job admitted. Retain uploads and retry the same request/key.') from error
            if not announced:
                print(json.dumps({'waiting_for_key_capacity': True, 'job_admitted': False}), flush=True)
                announced = True
            time.sleep(min(error.delay, remaining))


def tool_result(value, *, admission=False):
    structured = value.get('structuredContent')
    if structured is None:
        structured = json.loads(next(item['text'] for item in value['content'] if item['type'] == 'text'))
    if value.get('isError'):
        error = structured.get('error', {}) if isinstance(structured, dict) else {}
        if admission and capacity_rejection(error):
            raise AdmissionCapacityError(error)
        raise ValueError(f'MCP tool failed: {value}')
    return structured


def request_with_retry(client, method, url, *, retryable=None, **kwargs):
    """Retry reads/explicitly idempotent requests, never change their identity."""
    safe = method.upper() in {"GET", "HEAD"} if retryable is None else retryable
    attempts = 6 if safe else 1
    for attempt in range(attempts):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        else:
            if response.status_code not in {502, 503, 504} or attempt == attempts - 1:
                return response
            response.close()
        time.sleep(min(2 ** attempt, 10))
    raise AssertionError("Unreachable retry state")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def checked(response):
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code}; request {response.headers.get('x-request-id')}; {response.text[:2000]}")
    return response.json()


def main(*, require_qa=True):
    parser = argparse.ArgumentParser(description=__doc__)
    if require_qa:
        parser.add_argument("--qa-env", type=Path, required=True)
    else:
        parser.add_argument("--api-key-env", default="SCIENTIFIC_MODELS_API_KEY")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--model-id", choices=("scvi-scanvi", "gromacs"), default="scvi-scanvi",
                        help="Reuse the same QA-only replay/poll/artifact checks for mixed-workload regression")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--operation-id")
    parser.add_argument("--cohort", default="a")
    parser.add_argument("--idempotency-key", help="Explicit stable key when replaying a previously recorded qualification")
    parser.add_argument("--protocol", choices=("rest", "mcp"), default="rest")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--cancel", action="store_true", help="Cancel this explicitly selected/new operation and verify final cancellation")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--admission-wait-seconds", type=int, default=900,
                        help="Wait for explicit key-capacity non-admission, preserving the request and idempotency key")
    args = parser.parse_args()
    if args.admission_wait_seconds < 0:
        parser.error('Admission wait must be non-negative')
    submit_tool = {"scvi-scanvi": "submit_scvi_scanvi", "gromacs": "submit_gromacs_workflow"}[args.model_id]
    os.umask(0o077)
    if require_qa:
        env = dict(line.split("=", 1) for line in args.qa_env.read_text().splitlines() if "=" in line)
        key = env["SCIENTIFIC_MODELS_API_KEY"]
    else:
        key = os.environ.get(args.api_key_env)
        if not key:
            parser.error(f"Set the API key in environment variable {args.api_key_env}")
    with httpx.Client(base_url=args.origin, headers={"Authorization": "Bearer " + key},
                      timeout=httpx.Timeout(300, connect=15), trust_env=False) as client:
        me = checked(request_with_retry(client, "GET", "/v1/me"))
        if require_qa and (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Internal qualification requires system/qa")
        save(args.output / "caller.json", me)
        counter = 0
        session = {}

        def rpc(method, params):
            nonlocal counter
            counter += 1
            # Every caller below is discovery, a read, cancellation or a submit
            # with a stable idempotency key. Keep the same RPC id/body on retries.
            response = request_with_retry(client, "POST", "/mcp", retryable=True,
                                   headers={"Accept": "application/json, text/event-stream", **session},
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

        def tool(name, arguments, *, admission=False):
            value = rpc("tools/call", {"name": name, "arguments": arguments})
            return tool_result(value, admission=admission)

        if args.protocol == "mcp":
            rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "fs2-internal-scientific-qualification", "version": "1"}})
            response = request_with_retry(client, "POST", "/mcp", retryable=True,
                                   headers={"Accept": "application/json, text/event-stream", **session},
                                   json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            response.raise_for_status()
            tools = rpc("tools/list", {})
            selected = [item for item in tools["tools"] if item["name"] == submit_tool]
            if len(selected) != 1 or "parameters" not in selected[0]["inputSchema"]["properties"]:
                raise ValueError(f"Typed {submit_tool} tool is absent")
            save(args.output / "mcp-tool.json", selected[0])
        if args.discover_only:
            print(json.dumps({"discovery": "passed", "protocol": args.protocol}))
            return
        operation_id = args.operation_id
        if args.request:
            body = json.loads(args.request.read_text())
            # Preserve existing single-cell replay keys across this extension.
            prefix = "scvi" if args.model_id == "scvi-scanvi" else args.model_id
            key = args.idempotency_key or prefix + "-hosted-20261006-" + args.cohort
            def submit():
                if args.protocol == "mcp":
                    return tool(submit_tool, {**body, "idempotency_key": key}, admission=True)
                response = request_with_retry(client, "POST", f"/v1/models/{args.model_id}:submit",
                               retryable=True, json=body, headers={"Idempotency-Key": key})
                if response.status_code == 429:
                    try:
                        error = response.json().get('error', {})
                    except (ValueError, AttributeError):
                        error = {}
                    if capacity_rejection(error):
                        raise AdmissionCapacityError(error)
                return checked(response)
            admitted = submit_with_capacity_wait(submit, args.admission_wait_seconds)
            save(args.output / "admission.json", admitted)
            operation_id = admitted["operation"]["id"]
            replay = submit()
            save(args.output / "replay.json", replay)
            if replay["operation"]["id"] != operation_id or not replay["operation"]["reused"]:
                raise ValueError("Idempotency replay launched different work")
        if not operation_id:
            raise ValueError("Supply an existing operation or a finalized request")
        if args.cancel:
            cancelled = (tool("cancel_scientific_run", {"operation_id": operation_id}) if args.protocol == "mcp"
                         else checked(request_with_retry(client, "POST", f"/v1/operations/{operation_id}:cancel",
                                      retryable=True)))
            save(args.output / "cancellation.json", cancelled)
        deadline = time.monotonic() + args.timeout
        previous = None
        while time.monotonic() < deadline:
            status = (tool("get_scientific_status", {"operation_id": operation_id}) if args.protocol == "mcp"
                      else checked(request_with_retry(client, "GET", f"/v1/operations/{operation_id}")))
            save(args.output / "status.json", status)
            state = status["batch"]["status"] if "batch" in status else status["status"]
            if state != previous:
                print(json.dumps({"operation_id": operation_id, "protocol": args.protocol, "status": state}), flush=True)
                previous = state
            published = status["batch"]["result_published"] if "batch" in status else status["result_available"]
            if state in {"succeeded", "failed", "cancelled"} and published:
                if args.protocol == "mcp":
                    generic = tool("get_operation", {"operation_id": operation_id})
                    save(args.output / "generic-operation.json", generic)
                    if not generic["result_available"]:
                        raise ValueError("Generic MCP polling incorrectly hides the published batch result")
                break
            time.sleep(10)
        else:
            raise TimeoutError("Operation retained; do not duplicate work")
        result = (tool("get_scientific_result", {"operation_id": operation_id}) if args.protocol == "mcp"
                  else checked(request_with_retry(client, "GET", f"/v1/operations/{operation_id}/result")))
        save(args.output / "result.json", result)
        if args.protocol == "mcp":
            generic_result = tool("get_operation_result", {"operation_id": operation_id})
            save(args.output / "generic-result.json", generic_result)
            if generic_result != result:
                raise ValueError("Generic and scientific MCP result tools disagree")

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
            # Reconstruct human-readable filenames using the immutable worker
            # inventory, after every artifact has passed its hash/size checks.
            worker_entry = next((entry for entry in manifest["entries"] if entry["name"] == "result"), None)
            if worker_entry and worker_entry["semantic_type"] == "scvi-workflow-result/v1":
                worker_result = json.loads((args.output / "artifacts" / worker_entry["artifact"]["artifact_id"]).read_text())
                save(args.output / "worker-result.json", worker_result)
                by_name = {entry["name"]: entry["artifact"] for entry in manifest["entries"]}
                for index, item in enumerate(worker_result["files"]):
                    relative = Path(item["path"])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("Invalid path in the worker inventory")
                    pointer = by_name[f"file-{index:05d}"]
                    if pointer["sha256"] != item["sha256"] or pointer["size_bytes"] != item["size_bytes"]:
                        raise ValueError("Worker inventory differs from the verified artifact manifest")
                    destination = args.output / "data" / relative
                    if not destination.resolve().is_relative_to((args.output / "data").resolve()):
                        raise ValueError("Output path escapes the chosen directory")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(args.output / "artifacts" / pointer["artifact_id"], destination)
        # Keep transport failures/results visible even when the run failed.
        expected = "cancelled" if args.cancel else "succeeded"
        if state != expected:
            raise ValueError(f"Hosted operation ended {state}; inspect the retained result")
        print(json.dumps({"operation_id": operation_id, "status": state, "result_saved": True}), flush=True)


if __name__ == "__main__":
    main()

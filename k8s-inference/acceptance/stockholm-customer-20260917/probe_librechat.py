#!/usr/bin/env python3
"""Two bounded actual saved-agent turns; explicitly NOT canary-policy evidence.

Uses the hosted build's existing seeded login and server-managed gateway key.
No client setting, agent definition or key is changed. Only fresh conversations
are created. --execute is mandatory; run only after release-owner approval.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from collect_live import check, digest, kube, now, write_private
from run_live import MODELS, portable_payloads, semantic


def operations(value):
    found = set()
    if isinstance(value, str):
        try:
            return operations(json.loads(value))
        except (ValueError, TypeError, RecursionError):
            return found
    if isinstance(value, list):
        for item in value:
            found.update(operations(item))
    if isinstance(value, dict):
        if value.get("model_id") in MODELS and value.get("status") and value.get("id"):
            try:
                found.add(str(UUID(value["id"])))
            except ValueError:
                pass
        for item in value.values():
            found.update(operations(item))
    return found


def objects(value):
    if isinstance(value, str):
        try:
            yield from objects(json.loads(value))
        except (ValueError, TypeError, RecursionError):
            return
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)


def take_stream(response, events, deadline):
    check(response.status_code == 200, "librechat_turn_failed")
    if "application/json" in response.headers.get("content-type", ""):
        value = response.read()
        check(len(value) <= 1024 * 1024, "client_start_response_too_large")
        event = json.loads(value)
        events.append(event)
        return event.get("streamId")
    check("text/event-stream" in response.headers.get("content-type", ""), "client_stream_not_sse")
    size = 0
    for line in response.iter_lines():
        check(time.monotonic() < deadline, "client_turn_timeout")
        size += len(line)
        check(size <= 8 * 1024 * 1024, "client_stream_bound_exceeded")
        if not line.startswith("data:"):
            continue
        raw = line.removeprefix("data:").strip()
        if raw == "[DONE]":
            break
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        events.append(value)
        if value.get("final") is True:
            break
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    response = subprocess.run([
        "/usr/local/bin/nebius", "--profile", args.profile, "--timeout", "25s", "--auth-timeout", "25s",
        "--no-browser", "--no-check-update", "ai", "endpoint", "get", args.endpoint_id, "--format", "json",
    ], capture_output=True, text=True, timeout=40, check=False)
    check(response.returncode == 0, "endpoint_read_failed")
    endpoint = json.loads(response.stdout)
    environment = {row["name"]: row.get("value") for row in endpoint["spec"]["environment_variables"]}
    # The hosted gateway credential is a MysteryBox reference. Do not resolve
    # or copy it: the real saved agent uses its existing connection naturally.
    check("SCIENTIFIC_MODELS_API_KEY" in environment, "server_managed_key_not_configured")
    origin = next(url for url in endpoint["status"]["public_endpoints"] if url.startswith("https://"))
    script = '''
import json, os, pathlib, urllib.request
from urllib.parse import urlsplit
secret=pathlib.Path(os.environ['FS2_ADMIN_TOKEN_FILE']).read_text().strip()
request=urllib.request.Request('http://127.0.0.1:8080/admin/v1/tokens',
 headers={'Authorization':'Bearer '+secret,'Host':urlsplit(os.environ['FS2_PUBLIC_BASE_URL']).netloc})
with urllib.request.urlopen(request,timeout=15) as response:
 rows=json.load(response)
fields=('id','principal_id','tenant_id','scopes','models','max_concurrency','request_budget',
 'gpu_seconds_budget','rate_limit_requests','rate_window_seconds','expires_at','revoked_at','fingerprint')
print(json.dumps([{k:r.get(k) for k in fields} for r in rows if r.get('id')==TOKEN_ID]))
'''
    report = {"scope": "actual-hosted-librechat-existing-principal-not-stockholm-canary",
              "customer_ready": False, "endpoint_id": args.endpoint_id,
              "image_reference": endpoint["spec"]["image"], "caller_policy": None,
              "started_at": now(), "turns": [], "outcome": "failed"}
    secrets = tuple(value for name, value in environment.items() if isinstance(value, str) and value
                    and (name.endswith("_API_KEY") or name == "SEED_DEFAULT_USER_PASSWORD"))
    try:
        with httpx.Client(base_url=origin, timeout=90, trust_env=False, follow_redirects=False) as client:
            login = client.post("/api/auth/login", json={"email": environment["SEED_DEFAULT_USER_EMAIL"],
                                                         "password": environment["SEED_DEFAULT_USER_PASSWORD"]})
            check(login.status_code == 200, "client_login_failed")
            session = login.json()["token"]
            secrets += (session,)
            client.headers["Authorization"] = "Bearer " + session
            for path in ("/api/agents/agent_protein_structure", "/api/agents/v1/agent_protein_structure"):
                agent = client.get(path)
                if agent.status_code == 200 and "application/json" in agent.headers.get("content-type", ""):
                    break
            check(agent.status_code == 200 and "application/json" in agent.headers.get("content-type", ""),
                  "saved_protein_agent_unavailable")
            report["saved_agent_path"] = path
            report["saved_agent_sha256"] = digest(agent.json())
            for scenario in ("named", "generic"):
                payload = portable_payloads("openfold2")[0]
                request_id = "stockholm-client-" + str(uuid4())
                arguments = payload | {"idempotency_key": request_id, "wait_seconds": 0}
                tool = "infer_openfold2_native_mcp_bionemo-models"
                if scenario == "generic":
                    arguments = {"model_id": "openfold2", "protocol": "native", "payload": payload,
                                 "idempotency_key": request_id, "wait_seconds": 0}
                    tool = "invoke_model_mcp_bionemo-models"
                conversation = str(uuid4())
                prompt = (
                    "This one bounded synthetic protein acceptance request is explicitly approved. "
                    "Load the installed scientific-gateway and openfold2 skills first. Do not run any other model, "
                    "browse the web, write files, or change settings. Call " + tool + " with exactly this JSON: "
                    + json.dumps(arguments) + ". Replay the identical tool call once with the identical idempotency key; "
                    "confirm the same operation ID. Poll that operation only until terminal, then retrieve its result. "
                    "Do not submit another operation. Reply with only the operation ID, status, replay identity and "
                    "a short structure summary; do not print the full PDB. If a tool fails, report it and stop."
                )
                body = {"endpoint": "agents", "agent_id": "agent_protein_structure", "text": prompt,
                        "conversationId": conversation, "parentMessageId": "00000000-0000-0000-0000-000000000000",
                        "messageId": str(uuid4()), "isContinued": False, "isRegenerate": False}
                row = {"scenario": scenario, "conversation_id": conversation, "started_at": now(),
                       "expected_tool": tool, "arguments_sha256": digest(arguments), "idempotency_key": request_id}
                report["turns"].append(row)
                write_private(args.output / (scenario + "-request.json"), body, secrets)
                events = []
                deadline = time.monotonic() + 900
                with client.stream("POST", "/api/agents/chat", json=body) as response:
                    stream = take_stream(response, events, deadline)
                if stream:
                    check(str(UUID(stream)) == conversation, "unexpected_conversation_stream")
                    with client.stream("GET", "/api/agents/chat/stream/" + stream) as response:
                        take_stream(response, events, deadline)
                write_private(args.output / (scenario + "-events.json"), events, secrets)
                messages = client.get("/api/messages/" + conversation)
                check(messages.status_code == 200, "saved_messages_unavailable")
                saved = messages.json()
                write_private(args.output / (scenario + "-messages.json"), saved, secrets)
                op_ids = operations([events, saved])
                row["operation_ids"] = sorted(op_ids)
                check(len(op_ids) == 1, "actual_client_operation_correlation_missing_or_extra")
                op_id = next(iter(op_ids))
                observed = list(objects([events, saved]))
                terminal = [value for value in observed if value.get("id") == op_id
                            and value.get("status") == "succeeded" and value.get("model_id") == "openfold2"]
                check(terminal, "client_did_not_observe_terminal_operation")
                op = terminal[-1]
                row["operation"] = op
                check(op["status"] == "succeeded" and op["idempotency_key"] == request_id,
                      "client_operation_not_terminal_or_changed_request")
                results = [value["result"] for value in observed if isinstance(value.get("result"), dict)
                           and isinstance(value.get("operation"), dict) and value["operation"].get("id") == op_id]
                check(results, "client_did_not_retrieve_result")
                result = results[-1]
                semantic("openfold2", payload, result)
                policies = kube(args.kubeconfig, args.context, "-n", "fs2-system", "exec", "-i",
                                "deploy/fs2-serve-control-plane", "-c", "control-plane", "--", "python", "-",
                                script=script.replace("TOKEN_ID", repr(str(UUID(op["token_id"])))) )
                check(len(policies) == 1, "existing_client_policy_unresolved")
                report["caller_policy"] = policies[0]
                row.update(semantic_validated=True, result_sha256=digest(result), completed_at=now())
                print(json.dumps({"scenario": scenario, "operation_id": op_id,
                                  "scope": "existing-client-principal-only"}), flush=True)
            report["outcome"] = "existing_client_paths_completed"
    except Exception as error:
        report.update(failure_type=type(error).__name__, failure_code=getattr(error, "code", None))
    report["completed_at"] = now()
    write_private(args.output / "receipt.json", report, secrets)
    print(json.dumps({"outcome": report["outcome"], "output": str(args.output), "customer_ready": False}))
    return 0 if report["outcome"] == "existing_client_paths_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

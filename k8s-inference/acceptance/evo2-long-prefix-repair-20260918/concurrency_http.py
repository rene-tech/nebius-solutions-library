"""Isolated live proof: two GPU calls serialize, duplicate IDs never execute.

Run after the 96-case replay has been archived, then retain the two additional
server-memory records separately. This deliberately uses the internal model
HTTP endpoint; customer idempotency/billing is qualified at the control plane.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid

from replay_http import evaluate


def post(base, case, request_id):
    started = time.time()
    result = {"case_id": case["case_id"], "request_id": request_id,
              "started_unix_seconds": started, "status": None, "evaluation": None}
    request = urllib.request.Request(base + "/biology/arc/evo2/generate",
        data=json.dumps(case["arguments"]).encode(),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id})
    try:
        try:
            response = urllib.request.urlopen(request, timeout=180)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result.update(status=response.status, response=json.loads(response.read()))
        if result["status"] == 200:
            result["evaluation"] = evaluate(case, result["response"])
    except Exception as error:
        result["failure"] = {"type": type(error).__name__, "detail": str(error)}
    result["finished_unix_seconds"] = time.time()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    cases = [case for case in json.loads(args.cases.read_text())["cases"] if case["model_id"] == "evo2-40b"]
    long_case = next(case for case in cases if len(case["arguments"]["sequence"]) == 8192)
    short_case = next(case for case in cases if len(case["arguments"]["sequence"]) == 256)
    base, request_id = args.base_url.rstrip("/"), str(uuid.uuid4())
    probes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as clients:
        original = clients.submit(post, base, long_case, request_id)
        time.sleep(1)
        if original.done():
            args.output.write_text(json.dumps({"passed": False, "long": original.result(),
                "failure": "long request completed before concurrent check; overlap was not exercised"}, indent=2) + "\n")
            raise RuntimeError("long request already completed; concurrent-health test not exercised")
        duplicate = clients.submit(post, base, long_case, request_id)
        short = clients.submit(post, base, short_case, str(uuid.uuid4()))
        while not all(future.done() for future in (original, duplicate, short)):
            before = time.perf_counter()
            observation = {"started_unix_seconds": time.time(), "passed": False}
            try:
                with urllib.request.urlopen(base + "/v1/health/ready", timeout=3) as response:
                    observation.update(status=response.status, passed=response.status == 200)
            except Exception as error:
                observation["error"] = {"type": type(error).__name__, "detail": str(error)}
            observation["wall_seconds"] = time.perf_counter() - before
            probes.append(observation)
            time.sleep(max(0, 5 - observation["wall_seconds"]))
        results = {"long": original.result(), "duplicate": duplicate.result(), "short": short.result()}
    passed = ((results["long"]["evaluation"] or {}).get("semantic_pass", False)
              and (results["short"]["evaluation"] or {}).get("semantic_pass", False)
              and results["duplicate"]["status"] == 409 and len(probes) >= 3
              and all(probe["passed"] for probe in probes))
    report = {"passed": passed, "responses": results, "health_observations": probes,
              "server_followup": "Verify exactly two extra memory receipts, active_model_requests_at_start=1 and no interval overlap."}
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"passed": passed, "health_observations": len(probes)}), flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()

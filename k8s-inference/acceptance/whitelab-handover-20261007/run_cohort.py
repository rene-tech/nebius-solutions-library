"""Real concurrent scVI/MD public-path qualification using existing QA only.

Reuses finalized public-data artifacts and the customer collect.py client.
No cloud scaling, customer keys, cancellation or implicit resubmission.
Each output directory/idempotency key is a durable replay boundary.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

SOLUTION = Path(__file__).resolve().parents[2]
CLIENT = SOLUTION / "models/visual-science/scvi-scanvi"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def token(path):
    if path.suffix == ".json":
        return json.loads(path.read_text())["secret"]
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)["SCIENTIFIC_MODELS_API_KEY"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("qa-key", "md-key", "routine-request", "atlas-request", "md-request", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--cohort", required=True)
    p.add_argument("--atlas-jobs", type=int, choices=(0, 4), default=0)
    p.add_argument("--origin", default="https://89.169.99.188")
    args = p.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    identities, keys = {}, {"scvi-scanvi": token(args.qa_key), "gromacs": token(args.md_key)}
    for model, key in keys.items():
        r = httpx.get(args.origin + "/v1/me", headers={"Authorization": "Bearer " + key}, timeout=30)
        r.raise_for_status()
        me = r.json()
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Customer credentials are forbidden for internal qualification")
        if model == "scvi-scanvi" and me["max_concurrency"] != 8:
            raise ValueError("This qualification expects an explicitly prepared eight-slot QA key")
        identities[model] = me
    save(args.output / "identities.json", identities)
    fixtures = {key: json.loads(path.read_text()) for key, path in (
        ("routine", args.routine_request), ("atlas", args.atlas_request), ("md", args.md_request))}
    running, completed, handles = {}, [], []
    for index in range(10):
        model = "scvi-scanvi" if index < 8 else "gromacs"
        shape = ("atlas" if index < args.atlas_jobs else "routine") if index < 8 else "md"
        body = copy.deepcopy(fixtures[shape])
        name = f"{args.cohort}-{shape}-{index:02d}"
        directory = args.output / name
        parameters = body["parameters"]
        parameters["output_prefix"] = f"runs/whitelab-final-20261007/{name}"
        if model == "scvi-scanvi":
            parameters.update(max_epochs=None, scanvi_max_epochs=20, seed=42 + index, visualization="none")
        # Preserve the MD fixture's scientifically tested 10k-step protocol.
        # Cohort A's arbitrary 100k extension hit a native excluded-pair cutoff
        # error in its free-energy system. Retain that failed evidence; do not
        # change scientific cutoffs or disguise it as a platform success.
        request = directory / "request.json"
        if request.exists() and json.loads(request.read_text()) != body:
            raise ValueError("Refusing to change an existing cohort request")
        save(request, body)
        command = [sys.executable, str(CLIENT / "collect.py"), "--origin", args.origin,
                   "--model-id", model, "--protocol", "mcp" if index % 2 else "rest",
                   "--request", str(request), "--output", str(directory),
                   "--idempotency-key", "whitelab-final-20261007-" + name, "--timeout", "10800"]
        log = (directory / "client.log").open("a")
        handles.append(log)
        env = {**os.environ, "SCIENTIFIC_MODELS_API_KEY": keys[model]}
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        running[name] = (process, directory, model)
    started = time.time()
    while running:
        sample = {"timestamp": time.time(), "elapsed_seconds": time.time() - started, "clients_running": len(running)}
        try:
            response = httpx.get(args.origin + "/readyz", timeout=15)
            sample.update(http_status=response.status_code, readiness=response.json())
        except (httpx.HTTPError, ValueError) as error:
            sample["observer_error"] = type(error).__name__
        ids = set()
        for directory in args.output.iterdir():
            admission = directory / "admission.json"
            if directory.is_dir() and admission.exists():
                ids.add(json.loads(admission.read_text())["operation"]["id"])
        try:
            raw = subprocess.check_output(["kubectl", "--context", args.context, "--request-timeout=15s",
                "-n", "fs2-models", "get", "pods", "-l", "fs2.nebius.ai/tenant-id=system", "-o", "json"], timeout=20)
            pods = []
            for pod in json.loads(raw)["items"]:
                labels = pod["metadata"].get("labels", {})
                if labels.get("fs2.nebius.ai/operation-id") in ids:
                    pods.append({"pod": pod["metadata"]["name"], "operation_id": labels["fs2.nebius.ai/operation-id"],
                                 "model": labels.get("fs2.nebius.ai/model-id"), "phase": pod["status"]["phase"],
                                 "node": pod["spec"].get("nodeName"), "containers": [
                                     {"name": c["name"], "state": c.get("state"), "ready": c.get("ready")}
                                     for c in pod["status"].get("containerStatuses", [])]})
            sample["pods"] = pods
        except (subprocess.SubprocessError, ValueError) as error:
            sample["kubernetes_observer_error"] = type(error).__name__
        with (args.output / "observations.jsonl").open("a") as stream:
            stream.write(json.dumps(sample) + "\n")
        for name, (process, directory, model) in list(running.items()):
            code = process.poll()
            if code is None:
                continue
            result = {"name": name, "model": model, "client_exit_code": code}
            if code == 0 and model == "scvi-scanvi":
                verified = subprocess.run([sys.executable, str(CLIENT / "qualify_outputs.py"),
                    "--result", str(directory / "worker-result.json"), "--data", str(directory / "data"),
                    "--output", str(directory / "output-validation.json")], capture_output=True, text=True)
                result["semantic_validation_exit_code"] = verified.returncode
                if verified.returncode:
                    result["validation_error"] = verified.stderr[-2000:]
            completed.append(result)
            del running[name]
            save(args.output / "completed.json", completed)
            print(json.dumps(result), flush=True)
        print(json.dumps({"cohort": args.cohort, "running": len(running), "completed": len(completed),
                          "ready_http": sample.get("http_status"), "observed_pods": len(sample.get("pods", []))}), flush=True)
        if running:
            time.sleep(15)
    for log in handles:
        log.close()
    if any(row["client_exit_code"] or row.get("semantic_validation_exit_code", 0) for row in completed):
        raise SystemExit("Cohort failed; retained operations must be inspected, not silently rerun")


if __name__ == "__main__":
    main()

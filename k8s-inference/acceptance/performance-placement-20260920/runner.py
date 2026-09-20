"""Restartable baseline worker using public APIs and existing semantic validators.

No Kubernetes mutation, pod port-forward, model scaling or GPU scheduling occurs
here. A trial represents its explicitly named input workload, not an invented
universal model latency. The registry is durable; files are temporary receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx

from measurements import collect

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "components/control-plane/src"), str(ROOT / "catalog/runtime"), str(ROOT / "models")]
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "preempted"}


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


PUBLIC = module("performance_scientific", ROOT / "acceptance/scientific-fleet/run_acceptance.py")
FLEET = module("performance_fleet", ROOT / "acceptance/scientific-fleet/run_fleet_acceptance.py")
BIO = module("performance_bio", ROOT / "acceptance/h100-fleet/bionemo-structure/public_verify.py")
FOLD2 = module("performance_fold2", ROOT / "acceptance/h100-fleet/openfold2-upstream/public_verify.py")
FOLD3 = module("performance_fold3", ROOT / "acceptance/h100-fleet/openfold3-standalone/public_verify.py")
INVENTORY = module("performance_inventory", HERE / "inventory.py")
COSMOS = module("performance_cosmos", ROOT / "catalog/runtime/validators/validate_cosmos3_nano.py")


class QueuedPublicClient(PUBLIC.PublicApiClient):
    """Wait on explicit pre-admission 429; never retry an ambiguous admission."""

    def __init__(self, origin, token, timeout):
        super().__init__(origin, token)
        self.admission_timeout = timeout

    def request(self, *args, **kwargs):
        deadline = time.monotonic() + self.admission_timeout
        while True:
            response = super().request(*args, **kwargs)
            if response.status != 429 or time.monotonic() >= deadline:
                return response
            time.sleep(15)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def recipes(inventory):
    """Freeze a complete denominator, including explicitly unavailable recipes."""
    scientific = {item.model_id: item for item in FLEET.discover_inputs(ROOT)}
    cases = []
    for item in inventory["data"]["items"]:
        model = item["model_id"]
        case = {"case_id": model, "model_id": model, "repetitions": 3,
                "workload_class": "unqualified", "adapter": "unavailable",
                "fixture_ref": "catalog:" + model, "fixture_sha256": sha(canonical(item)),
                "cache_condition": "uncontrolled"}
        if model in scientific:
            entry = scientific[model]
            case.update(adapter="scientific", fixture_ref=entry.relative_path,
                        fixture_sha256=entry.sha256, workload_class="committed-scientific-fixture")
        elif model in BIO.MODELS:
            contract, pair = BIO.cases_for(model)
            case.update(adapter="bio-pair", fixture_ref=model, workload_class="two-native-semantic-requests",
                        fixture_sha256=sha(canonical([record.payload for record in pair])))
        elif model in BIO.public.MODELS:
            contract, pair = BIO.public.cases_for(model)
            case.update(adapter="media-pair", fixture_ref=model, workload_class="two-media-semantic-requests",
                        fixture_sha256=sha(canonical([record.payload for record in pair])))
        elif model in {"openfold2", "openfold3"}:
            helper = FOLD2 if model == "openfold2" else FOLD3
            contract, pair = helper.cases_for(model)
            case.update(adapter=model, fixture_ref=model, workload_class="two-structure-semantic-requests",
                        fixture_sha256=sha(canonical([record.payload for record in pair])))
        elif model == "qwen3-8b":
            case.update(adapter="qwen", fixture_ref=model, workload_class="exact-content-two-prompts",
                        fixture_sha256=sha(canonical(qwen_requests())))
        elif model == "cosmos3-nano":
            fixture = ROOT / "catalog/runtime/validators/assets/cosmos3-nano.json"
            case.update(adapter="cosmos", fixture_ref=str(fixture.relative_to(ROOT)),
                        workload_class="two-256p-25-frame-videos", fixture_sha256=sha(fixture.read_bytes()))
        elif model == "phenoage":
            from aging.fixtures import clinical_payload
            case.update(adapter="phenoage", fixture_ref="models/aging/fixtures.py", workload_class="sixteen-synthetic-samples",
                        fixture_sha256=sha(canonical(clinical_payload(16))))
        else:
            case["unavailable_reason"] = (
                "GLM is outside this H100/L40S campaign (B300-only qualification)" if model == "glm-5-2-fp8"
                else "No benchmark adapter selected yet; not a successful or measured model"
            )
            case["repetitions"] = 1
        cases.append(case)
    return cases


def qwen_requests():
    # The current reasoning-aware parser needs a realistic completion budget.
    return [{"model": "qwen3-8b", "messages": [{"role": "user", "content": f"Reply with {word} only. /no_think"}],
             "max_completion_tokens": 1024, "temperature": 0} for word in ("TELESCOPE", "MICROSCOPE")]


def checked(response):
    if response.is_error:
        raise RuntimeError(f"http_{response.status_code}")
    return response.json()


def prior_access_denial(admin, campaign_id, trial):
    """Do not repeat a denied request in the same immutable input cohort."""
    campaign = checked(admin.get(f"/admin/api/v1/performance/campaigns/{campaign_id}"))["data"]
    for previous in campaign["trials"]:
        if (previous["case_id"] == trial["case_id"] and previous["id"] != trial["id"]
                and (previous.get("result") or {}).get("error_code") in {"http_401", "http_403"}):
            return previous["id"]
    return None


def invoke(client, model, protocol, operation, payload, key, directory, timeout):
    started = time.monotonic()
    path = "/v1/chat/completions" if protocol == "openai-chat" else f"/v1/models/{model}:invoke"
    body = payload if protocol == "openai-chat" else {"operation": operation, "payload": payload}
    while True:
        response = client.post(path, json=body, headers={
            "Idempotency-Key": key, "x-fs2-wait-seconds": "0", "x-fs2-deadline-seconds": str(timeout),
        })
        if response.status_code != 429 or time.monotonic() - started >= timeout:
            break
        time.sleep(15)
    admitted = checked(response)
    operation_id = response.headers.get("x-fs2-operation-id") or admitted.get("id")
    if not operation_id:
        raise RuntimeError("missing_operation_id")
    (directory / (operation_id + "-admission.json")).write_bytes(canonical(admitted))
    # Durable public idempotency is stable across worker lease recovery.
    while True:
        status = checked(client.get(f"/v1/operations/{operation_id}"))
        if status["status"] in TERMINAL:
            break
        if time.monotonic() - started >= timeout:
            # Do not submit another operation or label still-running work successful.
            raise RuntimeError("operation_wait_deadline")
        time.sleep(3)
    (directory / (operation_id + "-status.json")).write_bytes(canonical(status))
    if status["status"] != "succeeded":
        raise RuntimeError("operation_" + status["status"])
    response = client.get(f"/v1/operations/{operation_id}/result")
    if response.is_error:
        raise RuntimeError(f"result_http_{response.status_code}")
    path = directory / (operation_id + "-result.json")
    raw = response.content
    if raw.startswith(b"{"):
        value = response.json()
        if value.get("schema") == "fs2-serve.nebius.ai/operation-artifact-result/v1":
            artifact = value["artifact"]
            materialized = client.get(f"/v1/artifacts/{artifact['artifact_id']}/content")
            materialized.raise_for_status()
            if len(materialized.content) != artifact["size_bytes"] or sha(materialized.content) != artifact["sha256"]:
                raise RuntimeError("result_artifact_identity_mismatch")
            (directory / (operation_id + "-envelope.json")).write_bytes(raw)
            raw = materialized.content
    path.write_bytes(raw)
    return path, status, time.monotonic() - started


def execute(trial, origin, token, directory, timeout):
    case = trial["case_spec"]
    model, adapter = case["model_id"], case["adapter"]
    started = time.monotonic()
    if adapter == "scientific":
        fragment = ROOT / case["fixture_ref"]
        if sha(fragment.read_bytes()) != case["fixture_sha256"]:
            raise RuntimeError("fixture_digest_changed")
        config = PUBLIC.RunConfig(endpoint=origin, repository_root=ROOT,
                                  activation_fragment=fragment, receipt_path=directory / "scientific.json",
                                  run_id=str(trial["id"]), timeout_seconds=timeout, overwrite=True)
        receipt = PUBLIC.run_acceptance(config, QueuedPublicClient(origin, token, timeout))
        return {"operation_id": receipt["operation_identity"]["operation_id"],
                "elapsed_seconds": time.monotonic() - started, "semantic_valid": True,
                "scientific_receipt": receipt}
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + token}, timeout=180, trust_env=False) as client:
        if adapter == "artifact-lerobot-v1":
            from robotics_workflow import execute as execute_lerobot
            return execute_lerobot(trial, client, token, directory, timeout)
        elif adapter == "artifact-native-v1":
            from extra import execute as execute_extra
            calls, semantic = execute_extra(trial, client, directory, invoke, timeout)
        elif adapter in {"bio-pair", "media-pair", "openfold2", "openfold3"}:
            helper = {"bio-pair": BIO, "media-pair": BIO.public, "openfold2": FOLD2, "openfold3": FOLD3}[adapter]
            contract, pair = helper.cases_for(model)
            if sha(canonical([record.payload for record in pair])) != case["fixture_sha256"]:
                raise RuntimeError("fixture_digest_changed")
            calls = [invoke(client, model, record.protocol, record.operation, record.payload,
                            f"benchmark-{trial['id']}-{index}", directory, timeout) for index, record in enumerate(pair)]
            semantic = helper.validate_pair(model, contract, [row[0] for row in calls], directory)
        elif adapter == "qwen":
            payloads = qwen_requests()
            if sha(canonical(payloads)) != case["fixture_sha256"]:
                raise RuntimeError("fixture_digest_changed")
            calls = [invoke(client, model, "openai-chat", "chat", payload, f"benchmark-{trial['id']}-{index}",
                            directory, timeout) for index, payload in enumerate(payloads)]
            for row, expected in zip(calls, ("TELESCOPE", "MICROSCOPE"), strict=True):
                response = json.loads(row[0].read_bytes())
                content = response["choices"][0]["message"]["content"].strip().strip(".!")
                if content != expected:
                    raise RuntimeError("qwen_exact_content_mismatch")
            semantic = {"status": "PASS", "contract": "exact-final-content"}
        elif adapter == "cosmos":
            fixture = ROOT / case["fixture_ref"]
            if sha(fixture.read_bytes()) != case["fixture_sha256"]:
                raise RuntimeError("fixture_digest_changed")
            contract = COSMOS.load_contract(fixture)
            calls = [invoke(client, model, "native", "generate-media", record["request"],
                            f"benchmark-{trial['id']}-{index}", directory, timeout)
                     for index, record in enumerate(contract["requests"])]
            semantic = COSMOS.validate(contract, [row[0] for row in calls])
        elif adapter == "phenoage":
            from aging.fixtures import clinical_payload
            from aging.contracts import ClinicalRequest
            from aging.phenoage.runtime import ClinicalPhenoAgeRuntime
            payload = clinical_payload(16)
            if sha(canonical(payload)) != case["fixture_sha256"]:
                raise RuntimeError("fixture_digest_changed")
            calls = [invoke(client, model, "native", "predict-age", payload, f"benchmark-{trial['id']}-0", directory, timeout)]
            actual = json.loads(calls[0][0].read_bytes())
            expected = ClinicalPhenoAgeRuntime().predict(ClinicalRequest.model_validate(payload))
            if actual.get("model_id") != model or actual.get("sample_count") != 16:
                raise RuntimeError("phenoage_response_identity_mismatch")
            for prediction, reference in zip(actual["predictions"], expected, strict=True):
                if prediction["sample_id"] != reference["sample_id"] or not math.isclose(
                    prediction["phenotypic_age_years"], reference["phenotypic_age_years"], rel_tol=0, abs_tol=1e-10
                ):
                    raise RuntimeError("phenoage_reference_mismatch")
            semantic = {"status": "PASS", "reference": "levine-2018-supplement-rounded-v1", "samples": 16}
        else:
            raise RuntimeError("benchmark_adapter_unavailable")
    return {"operation_id": calls[0][1]["id"], "semantic_valid": True,
            "elapsed_seconds": time.monotonic() - started, "request_count": len(calls),
            "operations": [row[1] for row in calls], "request_seconds": [row[2] for row in calls], "semantic": semantic}


def worker(args, admin, campaign_id, token, *, once=False):
    while True:
        reply = checked(admin.post(f"/admin/api/v1/performance/campaigns/{campaign_id}/claim",
                                   json={"worker": args.worker, "lease_seconds": 120}))["data"]
        trial = reply["trial"]
        if trial is None:
            if once:
                return False
            campaign = checked(admin.get(f"/admin/api/v1/performance/campaigns/{campaign_id}"))["data"]
            if all(item["status"] in TERMINAL | {"unsupported", "capacity-unavailable"} for item in campaign["trials"]):
                return
            time.sleep(10)
            continue
        directory = args.directory / str(trial["id"])
        directory.mkdir(exist_ok=True)
        stop = threading.Event()
        lease_lost = threading.Event()
        lease = {"worker": args.worker, "fence": trial["fence"]}

        def renew():
            while not stop.wait(25):
                try:
                    checked(admin.post(f"/admin/api/v1/performance/trials/{trial['id']}/heartbeat", json=lease))
                except Exception:
                    lease_lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            print(json.dumps({"event": "trial_started", "model": trial["model_id"], "trial": trial["id"]}), flush=True)
            started = time.monotonic()
            try:
                denied = prior_access_denial(admin, campaign_id, trial)
                if denied:
                    evidence = {"error_code": "access_denied_not_retried", "blocked_by_trial": denied,
                                "model_request_submitted": False, "elapsed_seconds": None}
                    result = {**lease, "status": "unsupported", "semantic_valid": False,
                              "error_code": "access_denied_not_retried", "elapsed_seconds": None}
                else:
                    evidence = execute(trial, args.origin, token, directory, args.timeout)
                    result = {**lease, "status": "succeeded", "semantic_valid": True,
                              "operation_id": evidence["operation_id"],
                              "elapsed_seconds": evidence["elapsed_seconds"] if trial["fence"] == 1 else None}
                    metrics, observations = collect(admin, evidence, trial["model_id"])
                    result.update(metrics)
                    evidence["observations"] = observations
            except Exception as error:
                code = str(error) if isinstance(error, (RuntimeError, PUBLIC.AcceptanceError)) else type(error).__name__
                # Safe summaries only; request payloads, signed URLs and credentials never enter the registry.
                code = code if code.replace("_", "").isalnum() and len(code) <= 128 else "benchmark_validation_failed"
                evidence = {"error_code": code, "elapsed_seconds": time.monotonic() - started}
                statuses = [json.loads(path.read_bytes()) for path in sorted(directory.glob("*-status.json"))]
                evidence["operations"] = statuses
                result = {**lease, "status": "failed", "error_code": code,
                          "elapsed_seconds": evidence["elapsed_seconds"] if trial["fence"] == 1 else None,
                          "operation_id": statuses[0]["id"] if statuses else None}
            data = canonical({"trial_id": trial["id"], "case": trial["case_spec"],
                              "worker_source_commit": args.commit, "result": evidence})
            (directory / "receipt.json").write_bytes(data)
            # Receipt upload is also subject to this key's concurrency cap.
            # Keep the lease and evidence while other model operations finish.
            deadline = time.monotonic() + args.timeout
            while True:
                try:
                    artifact = PUBLIC._upload(PUBLIC.PublicApiClient(args.origin, token), model_id=trial["model_id"],
                                             data=data, media_type="application/json", compression="none",
                                             idempotency_key=f"benchmark-receipt-{trial['id']}-{sha(data)}")
                    break
                except PUBLIC.AcceptanceError as error:
                    if str(error) != "http_upload_begin_429" or time.monotonic() >= deadline or lease_lost.is_set():
                        raise
                    time.sleep(15)
            result.update(receipt_sha256=sha(data), artifact_uri="artifact://" + artifact["artifact_id"])
            if lease_lost.is_set():
                raise RuntimeError("benchmark_lease_lost")
            checked(admin.post(f"/admin/api/v1/performance/trials/{trial['id']}/result", json=result))
            print(json.dumps({"event": "trial_finished", "model": trial["model_id"], "trial": trial["id"],
                              "status": result["status"], "seconds": result["elapsed_seconds"]}), flush=True)
        finally:
            stop.set()
            thread.join(timeout=5)
        if once:
            return True


def pool(args):
    """A fixed CPU worker pool, using database claims and public idempotency.

    Login is renewed between trials. A process/container restart recovers work
    through the expired lease; it does not own or kill the GPU operation.
    """
    last_campaign = None
    while True:
        worked = False
        with closing(INVENTORY.admin_client(args.kubeconfig, args.context, args.origin)) as admin:
            campaigns = checked(admin.get("/admin/api/v1/performance/campaigns", params={"limit": 200}))["data"]["items"]
            campaigns.reverse()
            # Round-robin campaigns; the registry already rounds repetitions.
            # A new speech/media campaign must not wait behind a long DAG fleet.
            previous = next((i for i, c in enumerate(campaigns) if c["id"] == last_campaign), None)
            if previous is not None:
                campaigns = campaigns[previous + 1:] + campaigns[:previous + 1]
            for campaign in campaigns:
                # The pod count bounds concurrency across campaigns as well.
                if worker(args, admin, campaign["id"], args.token_file.read_text().strip(), once=True):
                    worked = True
                    last_campaign = campaign["id"]
                    break
        if not worked:
            time.sleep(15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "create", "work", "pool"])
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--kubeconfig")
    parser.add_argument("--context")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--cases", type=Path, help="Optional immutable prepared-fixture case list")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--name", default="full-catalog-baseline-20260920")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--worker", default="benchmark-worker-1")
    parser.add_argument("--campaign-id")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--max-parallel", type=int, default=4)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.action == "pool":
        pool(args)
        return
    if args.action in {"plan", "create"}:
        inventory = json.loads(args.inventory.read_bytes())
        spec = {"name": args.name, "source_commit": args.commit, "catalog_sha256": sha(canonical(inventory)),
                "max_parallel": args.max_parallel, "cases": json.loads(args.cases.read_bytes()) if args.cases else recipes(inventory)}
        (args.directory / "campaign-plan.json").write_bytes(canonical(spec))
        if args.action == "plan":
            print(json.dumps({"models": len(spec["cases"]), "executable": sum(not c.get("unavailable_reason") for c in spec["cases"]),
                              "trials": sum(c["repetitions"] for c in spec["cases"])}))
            return
    with closing(INVENTORY.admin_client(args.kubeconfig, args.context, args.origin)) as admin:
        if args.action == "create":
            created = checked(admin.post("/admin/api/v1/performance/campaigns", json=spec))["data"]
            (args.directory / "campaign.json").write_bytes(canonical(created))
            print(json.dumps({"campaign_id": created["id"]}))
        else:
            worker(args, admin, args.campaign_id, args.token_file.read_text().strip())


if __name__ == "__main__":
    main()

"""Export payload-free, public-safe qualification evidence from retained QA receipts."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from manage_release import MODELS, ROOT


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(directory, cohort_ids, cold_cohort, backend_image, backend_source):
    cohorts = []
    for cohort in cohort_ids:
        path = directory / ("cohort-" + cohort) / "cohort.json"
        value = read(path)
        assert value["passed"] and len(value["receipts"]) == 13
        cases = []
        for row in value["receipts"]:
            assert row["passed"] and row["idempotency_passed"]
            case = {
                key: row.get(key)
                for key in (
                    "model",
                    "case",
                    "path",
                    "operation_id",
                    "expected_count",
                    "expected_failure",
                    "passed",
                    "idempotency_passed",
                    "elapsed_seconds",
                    "status_transitions",
                    "reference_max_absolute_difference",
                    "artifact_output",
                )
            }
            case["operation"] = {
                key: row["operation"].get(key)
                for key in (
                    "id",
                    "status",
                    "tenant_id",
                    "principal_id",
                    "protocol",
                    "attempt",
                    "accepted_at",
                    "activation_started_at",
                    "started_at",
                    "ready_at",
                    "completed_at",
                    "cold_start_seconds",
                    "error_code",
                    "error_detail",
                    "http_status",
                    "estimated_gpu_seconds",
                    "runtime",
                )
            }
            assert case["operation"]["tenant_id"] == "system"
            assert case["operation"]["estimated_gpu_seconds"] == 0
            if row.get("result_artifact"):
                case["result_artifact"] = {
                    key: row["result_artifact"].get(key)
                    for key in ("artifact_id", "sha256", "size_bytes", "media_type")
                }
            cases.append(case)
        invalid = {}
        for model in MODELS:
            bad = read(path.parent / f"{model}-invalid-contract.json")
            assert bad["status"] in (400, 422)
            invalid[model] = bad["status"]
        cohorts.append(
            {
                "id": cohort,
                "passed": True,
                "raw_receipt_sha256": sha(path),
                "cases": cases,
                "invalid_contract_http_status": invalid,
            }
        )
    cold = read(directory / ("cohort-" + cold_cohort) / "cold-prerequisite.json")[-1]
    assert not cold["pods"] and cold["deployments"] == {model: 0 for model in MODELS}
    samples = read(directory / "cluster-observations-r2.json")
    replicas = [
        {
            "at": row["at"],
            "models": {
                item["name"]: {
                    "desired": item["spec"]["replicas"],
                    "ready": item["status"].get("readyReplicas", 0),
                }
                for item in row["deployments"]
            },
        }
        for row in samples
    ]
    scales = {}
    for model in MODELS:
        observed = [row["models"][model] for row in replicas if model in row["models"]]
        scales[model] = {
            "max_ready": max(row["ready"] for row in observed),
            "natural_scale_zero": any(row["desired"] == 0 for row in observed),
        }
        assert scales[model] == {"max_ready": 2, "natural_scale_zero": True}
    operator = read(directory / "operator-evidence.json")
    apps = {}
    for model in MODELS:
        row = operator["models"][model]
        usage = row["usage"]["data"]
        apps[model] = {
            "app_id": row["app"]["app_id"],
            "usage": {
                key: usage[key]
                for key in (
                    "logical_runs",
                    "succeeded_runs",
                    "failed_runs",
                    "active_runs",
                    "unique_users",
                    "estimated_gpu_seconds",
                )
            },
            "resource_metrics": [
                {key: chart[key] for key in ("id", "state", "reason")}
                for chart in row["metrics"]["data"]["charts"]
            ],
            "log_lines_returned": len(row["logs"]["data"]["items"]),
            "lifecycle_subjects": len(row["telemetry/workloads"]["data"]["items"]),
        }
        assert usage["active_runs"] == 0 and usage["estimated_gpu_seconds"] == 0
    selections = read(directory / "registration-configmaps.json")["items"]
    evidence = {
        "schema": "fs2-toxicology-hosted-qualification/v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "origin": "https://89.169.99.188",
        "cluster": "mk8scluster-e00j5z9te7x5dd9g6a",
        "backend_source": backend_source,
        "backend_image_digest": backend_image.split("@", 1)[1],
        "model_images": {
            model: read(ROOT / f"catalog/runtime/deployment-runtimes/{model}-cpu.json")[
                "record"
            ]["runtime"]["image"]["reference"]
            for model in MODELS
        },
        "registration_configmaps": [item["metadata"]["name"] for item in selections],
        "tested_bound": {
            "concurrent_operations": 2,
            "max_molecules_per_operation": 1000,
            "cpu_per_replica": 2,
            "memory_gib_per_replica": 2,
            "max_replicas": 2,
        },
        "cohorts": cohorts,
        "scaling": scales,
        "replica_observations": replicas,
        "operator": apps,
        "timing_note": "Operation cold_start_seconds is accepted-to-ready, including queue/activation; not pure model load. Harness elapsed includes submit/replay/poll/result verification.",
        "untested": [
            "LibreChat/LLM integration",
            "independent cross-tenant load test",
            "preemption",
            "new-node provisioning",
            "GPU execution/snapshots",
            "clinical/regulatory validation",
        ],
        "verdict": "public-rest-and-typed-mcp-qualified-at-recorded-bound",
        "unrestricted_customer_ready": False,
    }
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cohorts", nargs=2, required=True)
    parser.add_argument("--cold-cohort", required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--backend-source", required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "retain immutable qualification receipts"
    args.output.write_text(
        json.dumps(
            export(
                args.directory,
                args.cohorts,
                args.cold_cohort,
                args.backend_image,
                args.backend_source,
            ),
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )

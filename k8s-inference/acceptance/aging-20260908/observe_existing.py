#!/usr/bin/env python3
"""Read-only recovery of the two preserved r03 operations; never submit/retry."""

import argparse
import json
import time
from pathlib import Path

import httpx
from public_apps import TERMINAL, Trace, admin_call, app_observation, emit, exchange, now, validate_result, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    trace = Trace(args.output)
    access = json.loads(args.access_bundle.read_bytes())
    origin = access["endpoints"]["inference_base_url"].removesuffix("/v1")
    subjects = {
        "phenoage": ("38aad847-9010-51d0-bed7-bcb404bc755d", "fe10fdcf-1eaf-49dc-a1b3-4fa3d3297a2f"),
        "altumage": ("197fa990-6f65-539d-a1d8-240877ce861b", "aab0c233-029f-4a6c-8cac-0a7835fbeaa0"),
    }
    result = {
        "started_at": now(),
        "purpose": "read-only retained-r03 recovery, not a replay or clean cohort",
        "models": {},
    }
    previous, zero_counts, ready_seen = {}, {model: 0 for model in subjects}, set()
    deadline = time.monotonic() + 720
    with (
        httpx.Client(base_url=origin, timeout=60, trust_env=False, headers={"origin": origin}) as admin,
        httpx.Client(
            base_url=origin,
            timeout=60,
            trust_env=False,
            headers={"origin": origin, "authorization": "Bearer " + access["credentials"]["inference_access_token"]},
        ) as public,
    ):
        try:
            exchange(
                admin,
                trace,
                "POST",
                "/admin/api/v1/session",
                headers={
                    "authorization": "Bearer " + access["credentials"]["admin_bootstrap_token"],
                },
            )
            while time.monotonic() < deadline:
                for model, (app_id, operation_id) in subjects.items():
                    app_path = f"/admin/api/v1/apps/{app_id}"
                    operation, headers = exchange(
                        public, trace, "GET", f"/v1/operations/{operation_id}", expected=(200, 403, 404)
                    )
                    public_read_allowed = "status" in operation
                    if "status" not in operation:
                        detail = admin_call(admin, trace, "GET", app_path + "/runs/" + operation_id)
                        operation = detail["operation"]
                    observation = app_observation(admin, trace, app_path)
                    subject = result["models"].setdefault(model, {"app_id": app_id, "operation_id": operation_id})
                    subject.update(operation=operation, last_observation=observation)
                    if previous.get(model) != operation["status"]:
                        emit("recovery_status", model_id=model, operation_id=operation_id, status=operation["status"])
                        previous[model] = operation["status"]
                    if model not in ready_seen and any(item["ready"] for item in observation["containers"]["items"]):
                        ready_seen.add(model)
                        subject["ready_observation"] = observation
                        emit("recovery_worker_ready", model_id=model, operation_id=operation_id)
                    if not public_read_allowed:
                        subject["result_unavailable_reason"] = "The submitting scoped key is revoked; another key cannot read its operation payload. Admin metadata only; no grant change."
                    if public_read_allowed and operation["status"] == "succeeded" and "result" not in subject:
                        value, _ = exchange(public, trace, "GET", f"/v1/operations/{operation_id}/result")
                        subject["result"] = value
                        # The accepted first fixture sample identity is retained in
                        # the exact native response; no new model input is created.
                        sample_id = value["predictions"][0]["sample_id"]
                        subject["validation"] = validate_result(
                            model, 0, {"samples": [{"sample_id": sample_id}]}, value
                        )
                    cold = observation["summary"]["status"] == "Cold" and observation["containers"]["total"] == 0
                    zero_counts[model] = zero_counts[model] + 1 if cold and operation["status"] in TERMINAL else 0
                if all(count >= 2 for count in zero_counts.values()):
                    result["resources_returned_to_zero"] = True
                    break
                time.sleep(15 if all(state in TERMINAL for state in previous.values()) else 5)
            else:
                result["error_code"] = "bounded_recovery_deadline"
        except Exception as error:
            result.update(
                error_type=type(error).__name__, error_code=str(error) if isinstance(error, AssertionError) else None
            )
        finally:
            exchange(admin, trace, "DELETE", "/admin/api/v1/session", expected=(204,))
            result["completed_at"] = now()
            write(args.output / "outcome.json", result)
    emit(
        "recovery_finished",
        resources_returned_to_zero=result.get("resources_returned_to_zero", False),
        error_type=result.get("error_type"),
        completed_at=result["completed_at"],
    )
    return 0 if result.get("resources_returned_to_zero") else 1


if __name__ == "__main__":
    raise SystemExit(main())

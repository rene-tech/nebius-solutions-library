#!/usr/bin/env python3
"""Bounded ordinary-key LeRobot dataset cohorts; no operator privileges.

Two sequential cohorts on an operator-frozen release, followed by invalid-input,
concurrency=1 and running-stage cancellation probes. Never automatically retries
an admission. Deployment stability and public-admin reconciliation are separate
operator receipts, not inferred from this client's success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import client
from fs2_serve.scientific_batch.worker_errors import ERROR_DETAILS

HERE = Path(__file__).resolve().parent


def cases(include_blur):
    value = [
        (cohort, policy, "mcp" if (cohort == 1) == (policy == "lighting") else "http")
        for cohort in (1, 2)
        for policy in ("lighting", "environment")
    ]
    if include_blur:
        value.append((2, "blur", "mcp"))
    return value


async def run_directory(args, state, token, cohort, policy, protocol):
    output = args.output / f"cohort-{cohort}-{policy}"
    expected_variants = json.loads((HERE / (policy + ".json")).read_text())["variants"][
        "count"
    ]
    entry = {
        "cohort": cohort,
        "policy": policy,
        "protocol": protocol,
        "output": str(output),
        "started_at": client.now(),
        "expected_variants": expected_variants,
    }
    state["runs"].append(entry)
    client.save(args.output / "acceptance.json", state, token)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HERE / "client.py"),
        "run",
        "--dataset",
        str(args.dataset),
        "--request",
        str(HERE / (policy + ".json")),
        "--endpoint",
        args.endpoint,
        "--key-file",
        str(args.key_file),
        "--reader-python",
        str(args.reader_python),
        "--output",
        str(output),
        "--protocol",
        protocol,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--max-bytes",
        str(args.max_bytes),
        "--max-expanded-bytes",
        str(args.max_expanded_bytes),
        "--check-replay",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout
    with (args.output / f"cohort-{cohort}-{policy}.log").open("x") as log:
        async for raw in process.stdout:
            line = raw.decode(errors="replace")
            client.check(token not in line, "client_output_contains_secret")
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
    code = await process.wait()
    entry["exit_code"] = code
    if (output / "run.json").exists():
        run = client.harness.private_json(output / "run.json")
        entry.update(operation_id=run.get("operation_id"), outcome=run.get("outcome"))
        entry["validated_datasets"] = len(run.get("validations", []))
    client.save(args.output / "acceptance.json", state, token)
    client.check(
        code == 0
        and entry.get("outcome") == "dataset_integrity_passed"
        and entry.get("validated_datasets") == expected_variants,
        "dataset_run_failed_stop_admissions",
    )
    return run


def stage_running(value):
    return client.operation(value).get("status") not in client.TERMINAL and any(
        stage.get("status") == "active"
        and any(
            attempt.get("last_phase") == "active_compute"
            and attempt.get("resource_released") is False
            for attempt in stage.get("attempts", [])
        )
        for stage in value.get("batch", {}).get("stages", [])
    )


async def probes(args, state, token, successful):
    output = args.output / "probes"
    output.mkdir(mode=0o700)
    journal = {
        "run_id": "lerobot-probes-" + str(uuid4()),
        "started_at": client.now(),
        "parent_ids": [],
        "outcome": "in_progress",
    }
    request = successful["request"]
    async with client.connect(args.endpoint, token, journal, output) as public:
        invalid = json.loads(json.dumps(request))
        invalid["parameters"]["variants"]["count"] = 0
        # Deliberately bypass the local parser to prove the PUBLIC 422 contract.
        response = await public.http.post(
            f"/v1/models/{client.MODEL}:submit",
            headers={"Idempotency-Key": journal["run_id"] + "-invalid"},
            json=invalid,
        )
        journal["invalid_input"] = {
            "status": response.status_code,
            "body": response.json(),
        }
        public.persist()
        client.check(response.status_code == 422, "invalid_input_not_public_422")

        await worker_failure_probe(public, args, request)

        journal["cancellation_idempotency_key"] = journal["run_id"] + "-cancel"
        public.persist()
        accepted = await public.submit(
            request, journal["cancellation_idempotency_key"], "http"
        )
        journal.update(
            operation_id=client.harness.operation_id(accepted),
            accepted=accepted,
            cancellation_parent_id=client.harness.operation_id(accepted),
        )
        journal["parent_ids"].append(journal["operation_id"])
        public.persist()
        before = await public.response(
            "GET", f"/v1/operations/{journal['operation_id']}"
        )
        journal["concurrency_before"] = before
        client.check(
            client.operation(before)["status"] not in client.TERMINAL,
            "concurrency_probe_parent_already_terminal",
        )
        public.persist()
        response = await public.http.post(
            f"/v1/models/{client.MODEL}:submit",
            headers={"Idempotency-Key": journal["run_id"] + "-second"},
            json=request,
        )
        journal["concurrency"] = {
            "status": response.status_code,
            "body": response.json(),
            "expected_current_contract": "429 concurrency_exceeded",
        }
        if response.status_code == 202:
            # Capture and settle any actually admitted parent before reporting
            # the unexpected contract. Never leave it behind or call this 429.
            second_id = client.harness.operation_id(response.json())
            journal["parent_ids"].append(second_id)
            public.persist()
            journal["unexpected_second_cancel"] = await public.response(
                "POST", f"/v1/operations/{second_id}:cancel"
            )
            journal["operation_id"] = second_id
            public.persist()
            journal["unexpected_second_terminal"] = await public.wait_existing(
                args.timeout_seconds, "cancelled"
            )
            journal["operation_id"] = journal["cancellation_parent_id"]
        public.persist()

        # Reach actual worker-stage execution, not just an admitted queue record.
        deadline = time.monotonic() + min(args.timeout_seconds, 900)
        while True:
            current = await public.response(
                "GET", f"/v1/operations/{journal['operation_id']}"
            )
            journal["before_cancel"] = current
            public.persist()
            if stage_running(current):
                journal["cancelled_running_stage"] = True
                break
            if client.operation(current)["status"] in client.TERMINAL:
                raise client.harness.AcceptanceError(
                    "cancellation_probe_finished_before_cancel"
                )
            if time.monotonic() >= deadline:
                journal["cancelled_running_stage"] = False
                break  # Still cancel/settle this admitted work at the bound.
            await asyncio.sleep(3)
        journal["cancel_requested_at"] = client.now()
        public.persist()
        journal["cancel_response"] = await public.response(
            "POST", f"/v1/operations/{journal['operation_id']}:cancel"
        )
        public.persist()
        journal["terminal"] = await public.wait_existing(
            args.timeout_seconds, "cancelled"
        )
        public.persist()
        client.check(
            journal["concurrency"]["status"] == 429, "concurrency_contract_was_not_429"
        )
        code = journal["concurrency"]["body"].get("error", {}).get("code")
        client.check(code == "concurrency_exceeded", "429_was_not_concurrency_limit")
        client.check(
            journal["cancelled_running_stage"], "running_cancellation_not_exercised"
        )
        journal.update(
            outcome="expected_negative_cases_passed", completed_at=client.now()
        )
        public.persist()
    state["probes"] = journal
    client.save(args.output / "acceptance.json", state, token)


async def worker_failure_probe(public, args, request):
    """One admitted invalid selection proves worker→public static diagnostics."""
    journal = public.state
    invalid = json.loads(json.dumps(request))
    invalid["parameters"]["selection"]["episodes"] = [999]
    receipt = {
        "idempotency_key": journal["run_id"] + "-worker-invalid",
        "request_sha256": client.digest(invalid),
        "selection": invalid["parameters"]["selection"],
        "phase": "admission_pending",
        "zero_generation_children_verified": False,
        "child_verification_source": "operator exact-parent terminal collector required",
    }
    journal["worker_invalid_dataset"] = receipt
    public.persist()
    accepted = await public.submit(invalid, receipt["idempotency_key"], "http")
    identifier = client.harness.operation_id(accepted)
    journal["operation_id"] = identifier
    journal["parent_ids"].append(identifier)
    receipt.update(operation_id=identifier, accepted=accepted, phase="admitted")
    public.persist()
    receipt["terminal"] = await public.wait_existing(args.timeout_seconds, "failed")
    public.persist()
    terminal = client.operation(receipt["terminal"])
    client.check(
        terminal.get("http_status") == 422
        and terminal.get("error_code") == "DATASET_INVALID"
        and terminal.get("error_detail") == ERROR_DETAILS["DATASET_INVALID"]
        and client.settled(receipt["terminal"], "failed"),
        "worker_failure_code_or_detail_mismatch",
    )
    receipt.update(phase="expected_failure_and_released", completed_at=client.now())
    public.persist()


async def execute(args, state, token):
    successful = None
    for cohort, policy, protocol in cases(args.include_blur):
        successful = await run_directory(args, state, token, cohort, policy, protocol)
    assert successful
    await probes(args, state, token, successful)
    state.update(
        outcome="bounded_dataset_cohorts_passed",
        completed_at=client.now(),
        validated_datasets=sum(entry["validated_datasets"] for entry in state["runs"]),
    )


def main(executor=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "key-file", "reader-python", "output", "release-receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--max-bytes", type=int, default=5 * 1024**3)
    parser.add_argument("--max-expanded-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--include-blur", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    token, state = "", {}
    try:
        client.check(
            0 < args.timeout_seconds <= 7200
            and 0 < args.max_bytes <= 5 * 1024**3
            and 0 < args.max_expanded_bytes <= 8 * 1024**3,
            "bounded_limits_required",
        )
        token = client.read_key(args.key_file)
        # Hash only: the ordinary-key runner does not consume admin/kube secrets.
        release = client.harness.file_identity(args.release_receipt)
        args.output.mkdir(mode=0o700, parents=True)
        state = {
            "schema": "fs2-serve.nebius.ai/lerobot-public-acceptance/v1",
            "started_at": client.now(),
            "release_receipt": release,
            "release_stability_verified_by_client": False,
            "customer_ready": False,
            "physical_alignment_verified": False,
            "actual_librechat_verified": False,
            "outcome": "in_progress",
            "runs": [],
        }
        client.save(args.output / "acceptance.json", state, token)
        asyncio.run((executor or execute)(args, state, token))
        client.save(args.output / "acceptance.json", state, token)
        print(
            json.dumps(
                {
                    "outcome": state["outcome"],
                    "runs": len(state["runs"]),
                    "datasets": state["validated_datasets"],
                    "customer_ready": False,
                }
            )
        )
        return 0
    except Exception as error:
        code = client.safe_error_code(error)
        if state:
            state.update(
                outcome="failed_stop_new_admissions", error=code, failed_at=client.now()
            )
            client.save(args.output / "acceptance.json", state, token)
        print(
            json.dumps(
                {"outcome": "failed", "code": code, "automatic_resubmission": False}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

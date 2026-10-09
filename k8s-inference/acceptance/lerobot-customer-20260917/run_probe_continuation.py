#!/usr/bin/env python3
"""Continue only cancellation/concurrency after the retained r150 harness error.

No blur generation or malformed/invalid-selection admission is repeated. The
original stopped receipt stays unchanged and is content-addressed alongside
the exact-parent operator proof. This is targeted debug, not final cohorts.
"""

from pathlib import Path

import run_acceptance as driver

client = driver.client


def configure_parser(parser):
    parser.add_argument("--previous-output", type=Path, required=True)
    parser.add_argument("--zero-child-receipt", type=Path, required=True)


def prior_inputs(args, state):
    paths = {
        "stopped_aggregate": args.previous_output / "acceptance.json",
        "blur_run": args.previous_output / "cohort-1-blur/run.json",
        "stopped_probes": args.previous_output / "probes/run.json",
        "zero_child_proof": args.zero_child_receipt,
    }
    values = {name: client.harness.private_json(path) for name, path in paths.items()}
    aggregate, blur, previous, proof = (
        values[name]
        for name in (
            "stopped_aggregate",
            "blur_run",
            "stopped_probes",
            "zero_child_proof",
        )
    )
    client.check(
        aggregate.get("error") == "worker_failure_code_or_detail_mismatch"
        and aggregate.get("outcome") == "failed_stop_new_admissions"
        and aggregate["release_receipt"] == state["release_receipt"]
        and blur.get("outcome") == "dataset_integrity_passed"
        and len(blur.get("validations", [])) == 2
        and not previous.get("cancellation_parent_id"),
        "continuation_prior_scope_mismatch",
    )
    parent = proof.get("parent", {})
    attempts = [
        a for stage in parent.get("stages", []) for a in stage.get("attempts", [])
    ]
    client.check(
        proof.get("schema") == "fs2-serve.nebius.ai/lerobot-parent-child-observation/v1"
        and proof.get("database_transaction_read_only") is True
        and parent.get("id") == previous["worker_invalid_dataset"]["operation_id"]
        and parent.get("id")
        == client.operation(previous["worker_invalid_dataset"]["terminal"])["id"]
        and parent.get("status") == "failed"
        and parent.get("error_code") == "INVALID_REQUEST"
        and proof.get("children") == []
        and attempts
        and all(attempt.get("resource_released") is True for attempt in attempts),
        "continuation_zero_child_proof_mismatch",
    )
    driver.check_worker_failure(previous["worker_invalid_dataset"]["terminal"])
    state.update(
        scope="continuation_only_concurrency_and_running_cancellation",
        final_cohorts_verified=False,
        harness_error="Prior helper incorrectly expected DATASET_INVALID; out-of-range selection is INVALID_REQUEST.",
        prior_receipts={
            name: client.harness.file_identity(path) for name, path in paths.items()
        },
        zero_generation_children_verified=True,
        prior_blur_parent_id=blur["operation_id"],
        prior_invalid_parent_id=parent["id"],
    )
    return blur, previous


async def execute(args, state, token):
    blur, previous = prior_inputs(args, state)
    client.save(args.output / "acceptance.json", state, token)
    await driver.probes(args, state, token, blur, previous=previous)
    state.update(
        outcome="targeted_blur_probes_passed_with_documented_harness_correction",
        completed_at=client.now(),
        validated_datasets=2,
        new_dataset_runs=0,
    )


if __name__ == "__main__":
    raise SystemExit(driver.main(executor=execute, configure_parser=configure_parser))

#!/usr/bin/env python3
"""Run one Terraform-sequenced cold-start attempt on a disposable FS2 cluster.

Terraform owns the immutable attempt ordinal and chain.  Odd attempts are the
conventional control and even attempts are the selected candidate.  Repeated
applies therefore cannot silently collapse the required control/candidate
alternation.  Tokens and semantic payloads are read only by the existing
token-safe acceptance harness and never enter this receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cold_start_framework import (
    MATRIX_PATH,
    PROTECTED_CLUSTER_IDS,
    ColdStartContractError,
    canonical_digest,
    load_json,
    matrix_model,
    validate_compatibility_tuple,
    validate_deployment_identity_binding,
    validate_matrix,
)


FS2_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_SCRIPT = (
    FS2_ROOT
    / "stages/workloads/scripts/model_autoscaling_acceptance.py"
)
RUN_ID = re.compile(r"^[a-z][a-z0-9]{5,11}$")
CLUSTER_ID = re.compile(r"^mk8scluster-[a-z0-9]+$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
EXPERIMENT_ID = re.compile(r"^[a-z][a-z0-9-]{5,47}$")


def _private_regular_file(path: Path, code: str) -> None:
    if not path.is_absolute():
        raise ColdStartContractError(code + "_path_not_absolute")
    try:
        metadata = path.stat()
    except OSError:
        raise ColdStartContractError(code + "_unavailable") from None
    if (
        path.is_symlink()
        or not path.is_file()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ColdStartContractError(code + "_mode_invalid")
    if metadata.st_uid != os.geteuid():
        raise ColdStartContractError(code + "_owner_invalid")


def _environment_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise ColdStartContractError(name.lower() + "_missing")
    return Path(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_target(args: argparse.Namespace) -> None:
    if not args.run_root.is_absolute() or ".." in args.run_root.parts:
        raise ColdStartContractError("run_root_invalid")
    if not args.run_root.is_dir() or stat.S_IMODE(args.run_root.stat().st_mode) != 0o700:
        raise ColdStartContractError("run_root_mode_invalid")
    expected_kubeconfig = args.run_root / "kubeconfig"
    if args.kubeconfig != expected_kubeconfig:
        raise ColdStartContractError("kubeconfig_not_run_owned")
    _private_regular_file(args.kubeconfig, "kubeconfig")
    if RUN_ID.fullmatch(args.run_id) is None:
        raise ColdStartContractError("run_id_invalid")
    if args.context != f"fs2-disposable-{args.run_id}":
        raise ColdStartContractError("kube_context_not_run_owned")
    if CLUSTER_ID.fullmatch(args.cluster_id) is None:
        raise ColdStartContractError("cluster_id_invalid")
    if args.cluster_id in PROTECTED_CLUSTER_IDS:
        raise ColdStartContractError("protected_cluster_denied")
    if COMMIT.fullmatch(args.source_commit) is None:
        raise ColdStartContractError("source_commit_invalid")
    if EXPERIMENT_ID.fullmatch(args.experiment_id) is None:
        raise ColdStartContractError("experiment_id_invalid")
    expected_arm = "control" if args.attempt_ordinal % 2 else "candidate"
    if args.arm != expected_arm:
        raise ColdStartContractError("attempt_arm_order_invalid")
    if args.attempt_ordinal == 1:
        if args.previous_attempt_digest is not None:
            raise ColdStartContractError("first_attempt_has_previous_digest")
    elif args.previous_attempt_digest is None or DIGEST.fullmatch(
        args.previous_attempt_digest
    ) is None:
        raise ColdStartContractError("previous_attempt_digest_missing")


def _validate_attempt_chain(args: argparse.Namespace) -> None:
    output_dir = args.run_root / "cold-start-benchmark" / args.experiment_id
    expected_output = output_dir / f"attempt-{args.attempt_ordinal:03d}.json"
    if args.output != expected_output:
        raise ColdStartContractError("attempt_output_path_not_canonical")
    if args.output.exists() or args.output.is_symlink():
        raise ColdStartContractError("attempt_receipt_already_exists")
    if args.attempt_ordinal == 1:
        return

    previous_path = output_dir / f"attempt-{args.attempt_ordinal - 1:03d}.json"
    _private_regular_file(previous_path, "previous_attempt_receipt")
    previous = load_json(previous_path)
    claimed_digest = previous.get("receipt_digest")
    if not isinstance(claimed_digest, str) or DIGEST.fullmatch(claimed_digest) is None:
        raise ColdStartContractError("previous_attempt_receipt_digest_invalid")
    unsigned = dict(previous)
    unsigned.pop("receipt_digest")
    if claimed_digest != canonical_digest(unsigned):
        raise ColdStartContractError("previous_attempt_receipt_digest_invalid")
    expected_identity = {
        "schema": "fs2-serve.nebius.ai/terraform-cold-start-attempt/v1",
        "experiment_id": args.experiment_id,
        "attempt_ordinal": args.attempt_ordinal - 1,
        "arm": "candidate" if args.attempt_ordinal % 2 else "control",
        "source_commit": args.source_commit,
        "cluster_id": args.cluster_id,
        "run_id": args.run_id,
        "model_id": args.model_id,
    }
    if any(previous.get(name) != value for name, value in expected_identity.items()):
        raise ColdStartContractError("previous_attempt_receipt_identity_mismatch")
    if claimed_digest != args.previous_attempt_digest:
        raise ColdStartContractError("previous_attempt_digest_mismatch")


def _write_attempt_receipt(path: Path, value: dict[str, Any]) -> None:
    """Write one immutable receipt without replacing an existing attempt."""

    if not path.is_absolute():
        raise ColdStartContractError("output_path_not_absolute")
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
    except OSError:
        raise ColdStartContractError("attempt_receipt_already_exists") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _validate_mechanism(
    args: argparse.Namespace,
    model: dict[str, Any],
    compatibility_tuple: dict[str, Any],
    *,
    matrix: dict[str, Any] | None = None,
) -> str | None:
    if matrix is None:
        raise ColdStartContractError("deployment_identity_matrix_required")
    validate_deployment_identity_binding(
        matrix,
        model_id=args.model_id,
        compatibility_tuple=compatibility_tuple,
    )
    if args.arm == "control":
        if args.mechanism != "conventional":
            raise ColdStartContractError("control_mechanism_not_conventional")
        return None
    capability = model["capabilities"].get(args.mechanism)
    if capability is None or capability["state"] != "eligible-experiment":
        raise ColdStartContractError("candidate_mechanism_not_eligible")
    activation_path = _environment_path("FS2_COLD_START_MECHANISM_ACTIVATION_RECEIPT")
    _private_regular_file(activation_path, "mechanism_activation_receipt")
    activation = load_json(activation_path)
    required = {
        "schema",
        "experiment_id",
        "model_id",
        "mechanism",
        "source_commit",
        "workload_contract_digest",
        "compatibility_tuple_digest",
        "terraform_plan_sha256",
        "applied",
    }
    if set(activation) != required:
        raise ColdStartContractError("mechanism_activation_shape_invalid")
    if activation["schema"] != "fs2-serve.nebius.ai/cold-start-mechanism-activation/v1":
        raise ColdStartContractError("mechanism_activation_schema_invalid")
    if (
        activation["experiment_id"] != args.experiment_id
        or activation["model_id"] != args.model_id
        or activation["mechanism"] != args.mechanism
        or activation["source_commit"] != args.source_commit
        or activation["compatibility_tuple_digest"]
        != canonical_digest(compatibility_tuple)
        or activation["applied"] is not True
    ):
        raise ColdStartContractError("mechanism_activation_identity_mismatch")
    for name in ("workload_contract_digest", "terraform_plan_sha256"):
        if not isinstance(activation[name], str) or DIGEST.fullmatch(activation[name]) is None:
            raise ColdStartContractError("mechanism_activation_digest_invalid")

    if args.mechanism in {"cuda-criu-snapshot", "dynamo-snapshot"}:
        eligibility_path = _environment_path("FS2_COLD_START_SNAPSHOT_ELIGIBILITY")
        _private_regular_file(eligibility_path, "snapshot_eligibility")
        eligibility = load_json(eligibility_path)
        if (
            eligibility.get("schema")
            != "fs2-serve.nebius.ai/snapshot-experiment-eligibility/v1"
            or eligibility.get("model_id") != args.model_id
            or eligibility.get("mechanism") != args.mechanism
            or eligibility.get("eligible_for_isolated_experiment") is not True
            or eligibility.get("production_promotion") != "denied"
            or eligibility.get("target_tuple_digest")
            != canonical_digest(compatibility_tuple)
        ):
            raise ColdStartContractError("snapshot_eligibility_mismatch")
        return _sha256_file(eligibility_path)
    return None


def _acceptance_command(
    args: argparse.Namespace,
    *,
    model: dict[str, Any],
    token_file: Path,
    request_file: Path,
    evidence_file: Path,
) -> list[str]:
    return [
        sys.executable,
        str(ACCEPTANCE_SCRIPT),
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
        "--endpoint",
        args.endpoint,
        "--tls-mode",
        args.tls_mode,
        "--token-file",
        str(token_file),
        "--request-file",
        str(request_file),
        "--semantic-call-count",
        "2",
        "--capture-startup-phases",
        "--optimization-matrix",
        str(args.matrix),
        "--benchmark-mechanism",
        args.mechanism,
        "--evidence-file",
        str(evidence_file),
        "--namespace",
        "fs2-models",
        "--model-id",
        args.model_id,
        "--deployment",
        model["deployment"],
        "--service",
        model["service"],
        "--expected-floor",
        "0",
        "--cooldown-seconds",
        str(args.cooldown_seconds),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--scale-down-timeout-seconds",
        str(args.scale_down_timeout_seconds),
    ]


def _validate_observed_identity(
    compatibility_tuple: dict[str, Any],
    identity: dict[str, Any],
    *,
    matrix: dict[str, Any] | None = None,
) -> str:
    if matrix is None:
        raise ColdStartContractError("deployment_identity_matrix_required")
    validate_deployment_identity_binding(
        matrix,
        model_id=compatibility_tuple["model_id"],
        compatibility_tuple=compatibility_tuple,
    )
    annotations = identity.get("deployment_annotations")
    image_ids = identity.get("pod_image_ids")
    node = identity.get("node")
    if (
        not isinstance(annotations, dict)
        or not isinstance(image_ids, list)
        or not isinstance(node, dict)
    ):
        raise ColdStartContractError("observed_identity_shape_invalid")
    expected_annotations = {
        "fs2.nebius/model-content-digest": "sha256:"
        + compatibility_tuple["model_content_digest"],
        "fs2.nebius/runtime-image-digest": compatibility_tuple["runtime_image_digest"],
        "fs2.nebius/compile-cache-abi": compatibility_tuple["compile_cache_abi"],
    }
    for name, expected in expected_annotations.items():
        observed = annotations.get(name)
        if observed is None:
            raise ColdStartContractError("observed_identity_annotation_missing:" + name)
        if observed != expected:
            raise ColdStartContractError("observed_identity_annotation_mismatch")
    if not any(
        isinstance(value, str)
        and compatibility_tuple["runtime_image_digest"] in value
        for value in image_ids
    ):
        raise ColdStartContractError("observed_runtime_image_id_mismatch")
    if identity.get("runtime_argv_digest") != compatibility_tuple["runtime_argv_digest"]:
        raise ColdStartContractError("observed_runtime_argv_mismatch")
    if (
        identity.get("runtime_environment_digest")
        != compatibility_tuple["runtime_environment_digest"]
    ):
        raise ColdStartContractError("observed_runtime_environment_mismatch")
    node_info = node.get("status", {}).get("nodeInfo", {})
    if node_info.get("kernelVersion") != compatibility_tuple["kernel_release"]:
        raise ColdStartContractError("observed_kernel_release_mismatch")
    runtime = node_info.get("containerRuntimeVersion")
    expected_runtime = (
        compatibility_tuple["container_runtime_name"]
        + "://"
        + compatibility_tuple["container_runtime_version"]
    )
    if runtime != expected_runtime:
        raise ColdStartContractError("observed_container_runtime_mismatch")
    if not isinstance(node.get("metadata", {}).get("uid"), str):
        raise ColdStartContractError("observed_node_uid_missing")
    return canonical_digest(identity)


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_target(args)
    _validate_attempt_chain(args)
    matrix = load_json(args.matrix)
    validate_matrix(matrix)
    model = dict(matrix_model(matrix, args.model_id))

    token_file = _environment_path("FS2_COLD_START_TOKEN_FILE")
    request_dir = _environment_path("FS2_COLD_START_REQUEST_DIR")
    identity_dir = _environment_path("FS2_COLD_START_IDENTITY_DIR")
    _private_regular_file(token_file, "token_file")
    if not request_dir.is_absolute() or not request_dir.is_dir():
        raise ColdStartContractError("request_dir_invalid")
    if not identity_dir.is_absolute() or not identity_dir.is_dir():
        raise ColdStartContractError("identity_dir_invalid")
    request_file = request_dir / f"{args.model_id}.json"
    identity_file = identity_dir / f"{args.model_id}.json"
    _private_regular_file(request_file, "request_file")
    _private_regular_file(identity_file, "identity_file")
    compatibility_tuple = load_json(identity_file)
    validate_compatibility_tuple(compatibility_tuple)
    if compatibility_tuple["model_id"] != args.model_id:
        raise ColdStartContractError("identity_model_mismatch")
    if compatibility_tuple["capacity_state"] not in {
        "prepared-node-zero-pod",
        "fresh-node-zero-pod",
        "preemption-replacement",
    }:
        raise ColdStartContractError("identity_capacity_state_not_cold")
    eligibility_digest = _validate_mechanism(
        args,
        model,
        compatibility_tuple,
        matrix=matrix,
    )

    output_dir = args.run_root / "cold-start-benchmark" / args.experiment_id
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    evidence_file = output_dir / f"attempt-{args.attempt_ordinal:03d}-acceptance.json"
    command = _acceptance_command(
        args,
        model=model,
        token_file=token_file,
        request_file=request_file,
        evidence_file=evidence_file,
    )
    result = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=args.timeout_seconds + args.scale_down_timeout_seconds + 120,
    )
    if result.returncode != 0:
        raise ColdStartContractError("acceptance_attempt_failed")
    _private_regular_file(evidence_file, "acceptance_evidence")
    evidence = load_json(evidence_file)
    if evidence.get("result") != "PASS":
        raise ColdStartContractError("acceptance_evidence_not_pass")
    calls = evidence.get("semantic_calls")
    if not isinstance(calls, list) or [call.get("ordinal") for call in calls] != [1, 2]:
        raise ColdStartContractError("two_semantic_calls_not_observed")
    if evidence.get("final") != {"replicas": 0, "ready": 0, "endpoints": 0}:
        raise ColdStartContractError("return_to_zero_not_observed")
    startup = evidence.get("startup_observation")
    if not isinstance(startup, dict) or not isinstance(
        startup.get("phase_observation"), dict
    ):
        raise ColdStartContractError("startup_observation_missing")
    identity_observation = startup.get("identity_observation")
    if not isinstance(identity_observation, dict):
        raise ColdStartContractError("identity_observation_missing")
    observed_identity_digest = _validate_observed_identity(
        compatibility_tuple,
        identity_observation,
        matrix=matrix,
    )

    completed_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    receipt = {
        "schema": "fs2-serve.nebius.ai/terraform-cold-start-attempt/v1",
        "experiment_id": args.experiment_id,
        "attempt_ordinal": args.attempt_ordinal,
        "repetition": (args.attempt_ordinal + 1) // 2,
        "arm": args.arm,
        "mechanism": args.mechanism,
        "previous_attempt_digest": args.previous_attempt_digest,
        "source_commit": args.source_commit,
        "cluster_id": args.cluster_id,
        "run_id": args.run_id,
        "model_id": args.model_id,
        "matrix_digest": canonical_digest(matrix),
        "compatibility_tuple_digest": canonical_digest(compatibility_tuple),
        "observed_identity_digest": observed_identity_digest,
        "snapshot_eligibility_receipt_sha256": eligibility_digest,
        "raw_acceptance_receipt_sha256": _sha256_file(evidence_file),
        "semantic_call_result_sha256": [call["result_sha256"] for call in calls],
        "phase_complete_for_promotion": startup["phase_observation"].get(
            "complete_for_promotion"
        ),
        "missing_required_events": startup["phase_observation"].get(
            "missing_required_events"
        ),
        "transition": "zero-to-ready-to-call1-to-call2-to-zero",
        "result": "PASS",
        "completed_at": completed_at,
    }
    receipt_digest = canonical_digest(receipt)
    receipt["receipt_digest"] = receipt_digest
    return receipt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--tls-mode",
        choices=("verified", "disposable-staging-insecure"),
        default="verified",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--attempt-ordinal", type=int, required=True)
    parser.add_argument("--arm", choices=("control", "candidate"), required=True)
    parser.add_argument(
        "--mechanism",
        choices=(
            "conventional",
            "shared-cache",
            "local-nvme",
            "oci-image-volume",
            "oci-modelcar",
            "cuda-criu-snapshot",
            "dynamo-snapshot",
        ),
        required=True,
    )
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument("--cooldown-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--scale-down-timeout-seconds", type=int, default=1800)
    parser.add_argument("--previous-attempt-digest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.attempt_ordinal <= 40:
        parser.error("--attempt-ordinal must be from 1 through 40")
    if not args.matrix.is_absolute():
        parser.error("--matrix must be absolute")
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    if args.run_root.resolve() not in args.output.resolve().parents:
        parser.error("--output must be inside --run-root")
    if not 5 <= args.cooldown_seconds <= 7200:
        parser.error("--cooldown-seconds must be from 5 through 7200")
    if not 30 <= args.timeout_seconds <= 14400:
        parser.error("--timeout-seconds must be from 30 through 14400")
    if not 30 <= args.scale_down_timeout_seconds <= 7200:
        parser.error("--scale-down-timeout-seconds must be from 30 through 7200")
    return args


def main() -> int:
    args = parse_args()
    try:
        _validate_target(args)
        _validate_attempt_chain(args)
    except ColdStartContractError:
        return 2
    try:
        receipt = run(args)
    except (ColdStartContractError, subprocess.TimeoutExpired) as error:
        failure = {
            "schema": "fs2-serve.nebius.ai/terraform-cold-start-attempt/v1",
            "experiment_id": args.experiment_id,
            "attempt_ordinal": args.attempt_ordinal,
            "repetition": (args.attempt_ordinal + 1) // 2,
            "arm": args.arm,
            "mechanism": args.mechanism,
            "previous_attempt_digest": args.previous_attempt_digest,
            "source_commit": args.source_commit,
            "cluster_id": args.cluster_id,
            "run_id": args.run_id,
            "model_id": args.model_id,
            "result": "FAIL",
            "failure_code": (
                str(error)
                if isinstance(error, ColdStartContractError)
                else "acceptance_attempt_timeout"
            ),
            "completed_at": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
        }
        failure["receipt_digest"] = canonical_digest(failure)
        receipt = failure
        result = 1
    else:
        result = 0
    try:
        _write_attempt_receipt(args.output, receipt)
    except ColdStartContractError:
        return 2
    return result


if __name__ == "__main__":
    raise SystemExit(main())

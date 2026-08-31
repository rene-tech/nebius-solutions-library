#!/usr/bin/env python3
"""Capture the warm-only, non-alias MSA CPU fallback semantic receipt.

The endpoint must be a caller-owned localhost port-forward to the static
``msa-search-pdb70`` Service. The canonical routed PDB70 NIM is intentionally
not involved. Two distinct frozen queries are validated in memory; response
payloads and request bodies are never retained.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parent
FS2_ROOT = ROOT.parent.parent
ACCEPTANCE_PATH = (
    FS2_ROOT / "stages/workloads/scripts/model_autoscaling_acceptance.py"
)
VALIDATOR_PATH = (
    FS2_ROOT
    / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2"
    / "msa-search-native/validate_msa_search.py"
)
MATRIX_PATH = ROOT / "cold-start-optimization-matrix.json"
FIXTURE_PATH = (
    FS2_ROOT
    / "catalog/runtime/packaged-repository/nim-fast-start/faststart-v2"
    / "msa-search-native/fixtures/request-pdb70.json"
)
BOOT_ID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class FallbackWarmError(ValueError):
    """The local endpoint, semantic result, or runtime identity was invalid."""


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FallbackWarmError("source_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise FallbackWarmError("source_module_unavailable") from None
    return module


ACCEPTANCE = _module("fs2_model_autoscaling_for_cpu_fallback", ACCEPTANCE_PATH)
VALIDATOR = _module("fs2_msa_semantic_for_cpu_fallback", VALIDATOR_PATH)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise FallbackWarmError("endpoint_not_local_port_forward")
    return f"http://{parsed.hostname}:{parsed.port}"


def _identity(kubectl: Any, args: argparse.Namespace) -> dict[str, Any]:
    snapshot = ACCEPTANCE.cluster_snapshot(
        kubectl,
        args.namespace,
        "msa-search-pdb70",
        args.deployment,
        args.service,
    )
    if (
        snapshot.deployment_replicas != 1
        or snapshot.deployment_ready != 1
        or snapshot.endpoints < 1
        or len(snapshot.pod_identities) != 1
    ):
        raise FallbackWarmError("fallback_warm_floor_invalid")
    identity_args = argparse.Namespace(
        optimization_matrix=args.optimization_matrix,
        model_id="msa-search-pdb70",
        namespace=args.namespace,
    )
    try:
        *_, identity = ACCEPTANCE._capture_runtime_identity(
            kubectl,
            identity_args,
            pod_identities=snapshot.pod_identities,
        )
    except BaseException:
        raise FallbackWarmError("fallback_runtime_identity_capture_failed") from None
    return identity


def _same_runtime(before: dict[str, Any], after: dict[str, Any]) -> bool:
    fields = (
        "pod",
        "container_image_ids",
        "pod_image_ids",
        "runtime_argv_digest",
        "runtime_environment_digest",
    )
    return all(
        before.get(field) == after.get(field) for field in fields
    ) and before.get("node", {}).get("metadata", {}) == after.get("node", {}).get(
        "metadata", {}
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    origin = _origin(args.endpoint)
    try:
        clock_domain = ACCEPTANCE.clock_domain()
    except BaseException:
        raise FallbackWarmError("monotonic_clock_identity_invalid") from None
    if BOOT_ID.fullmatch(clock_domain) is None:
        raise FallbackWarmError("monotonic_clock_identity_invalid")
    kubectl = ACCEPTANCE.Kubectl(args.kubeconfig, args.context)
    before = _identity(kubectl, args)
    try:
        template = VALIDATOR._read_fixture(args.request_file)
        ready_at = VALIDATOR._wait_ready(origin, args.ready_timeout_seconds)
    except BaseException:
        raise FallbackWarmError("fallback_readiness_or_fixture_invalid") from None

    started_monotonic_ns = time.monotonic_ns()
    activation_accepted_at = utc_now()
    activation_accepted_ns = time.monotonic_ns()
    calls: list[dict[str, Any]] = []
    for ordinal, query in enumerate((VALIDATOR.QUERY_1, VALIDATOR.QUERY_2), 1):
        payload = VALIDATOR._request_for_case(template, query)
        try:
            raw, _, request_started_at, response_received_at = VALIDATOR._post(
                origin, payload, args.request_timeout_seconds
            )
            invariant = VALIDATOR._validate_response(json.loads(raw), query)
        except BaseException:
            raise FallbackWarmError(
                f"fallback_semantic_call_{ordinal}_invalid"
            ) from None
        calls.append(
            {
                "ordinal": ordinal,
                "operation_id": f"direct-warm-call-{ordinal}",
                "accepted_at": utc_now(),
                "accepted_monotonic_ns": time.monotonic_ns(),
                "request_started_at": request_started_at,
                "response_received_at": response_received_at,
                "semantic_kind": "msa-search-pdb70-faststart-semantic-v1",
                "result_bytes": len(raw),
                "result_sha256": hashlib.sha256(raw).hexdigest(),
                "invariant": invariant,
            }
        )
    if calls[0]["result_sha256"] == calls[1]["result_sha256"]:
        raise FallbackWarmError("fallback_semantic_responses_not_distinct")
    after = _identity(kubectl, args)
    if not _same_runtime(before, after):
        raise FallbackWarmError("fallback_runtime_changed_during_attempt")
    observed_pod_uid = before["pod"]["uid"]
    observed_node_uid = before["node"]["metadata"]["uid"]
    completed_at = utc_now()
    return {
        "schema": "fs2-serve.nebius.ai/cpu-fallback-warm-acceptance/v1",
        "model_id": "msa-search-pdb70",
        "identity_relationship": "capability-equivalent-non-alias",
        "exact_pdb70_parity": False,
        "target": {
            "namespace": args.namespace,
            "deployment": args.deployment,
            "service": args.service,
            "expected_floor": 1,
        },
        "clock": {
            "domain": clock_domain,
            "kind": "linux-monotonic",
            "started_monotonic_ns": started_monotonic_ns,
        },
        "phase_timestamps": {
            "activation_accepted_at": activation_accepted_at,
            "readiness_observed_at": ready_at,
            "semantic_call1_accepted_at": calls[0]["accepted_at"],
            "semantic_call2_accepted_at": calls[1]["accepted_at"],
            "return_to_floor_accepted_at": completed_at,
        },
        "phase_monotonic_ns": {
            "activation_accepted": activation_accepted_ns,
            "readiness_observed": None,
            "semantic_call1_accepted": calls[0]["accepted_monotonic_ns"],
            "semantic_call2_accepted": calls[1]["accepted_monotonic_ns"],
            "return_to_floor_accepted": time.monotonic_ns(),
        },
        "semantic_calls": [
            {
                key: value
                for key, value in call.items()
                if key != "accepted_monotonic_ns"
            }
            for call in calls
        ],
        "runtime_identity_observation": before,
        "observed_runtime_identities": {
            "pod_uids": [observed_pod_uid],
            "node_uids": [observed_node_uid],
        },
        "result": "PASS",
        "completed_at": completed_at,
    }


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def write_new(path: Path, value: Any) -> None:
    if not path.is_absolute():
        raise FallbackWarmError("output_path_not_absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise FallbackWarmError("output_create_failed") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(canonical_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--namespace", default="fs2-models")
    parser.add_argument("--deployment", default="msa-search-pdb70")
    parser.add_argument("--service", default="msa-search-pdb70")
    parser.add_argument("--optimization-matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument("--request-file", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--ready-timeout-seconds", type=float, default=60)
    parser.add_argument("--request-timeout-seconds", type=float, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(arguments)
    for name in ("context", "namespace", "deployment", "service"):
        if ACCEPTANCE._NAME.fullmatch(getattr(args, name)) is None:
            parser.error(f"--{name.replace('_', '-')} is not a Kubernetes-safe name")
    if not args.kubeconfig.is_absolute():
        parser.error("--kubeconfig must be absolute")
    try:
        kubeconfig = args.kubeconfig.lstat()
    except OSError:
        parser.error("--kubeconfig is unavailable")
    if (
        args.kubeconfig.is_symlink()
        or not stat.S_ISREG(kubeconfig.st_mode)
        or stat.S_IMODE(kubeconfig.st_mode) != 0o600
        or kubeconfig.st_uid != os.geteuid()
    ):
        parser.error("--kubeconfig must be an owner mode-0600 regular file")
    if args.optimization_matrix.resolve() != MATRIX_PATH.resolve():
        parser.error("--optimization-matrix must be the checked-in exact matrix")
    if args.request_file.resolve() != FIXTURE_PATH.resolve():
        parser.error("--request-file must be the checked-in frozen fixture")
    for value in (args.ready_timeout_seconds, args.request_timeout_seconds):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            parser.error("timeouts must be positive finite numbers")
    return args


def main() -> int:
    args = parse_args()
    try:
        value = run(args)
    except FallbackWarmError as error:
        value = {
            "schema": "fs2-serve.nebius.ai/cpu-fallback-warm-acceptance/v1",
            "model_id": "msa-search-pdb70",
            "identity_relationship": "capability-equivalent-non-alias",
            "exact_pdb70_parity": False,
            "target": {
                "namespace": args.namespace,
                "deployment": args.deployment,
                "service": args.service,
                "expected_floor": 1,
            },
            "result": "FAIL",
            "failure_code": str(error),
            "completed_at": utc_now(),
        }
    try:
        write_new(args.output.resolve(), value)
    except FallbackWarmError:
        return 2
    return 0 if value["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

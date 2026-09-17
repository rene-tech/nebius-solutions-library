#!/usr/bin/env python3
"""Closed command dispatcher for the descriptor-sealed SAI-07 v4 zipapp."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def inspect_plan(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[1] not in {"foundation", "workloads"}:
        raise ValueError("inspect-plan requires one plan descriptor and fixed stage")
    import sai07_saved_plan_contract as saved_plan

    capsule = json.loads(Path("/proc/1/fd/180").read_bytes())
    runtime = capsule["runtime"]
    result = saved_plan.inspect_saved_plan(
        Path(arguments[0]),
        Path("/proc/1/fd/194"),
        runtime["runtime_files"]["terraform"]["sha256"],
        runtime["terraform_version"],
    )
    if result["configuration_sha256"] != runtime[
        "terraform_configuration_sha256"
    ][arguments[1]]:
        raise ValueError("saved plan configuration differs from capsule stage")
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def verify_settlement(arguments: list[str]) -> int:
    if len(arguments) != 4 or arguments[3] not in {"foundation", "workloads"}:
        raise ValueError(
            "verify-settlement requires authorized/first/second plan descriptors "
            "and fixed stage"
        )
    import sai07_saved_plan_contract as saved_plan

    capsule = json.loads(Path("/proc/1/fd/180").read_bytes())
    runtime = capsule["runtime"]
    result = saved_plan.verify_settled_plans(
        Path(arguments[0]),
        Path(arguments[1]),
        Path(arguments[2]),
        Path("/proc/1/fd/194"),
        runtime["runtime_files"]["terraform"]["sha256"],
        runtime["terraform_version"],
    )
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        raise ValueError("SAI-07 bundle command is required")
    command = sys.argv[1]
    arguments = sys.argv[2:]
    if command == "inspect-plan":
        return inspect_plan(arguments)
    if command == "verify-settlement":
        return verify_settlement(arguments)
    if command == "external-execute":
        import run_sai07_external_execution_v3 as executor

        sys.argv = ["run_sai07_external_execution_v3.py", *arguments]
        return executor.main()
    if command == "verify-ack":
        import verify_sai07_external_execution_ack_v3 as verifier

        if arguments:
            raise ValueError("verify-ack accepts its exact query only on stdin")
        sys.argv = ["verify_sai07_external_execution_ack_v3.py"]
        return verifier.main()
    if command == "verify-trust":
        import verify_sai07_custody_trust_v3 as verifier

        if arguments:
            raise ValueError("verify-trust accepts its exact query only on stdin")
        sys.argv = ["verify_sai07_custody_trust_v3.py"]
        return verifier.main()
    if command == "verify-manifest":
        import verify_sai07_custody_manifest_bundle_v3 as verifier

        if arguments:
            raise ValueError("verify-manifest accepts its exact query only on stdin")
        sys.argv = ["verify_sai07_custody_manifest_bundle_v3.py"]
        return verifier.main()
    if command == "verify-receipt":
        import verify_pod_security_receipts as verifier

        if arguments:
            raise ValueError("verify-receipt accepts its exact query from the environment")
        sys.argv = ["verify_pod_security_receipts.py"]
        return verifier.main()
    raise ValueError("unsupported SAI-07 bundle command")


if __name__ == "__main__":
    raise SystemExit(main())

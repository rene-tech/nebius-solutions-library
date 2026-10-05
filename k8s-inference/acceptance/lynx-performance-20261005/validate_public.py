"""Reuse exact-input native validation on hash-verified public downloads.

Reads existing evidence only; no network, submission, recovery or trajectory
copy. Public transport and molecular-output checks remain separate assertions.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from native_probe import validate
from recipes import TPR_SHA256, save, sha


def check(case):
    receipt = json.loads((case / "receipt.json").read_text())
    if (receipt.get("state") != "verified" or not receipt.get("idempotency_verified")
            or receipt.get("benchmark_identity", {}).get("original_tpr_sha256") != TPR_SHA256):
        raise ValueError("Require already verified exact-input public transport receipt")
    results = []
    for artifact in receipt["verified_artifacts"]:
        path = Path(artifact["path"])
        if (not path.resolve().is_relative_to(case.resolve()) or path.stat().st_size != artifact["size_bytes"]
                or sha(path) != artifact["sha256"]):
            raise ValueError("Downloaded artifact identity changed")
        if path.stat().st_size < 1024 * 1024:
            try:
                value = json.loads(path.read_text())
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict) and "commands" in value and "recipe_sha256" in value and "files" in value:
                results.append((path, value))
    directories = receipt["native_outputs"]["directories"]
    if len(results) != 1 or len(directories) != 1:
        raise ValueError("Exact recipe expects one native result and one materialized job directory")
    result_path, result = results[0]
    if result["operation_id"] != receipt["operation_id"] or result["status"] != "succeeded":
        raise ValueError("Native result does not bind the successful public operation")
    data = Path(directories[0]["path"])
    if not data.resolve().is_relative_to(case.resolve()):
        raise ValueError("Native directory escapes selected case")
    request = json.loads((case / "request.json").read_text())["parameters"]
    gpu_count = request.get("nodes", 1) * request.get("gpus_per_node", 1)
    mpi = receipt["benchmark_identity"]["model_id"] == "gromacs-mpi"
    validation = validate(case, request, gpu_count, mpi, False, 0, data_dir=data, result_path=result_path)
    return {"operation_id": receipt["operation_id"], "interface": receipt["benchmark_identity"]["interface"],
            "public_transport_verified": True, "artifacts_rehashed": len(receipt["verified_artifacts"]),
            "receipt_sha256": sha(case / "receipt.json"), "request_sha256": sha(case / "request.json"),
            "native": validation, "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Existing internal QA public receipt plus downloaded native output; not actual-agent or ensemble validation."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--case", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists():
        raise ValueError("Never overwrite an independent output-validation receipt")
    result = check(args.case)
    save(args.output, result)
    print(json.dumps(result))
    return 0 if result["native"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

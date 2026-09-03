#!/usr/bin/env python3
"""Render the AlphaFold 3 raw-input acceptance request from a real receipt.

Every reference-data field is derived from the published terminal receipt, so
the rendered request binds the exact tree and manifest identities the publisher
wrote rather than any hand-copied digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REFERENCE_DATA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REFERENCE_DATA))

import reference_data  # noqa: E402


def build_request(
    receipt: dict,
    *,
    request_id: str,
    tenant_id: str,
    workload_id: str,
    input_uri: str,
    input_sha256: str,
    input_bytes: int,
    manifest_uri: str,
    image: str,
    interpreter: str,
    script: str,
    output_prefix_uri: str,
    placement_path: Path | None = None,
) -> dict:
    contract = reference_data.load_placement_contract(placement_path)
    defaults = reference_data.resolve_stage_placement(contract, "raw-input")["defaults"]
    document = {
        "schema": reference_data.REQUEST_SCHEMA,
        "request_id": request_id,
        "tenant_id": tenant_id,
        "workload_id": workload_id,
        "input": {
            "uri": input_uri,
            "sha256": input_sha256,
            "bytes": input_bytes,
            "media_type": "application/json",
        },
        "reference_data": reference_data.derive_preprocess_reference_data(
            receipt, manifest_uri=manifest_uri
        ),
        "backend": {
            "kind": "alphafold3-data",
            "database_root": reference_data.derive_database_root(receipt),
            "output_format": "alphafold3-json",
            "threads": defaults["threads"],
            "entrypoint": {"interpreter": interpreter, "script": script},
        },
        "privacy": {
            "network_mode": "private-only",
            "public_msa_opt_in": False,
            "log_sequence_content": False,
        },
        "output": {"prefix_uri": output_prefix_uri, "retention_days": 30},
        "execution": {
            "image": image,
            "cpu": defaults["cpu"],
            "memory": defaults["memory"],
            "ephemeral_storage": defaults["ephemeral_storage"],
            "active_deadline_seconds": defaults["active_deadline_seconds"],
            "backoff_limit": defaults["backoff_limit"],
        },
    }
    validated = reference_data.validate_preprocess_request(document)
    reference_data.check_execution_fits(validated["execution"], contract, "raw-input")
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--request-id", default="af3-raw-input-acceptance")
    parser.add_argument("--tenant-id", default="tenant-cancer-immunotherapy")
    parser.add_argument("--workload-id", default="workload-af3-raw-input")
    parser.add_argument("--input-uri", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--input-bytes", type=int, required=True)
    parser.add_argument("--manifest-uri", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--interpreter", default="/alphafold3_venv/bin/python3")
    parser.add_argument("--script", default="/app/alphafold/run_alphafold.py")
    parser.add_argument("--output-prefix-uri", required=True)
    parser.add_argument("--placement", type=Path)
    arguments = parser.parse_args()

    receipt = reference_data.validate_terminal_receipt(
        reference_data.load_json(arguments.receipt)
    )
    document = build_request(
        receipt,
        request_id=arguments.request_id,
        tenant_id=arguments.tenant_id,
        workload_id=arguments.workload_id,
        input_uri=arguments.input_uri,
        input_sha256=arguments.input_sha256,
        input_bytes=arguments.input_bytes,
        manifest_uri=arguments.manifest_uri,
        image=arguments.image,
        interpreter=arguments.interpreter,
        script=arguments.script,
        output_prefix_uri=arguments.output_prefix_uri,
        placement_path=arguments.placement,
    )
    json.dump(document, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    print(
        json.dumps({
            "request_sha256": hashlib.sha256(
                reference_data.canonical_json(document)
            ).hexdigest(),
            "database_root": document["backend"]["database_root"],
            "reference_manifest_sha256": document["reference_data"]["manifest_sha256"],
        }, sort_keys=True),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

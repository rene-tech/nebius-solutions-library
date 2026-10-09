"""Fresh end-to-end large upload, including timed gateway checksum finalization.

Use only an existing internal QA identity and public qualification data. A
distinct cohort intentionally creates one new upload intent, not a GPU job.
"""

import argparse
import json
import os
import time
from pathlib import Path

import httpx

from qualify_api import checked, multipart_file, post_with_admission_wait, save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--cohort", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    env = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    size = args.input.stat().st_size
    with httpx.Client(
        base_url="https://89.169.99.188",
        timeout=900,
        trust_env=False,
        headers={"Authorization": "Bearer " + env["SCIENTIFIC_MODELS_API_KEY"]},
    ) as client:
        me = checked(client.get("/v1/me"))
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError("Only existing internal QA is allowed")
        reserved = post_with_admission_wait(
            client,
            "/v1/scientific-artifacts/uploads",
            wait_seconds=900,
            json={
                "model_id": "scvi-scanvi",
                "sha256": args.sha256,
                "size_bytes": size,
                "media_type": "application/x-hdf5",
                "compression": "none",
            },
            headers={"Idempotency-Key": "scvi-fresh-transfer-20261006-" + args.cohort},
        )
        save(
            args.output / "reservation.json",
            {key: reserved[key] for key in ("upload_id", "operation_id")},
        )
        started = time.monotonic()
        with httpx.Client(timeout=900, trust_env=False) as objects:
            multipart_file(
                client,
                objects,
                reserved,
                args.input,
                size,
                args.output / "multipart.json",
            )
        transferred = time.monotonic()
        ref = checked(
            client.post(
                f"/v1/scientific-artifacts/uploads/{reserved['upload_id']}:finalize",
                json={"operation_id": reserved["operation_id"]},
            )
        )
        ended = time.monotonic()
        if (ref["sha256"], ref["size_bytes"]) != (args.sha256, size):
            raise ValueError("Finalized identity differs from the known public input")
        receipt = {
            "status": "passed",
            "artifact": ref,
            "operation_id": reserved["operation_id"],
            "transfer_seconds": transferred - started,
            "finalization_seconds": ended - transferred,
            "total_seconds": ended - started,
        }
        save(args.output / "receipt.json", receipt)
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    main()

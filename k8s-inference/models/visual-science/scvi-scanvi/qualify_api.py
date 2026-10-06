"""Internal system/qa client for the hosted single-cell job qualification.

Presigned upload handles stay in memory. Receipts contain only immutable
artifact/operation identities; credentials and input bytes are not logged.
Repeated invocations reuse their saved artifact and run identities.
"""

import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def checked(response):
    if response.is_error:
        raise RuntimeError(
            f"HTTP {response.status_code}; request {response.headers.get('x-request-id', 'unknown')}; {response.text[:2000]}"
        )
    return response.json()


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def post_with_admission_wait(client, endpoint, *, wait_seconds, **kwargs):
    """Retry only explicit non-admission, keeping the same idempotency key.

    Do not retry ambiguous transport errors or arbitrary HTTP failures here.
    Upload/job receipts let a disconnected client resume explicitly instead.
    """
    deadline, announced = time.monotonic() + wait_seconds, False
    while True:
        response = client.post(endpoint, **kwargs)
        if response.status_code != 429:
            return checked(response)
        try:
            error = response.json().get("error", {})
        except ValueError:
            return checked(response)
        code = error.get("code", error.get("type"))
        if code not in {"concurrency_exceeded", "admission_limit_reached"}:
            return checked(response)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "API key concurrency is still occupied. Uploaded files are retained; "
                "rerun with the same output directory and idempotency key, or increase "
                "--admission-wait-seconds. No new job was admitted."
            )
        if not announced:
            print(
                json.dumps({"waiting_for_key_capacity": True, "job_admitted": False}),
                flush=True,
            )
            announced = True
        delay = error.get("retry_after_seconds", response.headers.get("retry-after", 5))
        try:
            delay = min(30, max(1, float(delay)))
        except (TypeError, ValueError):
            delay = 5
        time.sleep(min(delay, remaining))


def multipart_file(client, objects, reserved, path, size, receipt):
    endpoint = reserved.get("multipart_path")
    if not endpoint:
        raise RuntimeError(
            "The deployed artifact API does not advertise multipart upload"
        )
    common = {"operation_id": reserved["operation_id"]}
    session = checked(client.post(endpoint, json={**common, "action": "start"}))
    save(receipt, session)
    common["multipart_upload_id"] = session["multipart_upload_id"]
    present = checked(client.post(endpoint, json={**common, "action": "list"}))
    part_size = session["part_size_bytes"]
    total = (size + part_size - 1) // part_size
    completed = {part["part_number"]: part["size_bytes"] for part in present["parts"]}
    needed = [
        number
        for number in range(1, total + 1)
        if completed.get(number) != min(part_size, size - (number - 1) * part_size)
    ]
    print(
        json.dumps({"multipart_total": total, "parts_to_transfer": len(needed)}),
        flush=True,
    )
    for offset in range(0, len(needed), 128):
        handles = checked(
            client.post(
                endpoint,
                json={
                    **common,
                    "action": "parts",
                    "part_numbers": needed[offset : offset + 128],
                },
            )
        )["parts"]

        def transfer(part):
            start = (part["part_number"] - 1) * part_size
            length = min(part_size, size - start)

            def chunks():
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        block = stream.read(min(1024**2, remaining))
                        if not block:
                            raise ValueError("Input was truncated during upload")
                        remaining -= len(block)
                        yield block

            result = objects.put(
                part["url"], content=chunks(), headers={"Content-Length": str(length)}
            )
            if result.is_error:
                raise RuntimeError(
                    f"Multipart part {part['part_number']} HTTP {result.status_code}"
                )
            return part["part_number"]

        with ThreadPoolExecutor(max_workers=4) as executor:
            for number in executor.map(transfer, handles):
                if number % 10 == 0 or number == total:
                    print(
                        json.dumps({"multipart_part_transferred": number, "of": total}),
                        flush=True,
                    )
    return checked(client.post(endpoint, json={**common, "action": "complete"}))


def main(*, require_qa=True):
    parser = argparse.ArgumentParser(description=__doc__)
    if require_qa:
        parser.add_argument("--qa-env", type=Path, required=True)
    else:
        parser.add_argument("--api-key-env", default="SCIENTIFIC_MODELS_API_KEY")
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Upload inputs and save request.json without starting GPU work",
    )
    parser.add_argument(
        "--reference",
        type=Path,
        help="Reference archive from an earlier matching scVI/scANVI run, for map-query",
    )
    parser.add_argument(
        "--idempotency-key",
        help="Reuse on retries; choose a new value for an intentional repeat",
    )
    parser.add_argument(
        "--sha256",
        help="Previously recorded input SHA-256; the server still verifies every uploaded byte",
    )
    parser.add_argument(
        "--admission-wait-seconds",
        type=int,
        default=900,
        help="Wait for this API key's available slots; does not change server concurrency (default 900)",
    )
    args = parser.parse_args()
    os.umask(0o077)
    if require_qa:
        values = dict(
            line.split("=", 1)
            for line in args.qa_env.read_text().splitlines()
            if "=" in line
        )
        key = values["SCIENTIFIC_MODELS_API_KEY"]
    else:
        key = os.environ.get(args.api_key_env)
        if not key:
            parser.error(f"Set the API key in environment variable {args.api_key_env}")
    with httpx.Client(
        base_url=args.origin,
        headers={"Authorization": "Bearer " + key},
        timeout=httpx.Timeout(900, connect=15),
        trust_env=False,
    ) as client:
        me = checked(client.get("/v1/me"))
        if require_qa and (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError(
                "Qualification requires existing system/qa; never a customer key"
            )
        save(args.output / "caller.json", me)
        print(
            json.dumps(
                {
                    "identity": me["tenant_id"] + "/" + me["principal_id"],
                    "authenticated": True,
                }
            ),
            flush=True,
        )
        if args.input is None:
            return

        def upload(path, media_type, suffix, compression="none"):
            receipt = args.output / (suffix + "-artifact.json")
            if suffix == "anndata" and args.sha256:
                if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
                    raise ValueError("Invalid previously recorded SHA-256")
                digest = args.sha256
            else:
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
            size = path.stat().st_size
            if receipt.exists():
                value = json.loads(receipt.read_text())
                if (value["sha256"], value["size_bytes"]) != (digest, size):
                    raise ValueError(
                        "Input changed; use a new qualification run directory"
                    )
                return value
            reservation_receipt = args.output / (suffix + "-upload.json")
            completion_receipt = args.output / (suffix + "-transfer-completed.json")
            if reservation_receipt.exists():
                previous = json.loads(reservation_receipt.read_text())
                status = checked(
                    client.get(f"/v1/operations/{previous['operation_id']}")
                )
                # A lost finalize response must not restart a completed
                # multipart upload or attempt to overwrite its write-once key.
                if completion_receipt.exists() or status["status"] == "succeeded":
                    ref = checked(
                        client.post(
                            f"/v1/scientific-artifacts/uploads/{previous['upload_id']}:finalize",
                            json={"operation_id": previous["operation_id"]},
                        )
                    )
                    if (ref["sha256"], ref["size_bytes"]) != (digest, size):
                        raise ValueError("Input changed; use a new run directory")
                    save(receipt, ref)
                    print(
                        json.dumps(
                            {
                                "uploaded": suffix,
                                "size_bytes": size,
                                "verified": True,
                                "resumed_finalization": True,
                            }
                        ),
                        flush=True,
                    )
                    return ref
            reserved = post_with_admission_wait(
                client,
                "/v1/scientific-artifacts/uploads",
                wait_seconds=args.admission_wait_seconds,
                json={
                    "model_id": "scvi-scanvi",
                    "sha256": digest,
                    "size_bytes": size,
                    "media_type": media_type,
                    "compression": compression,
                },
                headers={"Idempotency-Key": "scvi-input-v1-" + suffix + "-" + digest},
            )
            save(
                args.output / (suffix + "-upload.json"),
                {k: reserved[k] for k in ("operation_id", "upload_id")},
            )
            status = checked(client.get(f"/v1/operations/{reserved['operation_id']}"))
            if status["status"] == "succeeded":
                ref = checked(
                    client.post(
                        f"/v1/scientific-artifacts/uploads/{reserved['upload_id']}:finalize",
                        json={"operation_id": reserved["operation_id"]},
                    )
                )
                if (ref["sha256"], ref["size_bytes"]) != (digest, size):
                    raise ValueError("The reused upload does not match this input")
                save(receipt, ref)
                print(
                    json.dumps(
                        {
                            "uploaded": suffix,
                            "size_bytes": size,
                            "verified": True,
                            "reused_verified_upload": True,
                        }
                    ),
                    flush=True,
                )
                return ref
            headers = {**reserved["handle"]["headers"], "content-length": str(size)}
            started = time.monotonic()
            # Separate client: do not send the platform bearer to object storage.
            with httpx.Client(
                timeout=httpx.Timeout(900, connect=15), trust_env=False
            ) as objects:
                if size > 100 * 1024**2:
                    multipart_file(
                        client,
                        objects,
                        reserved,
                        path,
                        size,
                        args.output / (suffix + "-multipart.json"),
                    )
                else:
                    with path.open("rb") as handle:
                        response = objects.put(
                            reserved["handle"]["url"], content=handle, headers=headers
                        )
                    if response.is_error:
                        raise RuntimeError(f"Object upload HTTP {response.status_code}")
            save(completion_receipt, {"sha256": digest, "size_bytes": size})
            ref = checked(
                client.post(
                    f"/v1/scientific-artifacts/uploads/{reserved['upload_id']}:finalize",
                    json={"operation_id": reserved["operation_id"]},
                )
            )
            assert (ref["sha256"], ref["size_bytes"]) == (digest, size)
            save(receipt, ref)
            save(
                args.output / (suffix + "-transfer.json"),
                {
                    "size_bytes": size,
                    "seconds": time.monotonic() - started,
                    "verified": True,
                },
            )
            print(
                json.dumps({"uploaded": suffix, "size_bytes": size, "verified": True}),
                flush=True,
            )
            return ref

        data = upload(args.input, "application/x-hdf5", "anndata")
        if args.stage_only:
            return
        if args.parameters is None:
            raise ValueError("Supply explicit parameters to submit a GPU job")
        manifest = {
            "schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1",
            "manifest_id": "scvi-whitelab-" + data["sha256"],
            "entries": [
                {
                    "name": "anndata",
                    "semantic_type": "anndata-counts/v1",
                    "artifact": data,
                }
            ],
        }
        if args.reference:
            reference = upload(args.reference, "application/x-tar", "reference", "gzip")
            manifest["entries"].append(
                {
                    "name": "reference",
                    "semantic_type": "scvi-reference/v1",
                    "artifact": reference,
                }
            )
        manifest["manifest_id"] = (
            "scvi-"
            + hashlib.sha256(
                json.dumps(manifest["entries"], sort_keys=True).encode()
            ).hexdigest()
        )
        save(args.output / "manifest.json", manifest)
        pointer = upload(
            args.output / "manifest.json",
            "application/vnd.fs2.scientific-manifest+json",
            "manifest",
        )
        body = {
            "schema": "fs2-serve.nebius.ai/scientific-run-request/v1",
            "operation": "fit-transform",
            "service_class": "customer-batch",
            "input_manifest": pointer,
            "parameters": json.loads(args.parameters.read_text()),
        }
        identity = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        save(args.output / "request.json", body)
        if args.prepare_only:
            print(
                json.dumps(
                    {"prepared": True, "request": str(args.output / "request.json")}
                ),
                flush=True,
            )
            return
        response = post_with_admission_wait(
            client,
            "/v1/models/scvi-scanvi:submit",
            wait_seconds=args.admission_wait_seconds,
            json=body,
            headers={
                "Idempotency-Key": args.idempotency_key or "scvi-run-v1-" + identity
            },
        )
        save(args.output / "request.json", body)
        save(args.output / "admission.json", response)
        print(
            json.dumps(
                {
                    "submitted": True,
                    "operation_id": response["operation"]["id"],
                    "receipt": str(args.output / "admission.json"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()

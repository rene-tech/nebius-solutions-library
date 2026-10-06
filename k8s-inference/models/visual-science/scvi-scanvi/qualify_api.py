"""Internal system/qa client for the hosted single-cell job qualification.

Presigned upload handles stay in memory. Receipts contain only immutable
artifact/operation identities; credentials and input bytes are not logged.
Repeated invocations reuse their saved artifact and run identities.
"""

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def checked(response):
    if response.is_error:
        raise RuntimeError(
            f"HTTP {response.status_code}; request {response.headers.get('x-request-id', 'unknown')}"
        )
    return response.json()


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


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
            with path.open("rb") as stream:
                stream.seek(start)
                content = stream.read(min(part_size, size - start))
            result = objects.put(part["url"], content=content)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-env", type=Path, required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--parameters", type=Path)
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    values = dict(
        line.split("=", 1)
        for line in args.qa_env.read_text().splitlines()
        if "=" in line
    )
    key = values["SCIENTIFIC_MODELS_API_KEY"]
    with httpx.Client(
        base_url=args.origin,
        headers={"Authorization": "Bearer " + key},
        timeout=httpx.Timeout(900, connect=15),
        trust_env=False,
    ) as client:
        me = checked(client.get("/v1/me"))
        if (me["tenant_id"], me["principal_id"]) != ("system", "qa"):
            raise ValueError(
                "Qualification requires existing system/qa; never a customer key"
            )
        save(args.output / "caller.json", me)
        print(json.dumps({"identity": "system/qa", "authenticated": True}), flush=True)
        if args.input is None:
            return

        def upload(path, media_type, suffix):
            receipt = args.output / (suffix + "-artifact.json")
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
            reserved = checked(
                client.post(
                    "/v1/scientific-artifacts/uploads",
                    json={
                        "model_id": "scvi-scanvi",
                        "sha256": digest,
                        "size_bytes": size,
                        "media_type": media_type,
                        "compression": "none",
                    },
                    headers={
                        "Idempotency-Key": "scvi-whitelab-20261006-"
                        + suffix
                        + "-"
                        + digest
                    },
                )
            )
            save(
                args.output / (suffix + "-upload.json"),
                {k: reserved[k] for k in ("operation_id", "upload_id")},
            )
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
        response = checked(
            client.post(
                "/v1/models/scvi-scanvi:submit",
                json=body,
                headers={"Idempotency-Key": "scvi-whitelab-20261006-" + identity},
            )
        )
        save(args.output / "request.json", body)
        save(args.output / "admission.json", response)
        print(
            json.dumps(
                {"submitted": True, "receipt": str(args.output / "admission.json")}
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()

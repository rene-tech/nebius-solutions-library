"""Read-only exact-image recovery of an existing successful customer operation.

There are no source, submission, cancellation or GPU arguments. Credentials
enter Docker through environment only. Full client output stays in a private
sibling log; stdout contains a compact sanitized verification record.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import UUID


CLIENT_FILES = ("/opt/bionemo/invoke-scientific-batch.py", "/opt/bionemo/scientific_receipts.py")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def save_private(path: Path, value: dict) -> None:
    with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        stream.write(json.dumps(value, indent=2) + "\n")


def command(image: str, operation: str, output: Path) -> list[str]:
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("pin the exact client image digest")
    operation = str(UUID(operation))
    return ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
            "--entrypoint", "/opt/scientific-client/bin/python",
            "--env", "SCIENTIFIC_MODELS_API_KEY", "--env", "SCIENTIFIC_MODELS_MCP_URL",
            "--mount", f"type=bind,src={output.resolve()},dst=/recovery",
            image, CLIENT_FILES[0], "--recover-operation-id", operation, "--output", "/recovery"]


def verify_downloads(output: Path, previous: Path, operation: str) -> dict:
    receipt = read(output / "recovery-receipt.json")
    status = read(output / "status.json")
    result = read(output / "result.json")
    original = read(previous / "receipt.json")
    if not (receipt["state"] == original["state"] == "verified"
            and receipt["operation_id"] == original["operation_id"] == status["operation"]["id"] == result["operation_id"] == operation):
        raise ValueError("recovery does not match the same completed operation")
    if receipt["identity"]["caller_fingerprint"] != original["identity"]["caller_fingerprint"] or receipt["identity"]["endpoint"] != original["identity"]["endpoint"]:
        raise ValueError("recovery changed the owner or endpoint")
    if status["operation"]["status"] != "succeeded" or not status["batch"]["result_published"] or result["terminal_status"] != "succeeded":
        raise ValueError("existing operation no longer has a published successful result")
    if (output / "output-manifest.json").read_bytes() != (previous / "output-manifest.json").read_bytes():
        raise ValueError("fresh manifest differs from original verified scientific output")
    manifest = read(output / "output-manifest.json")
    entries, downloads = manifest["entries"], receipt["verified_artifacts"]
    if not entries or len(entries) != len(downloads):
        raise ValueError("recovery did not download the complete manifest")
    records = [(output / "output-manifest.json", receipt["output_manifest"], result["output_manifest"])]
    records += [(output / f"output-{i:02d}.artifact", download, entry["artifact"])
                for i, (entry, download) in enumerate(zip(entries, downloads))]
    for path, downloaded, reference in records:
        if any(downloaded[k] != reference[k] for k in ("artifact_id", "sha256", "size_bytes")):
            raise ValueError("download identity differs from published artifact")
        if path.is_symlink() or not path.is_file() or path.stat().st_size != reference["size_bytes"] or sha(path) != reference["sha256"]:
            raise ValueError("freshly downloaded artifact failed independent size/SHA verification")
        if downloaded["publication"] != "verified-copy" or downloaded["transfer_attempts"] < 1:
            raise ValueError("fresh recovery reused local artifacts instead of transferring them")
    return {
        "status": "passed", "model_id": status["operation"]["model_id"], "operation_id": operation,
        "verified_artifacts": len(downloads), "verified_artifact_bytes": sum(d["size_bytes"] for d in downloads),
        "fresh_content_downloads_including_manifest": len(records),
        "transport_attempts_including_manifest": sum(d["transfer_attempts"] for _, d, _ in records),
        "all_downloads_publication": "verified-copy", "original_manifest_byte_identity": True,
        "network_get_evidence": "Fresh empty directory + verified-copy receipts + inspected immutable client GET path; not a packet capture.",
        "inference_submitted": False, "upload_or_cancel_requested": False,
        "scientific_simulations_rerun": False,
        "output_manifest_sha256": sha(output / "output-manifest.json"),
        "recovery_receipt_sha256": sha(output / "recovery-receipt.json"),
        "original_receipt_sha256": sha(previous / "receipt.json"),
        "original_operation_completed_at": status["operation"].get("completed_at"),
    }


def run(args) -> dict:
    operation = str(UUID(args.operation_id))
    invocation = command(args.image, operation, args.output)
    previous = read(args.previous_receipt / "receipt.json")
    secret = read(args.key_file)["secret"]
    if previous["state"] != "verified" or previous["operation_id"] != operation:
        raise ValueError("supply the original verified receipt for this operation")
    if previous["identity"]["caller_fingerprint"] != hashlib.sha256(secret.encode()).hexdigest() or previous["identity"]["endpoint"] != args.mcp_url:
        raise ValueError("use the same ordinary owner key and endpoint")
    image_hashes = subprocess.check_output(["docker", "run", "--rm", "--network", "none",
        "--entrypoint", "/usr/bin/sha256sum", args.image, *CLIENT_FILES], text=True)
    hashes = {line.split()[1]: line.split()[0] for line in image_hashes.splitlines()}
    if set(hashes) != set(CLIENT_FILES) or any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes.values()):
        raise ValueError("could not identify exact installed client sources")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    log = args.output.with_name(args.output.name + ".client.log")
    summary_path = args.output.with_name(args.output.name + ".summary.json")
    started = datetime.now(timezone.utc).isoformat()
    env = {**os.environ, "SCIENTIFIC_MODELS_API_KEY": secret, "SCIENTIFIC_MODELS_MCP_URL": args.mcp_url}
    start = time.monotonic()
    with open(log, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        process = subprocess.run(invocation, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=900)
    if process.returncode:
        summary = {"status": "failed", "operation_id": operation, "client_image": args.image,
                   "client_exit_code": process.returncode, "private_log": str(log),
                   "inference_submitted": False, "scientific_simulations_rerun": False}
        save_private(summary_path, summary)
        return summary
    summary = {**verify_downloads(args.output, args.previous_receipt, operation),
               "client_image": args.image, "installed_client_source_sha256": hashes,
               "helper_source_sha256": sha(Path(__file__)), "started_at": started,
               "finished_at": datetime.now(timezone.utc).isoformat(),
               "readback_elapsed_seconds": time.monotonic() - start,
               "private_output_directory": str(args.output), "private_log": str(log)}
    save_private(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--previous-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New, nonexistent directory; no original receipts or artifacts are copied.")
    parser.add_argument("--mcp-url", required=True)
    summary = run(parser.parse_args())
    print(json.dumps(summary))
    raise SystemExit(0 if summary["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

"""Scientific and delivery gates for a real late-state public continuation."""

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import re


def topology_equivalence(text, target_step):
    """Fail closed on differences, not merely gmx check's zero process exit."""
    start = text.find("comparing inputrec")
    if start < 0:
        raise ValueError("No complete native TPR comparison")
    body = text[start:]
    # GROMACS' closing quote is stderr and may split a buffered stdout line.
    body = re.sub(r"\nGROMACS reminds you: [^\n]*\n", "", body)
    body = re.sub(r"comparing t\s+_resinfo", "comparing t_resinfo", body)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    differences = [line for line in lines if not line.startswith("comparing ")]
    if differences != [f"inputrec->nsteps (500000000 - {target_step})"]:
        raise ValueError("TPR comparison found an unapproved difference or incomplete diagnostic")
    required = {"inputrec", "mtop topology", "force field parameters", "atoms", "InteractionLists",
                "molecule blocks", "groups", "intermolecular exclusions", "moleculeBlockIndices",
                "flags", "box", "box_rel", "boxv", "x", "v"}
    compared = {line.removeprefix("comparing ") for line in lines if line.startswith("comparing ")}
    if not required <= compared:
        raise ValueError("Native comparison did not cover complete topology, coordinates and velocities")
    return {"status": "passed", "only_difference": "nsteps", "target_step": target_step,
            "required_sections": sorted(required), "relative_tolerance": 0, "absolute_tolerance": 0}


def preserved_history(fixture, checkpoint):
    indexed = {row["path"]: row for row in checkpoint["files"]}
    for old in fixture["source_files"]:
        current = indexed.get(f"source-history/{old['path']}", {})
        if (current.get("sha256"), current.get("size_bytes")) != (old["sha256"], old["size_bytes"]):
            raise ValueError("Original late native history was lost or changed")
    return len(fixture["source_files"])


def restart_step(text):
    starts = [int(value) for value in re.findall(r"continuing from step\s+(\d+)", text, re.I)]
    if len(starts) != 1:
        raise ValueError("Native restart log must identify exactly one saved starting step")
    return starts[0]


def summarize_progress(checkpoint):
    native = [row for row in checkpoint["state"]["commands"] if "mdrun" in row["command"]]
    good = [row for row in native if row["exit_code"] == 0]
    return {"generation": checkpoint["state"]["generation"],
            "checkpoint_step": max((row.get("checkpoint_step") or 0 for row in native), default=0),
            "native_elapsed_seconds": sum(row["wall_seconds"] for row in native),
            "native_segments": len(native), "zero_exit_segments": len(good),
            "native_ns_per_day": [row["performance_ns_per_day"] for row in good
                                  if row.get("performance_ns_per_day") is not None],
            "workspace_files": len(checkpoint["files"]),
            "workspace_bytes": sum(row["size_bytes"] for row in checkpoint["files"])}


def delivery_gate(fixture, source_step, final_status, checkpoint):
    progress = summarize_progress(checkpoint)
    if final_status["batch"]["status"] != "succeeded" or not final_status["batch"]["result_published"]:
        raise ValueError("Public operation is not durably successful")
    if progress["checkpoint_step"] != fixture["target_step"] or progress["native_segments"] != progress["zero_exit_segments"]:
        raise ValueError("Native continuation did not finish the finite horizon cleanly")
    operation = final_status["operation"]
    wall = (datetime.fromisoformat(operation["completed_at"]) - datetime.fromisoformat(operation["accepted_at"])).total_seconds()
    new_ns = (fixture["target_step"] - source_step) * float(fixture["dt_ps"]) / 1000
    if wall <= 0 or new_ns <= 0:
        raise ValueError("Missing positive accepted-to-durable-completion work/time")
    delivered = new_ns * 86400 / wall
    native = new_ns * 86400 / progress["native_elapsed_seconds"]
    if not all(math.isfinite(value) for value in (delivered, native)):
        raise ValueError("Performance measurements are non-finite")
    # Return all evidence before the caller declares a failed quality gate.
    gates = {"minimum_delivered_rate": delivered >= fixture["minimum_delivered_ns_per_day"],
             "minimum_native_duration": progress["native_elapsed_seconds"] >= fixture["minimum_native_seconds"]}
    return {**progress, "resumed_from_step": source_step, "newly_completed_ns": new_ns,
            "accepted_to_durable_seconds": wall, "delivered_ns_per_day": delivered,
            "native_useful_ns_per_day": native, "gates": gates,
            "status": "passed" if all(gates.values()) else "failed"}


def verify_customer_storage(client, checkpoint, *, expected_bucket):
    """Read the independently exported manifest and all unique object bytes."""
    location = checkpoint["customer_storage"]
    if location["bucket"] != expected_bucket:
        raise ValueError("Not the assigned demo output bucket")
    response = client.get_object(Bucket=expected_bucket, Key=location["manifest_key"])
    try:
        raw = response["Body"].read(32 * 1024**2 + 1)
    finally:
        response["Body"].close()
    if len(raw) > 32 * 1024**2 or len(raw) != response["ContentLength"]:
        raise ValueError("Customer manifest exceeds its length bound")
    document = json.loads(raw)
    if (document["state"]["operation_id"], document["state"]["generation"], document["bucket"]) != (
        checkpoint["state"]["operation_id"], checkpoint["state"]["generation"], expected_bucket,
    ):
        raise ValueError("Customer export does not identify this committed generation")
    expected = {row["path"]: (row["sha256"], row["size_bytes"]) for row in checkpoint["files"]}
    exported = {row["path"]: (row["sha256"], row["size_bytes"]) for row in document["files"]}
    if len(document["files"]) != len(exported) or exported != expected:
        raise ValueError("Customer export lost or changed a native file")
    unique = {}
    for row in document["files"]:
        if row["key"] != f"{location['prefix']}/objects/{row['sha256']}":
            raise ValueError("Customer manifest points outside its content-addressed run")
        unique[row["key"]] = row

    def check(row):
        obj = client.get_object(Bucket=expected_bucket, Key=row["key"])
        digest, size = hashlib.sha256(), 0
        try:
            if obj["ContentLength"] != row["size_bytes"]:
                raise ValueError("Customer object length changed")
            while block := obj["Body"].read(4 * 1024**2):
                size += len(block)
                if size > row["size_bytes"]:
                    raise ValueError("Customer object exceeds its declared length")
                digest.update(block)
        finally:
            obj["Body"].close()
        if (size, digest.hexdigest()) != (row["size_bytes"], row["sha256"]):
            raise ValueError("Customer object bytes do not match committed native output")
        return size

    pending, sizes = list(unique.values()), []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for offset in range(0, len(pending), 64):
            sizes.extend(pool.map(check, pending[offset:offset + 64]))
    return {"status": "passed", "files": len(exported), "unique_objects": len(unique),
            "unique_bytes": sum(sizes), "sha256_checked": True, "parallel_reads": 4,
            "manifest_sha256": hashlib.sha256(raw).hexdigest()}

"""Retain admitted plans for benchmark receipts using a bounded read-only query.

Public operation status omits the frozen execution mode. Never infer JobSet
identity from a job named 'gang', or export credentials/the entire stored state.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from activate_mpi import CONTEXT


def plan_query(operation_ids):
    identifiers = sorted({str(UUID(value)) for value in operation_ids})
    if not 1 <= len(identifiers) <= 128:
        raise ValueError("Select one to 128 exact receipt operation IDs")
    values = ",".join(f"'{value}'::uuid" for value in identifiers)
    return ("SELECT json_build_object('operation_id',operation_id,'tenant_id',tenant_id,"
            "'model_id',model_id,'plan',state->'plan') FROM fs2_scientific_batches "
            f"WHERE operation_id=ANY(ARRAY[{values}]) AND tenant_id='system' "
            "AND model_id IN ('gromacs','gromacs-mpi') ORDER BY operation_id;")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    paths = {}
    for campaign in args.campaign:
        for path in sorted(campaign.glob("cohort-*/*/receipt.json")):
            receipt = json.loads(path.read_text())
            if receipt.get("operation_id"):
                operation = str(UUID(receipt["operation_id"]))
                paths.setdefault(operation, []).append(path.parent / "frozen-plan.json")
    if not paths:
        raise ValueError("No submitted operation receipts in selected campaigns")
    kube = ["kubectl", "--context", CONTEXT, "--request-timeout=20s", "-n", "fs2-data"]
    cluster = json.loads(subprocess.check_output(
        [*kube, "get", "cluster.postgresql.cnpg.io", "fs2-control-db", "-o", "json"], timeout=25))
    primary = cluster["status"]["currentPrimary"]
    database = cluster["spec"]["bootstrap"]["initdb"]["database"]
    raw = subprocess.check_output([
        *kube, "exec", primary, "-c", "postgres", "--", "env",
        "PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=10000",
        "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", database,
        "-c", plan_query(paths),
    ], timeout=35)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len(rows) != len(paths) or {row["operation_id"] for row in rows} != set(paths):
        raise ValueError("Read-only source did not return every exact internal benchmark operation")
    for row in rows:
        if row["tenant_id"] != "system" or not isinstance(row["plan"], dict):
            raise ValueError("Invalid frozen internal plan")
        for destination in paths[row["operation_id"]]:
            if destination.exists():
                previous = json.loads(destination.read_text())
                if any(previous.get(key) != value for key, value in row.items()):
                    raise ValueError("Previously captured immutable plan changed")
                continue
            with destination.open("x") as handle:
                json.dump({**row, "captured_at": datetime.now(timezone.utc).isoformat(),
                           "source": "postgresql-admitted-plan"}, handle, indent=2)
                handle.write("\n")
    print(json.dumps({"operations": len(rows), "read_only": True, "full_state_exported": False}))


if __name__ == "__main__":
    main()

"""Prebuild migration0038's exact non-unique index without blocking writers.

Uses the existing migration owner/Secret and immutable live image. This does not
advance the migration ledger: that is a separate coordinated reader release.
Default is a server dry-run. Never prints database credentials or state values.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = "nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a"
NAME = "fs2-scientific-claim-index-20261005"
IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-serve-control-plane"
    "@sha256:64c5c77d4eb6cac4b4ecf58d16766b2aa0423f836c1b9adee5a2034abd3e3644"
)
MIGRATION = (
    ROOT / "components/control-plane/migrations/0038_scientific_claimable_index.sql"
)
MIGRATION_SHA = "fed73c3987c65954c165f1359c1be2c8e79025d64d6006c2b728bc7c94aa7f91"
EXPECTED_INDEX = (
    "CREATE INDEX fs2_scientific_batches_pending_claim_idx ON public.fs2_scientific_batches "
    "USING btree (status, lease_expires_at, operation_id) WHERE ((status = ANY "
    "(ARRAY['queued'::text, 'running'::text])) OR ((status = ANY "
    "(ARRAY['succeeded'::text, 'failed'::text, 'cancelled'::text])) AND "
    "(((state ->> 'result_published'::text))::boolean = false)))"
)
PROGRAM = """
import ast, asyncio, inspect, json, os, textwrap, time
from datetime import datetime, timezone
import asyncpg
from fs2_serve.postgresql_release import EXPECTED_MIGRATIONS
from fs2_serve.scientific_batch.postgres_repository import PostgresScientificBatchRepository

async def main():
    dsn = os.environ["FS2_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn, command_timeout=180, server_settings={
        "application_name":"fs2-serve-migration", "lock_timeout":"5s", "statement_timeout":"120s"})
    try:
        await conn.execute("SELECT pg_advisory_lock(727201920001)")
        before = [(r["version"],r["sha256"]) for r in await conn.fetch(
            "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY applied_at,version")]
        if before != list(EXPECTED_MIGRATIONS) or len(before) != 37:
            raise RuntimeError("Expected exact existing37-migration release; no DDL performed")
        query = next(n.value for n in ast.walk(ast.parse(textwrap.dedent(
            inspect.getsource(PostgresScientificBatchRepository.claim_next))))
            if isinstance(n,ast.Constant) and isinstance(n.value,str) and "WITH candidate AS" in n.value)
        plan_before = json.loads(await conn.fetchval("EXPLAIN (FORMAT JSON) " + query, "index-proof",30.0))
        existing = await conn.fetchrow("SELECT pg_get_indexdef(indexrelid) AS definition,indisvalid,indisready "
            "FROM pg_index WHERE indexrelid=to_regclass('public.fs2_scientific_batches_pending_claim_idx')")
        if existing and (existing["definition"] != EXPECTED_INDEX or not existing["indisvalid"] or not existing["indisready"]):
            raise RuntimeError("Existing index differs or is incomplete; operator inspection required")
        started = datetime.now(timezone.utc).isoformat()
        clock = time.monotonic()
        if not existing:
            await conn.execute(INDEX_SQL)
        await conn.execute("ANALYZE fs2_scientific_batches")
        row = await conn.fetchrow("SELECT pg_get_indexdef(indexrelid) AS definition,indisvalid,indisready "
            "FROM pg_index WHERE indexrelid=to_regclass('public.fs2_scientific_batches_pending_claim_idx')")
        if not row or row["definition"] != EXPECTED_INDEX or not row["indisvalid"] or not row["indisready"]:
            raise RuntimeError("Index did not reach the exact valid/ready definition")
        after = [(r["version"],r["sha256"]) for r in await conn.fetch(
            "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY applied_at,version")]
        if before != after:
            raise RuntimeError("Migration ledger changed unexpectedly")
        plan_after = json.loads(await conn.fetchval("EXPLAIN (FORMAT JSON) " + query,"index-proof",30.0))
        print(json.dumps({"status":"passed","started_at":started,"finished_at":datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds":time.monotonic()-clock,"preexisting":bool(existing),"index":dict(row),
            "migration_ledger_unchanged":True,"plan_before":plan_before,"plan_after":plan_after}),flush=True)
    finally:
        await conn.close()
asyncio.run(main())
"""


def manifest():
    payload = MIGRATION.read_bytes()
    if hashlib.sha256(payload).hexdigest() != MIGRATION_SHA:
        raise ValueError("Reviewed migration SQL changed")
    sql = payload.decode().replace(
        "CREATE INDEX IF NOT EXISTS", "CREATE INDEX CONCURRENTLY", 1
    )
    job = copy.deepcopy(
        yaml.safe_load(
            (
                ROOT / "acceptance/customer-workbenches-20261002/migration-job.yaml"
            ).read_text()
        )
    )
    job["metadata"]["name"] = NAME
    job["metadata"].setdefault("annotations", {})["fs2.nebius.ai/task"] = (
        "fs2-lynx-gromacs-longrun-performance-r20261005"
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["image"] = IMAGE
    container["command"] = ["python", "-c"]
    container["args"] = [
        "INDEX_SQL="
        + repr(sql)
        + "\nEXPECTED_INDEX="
        + repr(EXPECTED_INDEX)
        + "\n"
        + PROGRAM
    ]
    return job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.apply and args.verify:
        parser.error("Apply and verify are separate operations")
    os.umask(0o077)
    base = [
        "kubectl",
        "--context",
        CONTEXT,
        "--request-timeout=30s",
        "-n",
        "fs2-system",
    ]
    if args.verify:
        job = json.loads(
            subprocess.check_output([*base, "get", "job", NAME, "-o", "json"])
        )
        if job["spec"]["template"]["spec"]["containers"][0]["image"] != IMAGE:
            raise RuntimeError("Job image identity differs")
        if job.get("status", {}).get("succeeded") != 1:
            raise RuntimeError("Online index job has not succeeded")
        lines = subprocess.check_output(
            [*base, "logs", "job/" + NAME], text=True
        ).splitlines()
        receipt = json.loads(lines[-1])
        if receipt.get("status") != "passed" or not receipt.get(
            "migration_ledger_unchanged"
        ):
            raise RuntimeError("Online index verification failed")
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "job.json").write_text(json.dumps(job, indent=2) + "\n")
        (args.output / "verification.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        print(json.dumps({"status": "passed", "output": str(args.output), "job": NAME}))
        return
    args.output.mkdir(parents=True, exist_ok=False)
    path = args.output / "job.json"
    path.write_text(json.dumps(manifest(), indent=2) + "\n")
    checked = subprocess.check_output(
        [*base, "create", "-f", str(path), "--dry-run=server", "-o", "json"]
    )
    (args.output / "server-dry-run.json").write_bytes(checked)
    if args.apply:
        applied = subprocess.check_output(
            [*base, "create", "-f", str(path), "-o", "json"]
        )
        (args.output / "created-job.json").write_bytes(applied)
    print(
        json.dumps(
            {
                "job": NAME,
                "applied": args.apply,
                "verified": False,
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()

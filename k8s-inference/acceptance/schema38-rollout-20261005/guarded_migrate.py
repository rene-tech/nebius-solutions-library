"""Exact 37 -> 38 release guard; run only in the reviewed migration Job.

The index must already have been built online. This delegates ledger changes to
the existing migrator; it never inserts ledger rows or rebuilds an index itself.
"""

import asyncio
import json
import os

import asyncpg

from fs2_serve.postgres import PostgresStore
from fs2_serve.postgresql_release import EXPECTED_MIGRATIONS
from fs2_serve.settings import Settings

MIGRATION = (
    "0038_scientific_claimable_index.sql",
    "fed73c3987c65954c165f1359c1be2c8e79025d64d6006c2b728bc7c94aa7f91",
)
EXPECTED_INDEX = (
    "CREATE INDEX fs2_scientific_batches_pending_claim_idx ON public.fs2_scientific_batches "
    "USING btree (status, lease_expires_at, operation_id) WHERE ((status = ANY "
    "(ARRAY['queued'::text, 'running'::text])) OR ((status = ANY "
    "(ARRAY['succeeded'::text, 'failed'::text, 'cancelled'::text])) AND "
    "(((state ->> 'result_published'::text))::boolean = false)))"
)


def validate_state(expected, ledger, index, *, before):
    if len(expected) != 38 or tuple(expected[-1]) != MIGRATION:
        raise RuntimeError("Candidate is not the reviewed 38-migration release")
    allowed = (expected[:37], expected) if before else (expected,)
    if ledger not in allowed:
        raise RuntimeError("Ledger is not the exact reviewed migration prefix")
    if not index or (
        index["definition"] != EXPECTED_INDEX
        or not index["indisvalid"]
        or not index["indisready"]
    ):
        raise RuntimeError("Exact online-built claim index is absent or incomplete")


async def inspect_state(dsn, expected, *, before):
    connection = await asyncpg.connect(
        dsn,
        command_timeout=10,
        server_settings={
            "application_name": "fs2-serve-migration",
            "statement_timeout": "10s",
            "lock_timeout": "5s",
        },
    )
    try:
        async with connection.transaction(readonly=True):
            ledger = [
                (str(row["version"]), str(row["sha256"]))
                for row in await connection.fetch(
                    "SELECT version,sha256 FROM fs2_schema_migrations ORDER BY applied_at,version"
                )
            ]
            index = await connection.fetchrow(
                "SELECT pg_get_indexdef(indexrelid) AS definition,indisvalid,indisready "
                "FROM pg_index WHERE indexrelid="
                "to_regclass('public.fs2_scientific_batches_pending_claim_idx')"
            )
            validate_state(expected, ledger, index, before=before)
            return len(ledger)
    finally:
        await connection.close()


async def main():
    expected = list(EXPECTED_MIGRATIONS)
    dsn = os.environ["FS2_DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    before = await inspect_state(dsn, expected, before=True)
    # Close the read-only preflight connection before the normal migrator takes
    # its own advisory transaction lock. Do not hold that lock in two sessions.
    # The release manager is the sole DDL owner during this small transition.
    if before == 37:
        settings = Settings()
        await PostgresStore.migrate_database(
            settings.database_url,
            settings.migrations_dir,
            settings.reporting_database_role,
            settings.runtime_database_role,
            settings.maintenance_database_role,
            settings.activation_database_role,
        )
    after = await inspect_state(dsn, expected, before=False)
    print(
        json.dumps(
            {
                "status": "passed",
                "ledger_before": before,
                "ledger_after": after,
                "prebuilt_index_verified": True,
                "already_applied": before == 38,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())

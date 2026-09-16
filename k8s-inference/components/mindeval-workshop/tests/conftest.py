import os
from pathlib import Path

import asyncpg
import pytest_asyncio
from cryptography.fernet import Fernet

from fs2_workshop.store import Store


@pytest_asyncio.fixture
async def store():
    # Dedicated disposable test database only, never a platform DSN.
    dsn = os.environ.get("WORKSHOP_TEST_DATABASE_URL", "postgresql://postgres@127.0.0.1:15496/postgres")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=12)
    for role in ("fs2_serve_runtime", "fs2_serve_reporting"):
        if not await pool.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
            await pool.execute(f"CREATE ROLE {role}")
    await pool.execute(Path(__file__).parents[1].joinpath("src/fs2_workshop/schema.sql").read_text())
    await pool.execute("TRUNCATE fs2_workshop.audio,fs2_workshop.events,fs2_workshop.runs,fs2_workshop.batches")
    yield Store(pool, Fernet.generate_key())
    await pool.close()

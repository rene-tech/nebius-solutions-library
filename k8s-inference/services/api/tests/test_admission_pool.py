"""Four concurrent creates on a four-connection pool: the create lock holds one pooled connection each, and
everything inside it (the admission record, ownership, the key ledger) reuses that connection instead of waiting
for a fifth (api 0.11.138; before: PoolTimeout after 30 s for every caller, Root's 2026-10-20 report)."""

import os
import threading
import time
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

import admissions
import db
import keys


@pytest.fixture
def database(monkeypatch):
    """A private schema on the test server with the PRODUCTION pool size (db.py: max_size 4), which is what the
    defect depends on; tests/test_database.py's fixture widens the pool and would hide it."""
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL is needed for PostgreSQL integration tests")
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(psycopg.sql.SQL("create schema {}").format(psycopg.sql.Identifier(schema)))
    url = make_conninfo(base, options="-c search_path=" + schema)
    try:
        with ConnectionPool(url, min_size=1, max_size=4, timeout=30, kwargs={"autocommit": True}) as pool:
            monkeypatch.setattr(db, "DATABASE_URL", url)
            monkeypatch.setattr(db, "_pool", pool)
            monkeypatch.setattr(keys, "LITELLM_MASTER_KEY", "")
            yield url
    finally:
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(psycopg.sql.SQL("drop schema {} cascade").format(psycopg.sql.Identifier(schema)))


def test_four_lock_holders_record_their_admissions_without_a_fifth_connection(database):
    db.migrate()
    with db.pool().connection() as c:  # the pool itself: this test must also run on the code before the fix
        c.execute("delete from run_admissions where operation like 'op-pool-%'")
    assert db.pool().max_size == 4
    ready = threading.Barrier(4, timeout=20)
    errors: list[BaseException] = []

    def create(i: int):
        job = {
            "metadata": {
                "name": f"op-pool-{i}",
                "labels": {"serverless2.nebius/mode": "run", "serverless2.nebius/tenant": "demo"},
                "annotations": {},
            },
            "spec": {"activeDeadlineSeconds": 600},
        }
        try:
            with keys.operation_lock(f"op-pool-{i}"):
                ready.wait()  # every holder owns one of the four pooled connections now
                keys.own_operation(f"op-pool-{i}", "demo", "owner", "llm-example")
                admissions.record(job, "eu-north1", None)
                assert admissions.get(f"op-pool-{i}") is not None
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=create, args=(i,)) for i in range(4)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    assert not any(t.is_alive() for t in threads), "a create is still waiting for a connection"
    assert errors == []
    assert time.time() - t0 < 20  # nowhere near the 30 s pool timeout
    with db.pool().connection() as c:
        assert c.execute("select count(*) from run_admissions where operation like 'op-pool-%'").fetchone()[0] == 4

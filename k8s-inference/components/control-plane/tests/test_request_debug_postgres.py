"""Actual isolated PostgreSQL migration/encryption/access contracts."""

import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from test_request_debug import NOW, row
from test_users_apps_postgres import database, operation, token  # noqa: F401

from fs2_serve.postgres import PostgresStore
from fs2_serve.request_debug import (
    _META_SCALAR_BUDGETS,
    PostgresDebugStore,
    _stored_payload_ceiling,
    body_capture,
    suppressed_body,
)

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def debug_database(database):  # noqa: F811
    await database.pool.execute("TRUNCATE fs2_request_debug")
    try:
        yield database
    finally:
        await database.pool.execute("TRUNCATE fs2_request_debug")


async def test_actual_encrypted_detail_pagination_unauthenticated_and_same_tenant_enrichment(debug_database):
    db = debug_database
    store = PostgresDebugStore(db.pool, db.cipher, max_body_bytes=64 * 1024)  # production-like cap wired
    key = await token(db)
    operation_id = await operation(db, key, model="boltz2")
    first = row(
        operation_id=operation_id,
        model_id=None,
        request_headers=[("x-api-key", "PRIVATE_KEY")],
        query_string="access_token=PRIVATE_QUERY&input_id=keep",
        request_body=body_capture(b"model-request-content", "text/plain", True),
        error_detail="useful upstream error",
    )
    second = row(started_at=NOW + timedelta(seconds=1), source="upstream", operation_attempt=2, upstream_attempt=1)
    unauthenticated = row(tenant_id=None, principal_id=None)
    for item in (first, second, unauthenticated):
        await store.record(item)
    await store.record(first)
    raw = await db.pool.fetchrow("SELECT * FROM fs2_request_debug WHERE id=$1", first.id)
    assert b"model-request-content" not in bytes(raw["ciphertext"])
    assert "PRIVATE_KEY" not in str(dict(raw)) and "PRIVATE_QUERY" not in str(dict(raw))
    assert not {"request_body", "request_headers", "error_detail", "query_string"} & set(raw.keys())
    detail = await store.get(first.id, "tenant-a")
    # first is BOUNDED (small bodies within the cap): decrypted + egress-sanitized, so the request is
    # served re-scrubbed while the stored free-text error_detail is failed closed to the generic marker.
    assert detail.model_id == "boltz2" and detail.request_body.data == "model-request-content"
    assert detail.request_headers == [("x-api-key", "[REDACTED]")]
    assert detail.error_detail == "[detail withheld on read]"
    assert await store.get(first.id, "tenant-b") is None
    assert await store.get(unauthenticated.id, "tenant-a") is None
    assert await store.get(unauthenticated.id) is not None
    page = await store.list(tenant_id="tenant-a", model_id="boltz2", limit=1)
    assert page.items[0].id == second.id and page.next_cursor
    tail = await store.list(tenant_id="tenant-a", model_id="boltz2", limit=1, cursor=page.next_cursor)
    assert [item.id for item in tail.items] == [first.id] and tail.next_cursor is None
    assert len((await store.list()).items) == 3
    assert len((await store.list(operation_id=operation_id)).items) == 1
    # An authenticated metadata binding cannot be changed to another customer.
    await db.pool.execute("UPDATE fs2_request_debug SET tenant_id='tampered' WHERE id=$1", first.id)
    with pytest.raises(InvalidTag):
        await store.get(first.id)


async def test_oversized_stored_ciphertext_is_metadata_only_on_detail_and_list_without_decrypt(debug_database):
    """SAI-01 regression (blockers 1 & 3): boundedness is gated on the ACTUAL STORED CIPHERTEXT LENGTH vs
    the WHOLE-EXCHANGE ceiling (not observed_bytes, not the redacted flag, not the per-body cap), and the
    ONE atomic CASE query returns the ciphertext only when octet_length <= ceiling (else NULL) so an
    over-ceiling row's bytes are never selected or decrypted (no TOCTOU, no eager decrypt). Even under a
    DEFAULT cap=None, an over-ceiling row is served METADATA-ONLY on BOTH detail and list. Proven by
    overwriting the ciphertext with an over-ceiling blob: the CASE yields NULL so a decrypt (which would
    raise InvalidTag on the garbage) never happens. This is the ancestor-88520758f case where redacted=True
    does NOT imply a tiny stored payload. Postgres-marked; run by CI."""
    db = debug_database
    store = PostgresDebugStore(db.pool, db.cipher)  # cap=None: the ceiling is derived from the hard per-body cap
    big = row(
        request_body=body_capture(b"small request body", "text/plain", True),
        error_detail="raw upstream detail with sk-OVERSIZE-LEAK",
    )
    await store.record(big)
    # Overwrite the ciphertext with a blob over the WHOLE-EXCHANGE ceiling: the CASE's octet_length guard
    # yields NULL, so the read path classifies the row non-bounded and NEVER selects/decrypts the bytes (a
    # decrypt of the garbage would raise InvalidTag). The redacted flag is irrelevant — only stored length.
    await db.pool.execute(
        "UPDATE fs2_request_debug SET ciphertext=$2 WHERE id=$1",
        big.id,
        b"\x00" * (_stored_payload_ceiling(None) + 1),
    )
    detail = await store.get(big.id, "tenant-a")
    assert detail is not None  # did NOT raise InvalidTag => never decrypted the oversized ciphertext
    assert detail.request_body.data == "[REDACTED]" and detail.request_body.redacted is True
    assert detail.response_body.data == "[REDACTED]"
    assert detail.request_headers == [] and detail.response_headers == [] and detail.query_string == ""
    assert "OVERSIZE-LEAK" not in (detail.error_detail or "")  # raw stored detail never disclosed
    summary = (await store.list(tenant_id="tenant-a")).items[0]
    assert summary.id == big.id
    assert summary.request_redacted is True and summary.response_redacted is True  # agrees with detail

    # And a row whose stored ciphertext is small stays BOUNDED and serves its safe request even with a huge
    # response WIRE length (the response is a withheld marker, so it does not enlarge the ciphertext).
    current = row(
        request_body=body_capture(b'{"input":"served"}', "application/json", True),
        response_body=suppressed_body("application/json", observed_bytes=5_000_000, complete=True),
    )
    await store.record(current)
    served = await store.get(current.id, "tenant-a")
    assert served is not None and served.request_body.data == '{"input":"served"}'  # request served
    assert served.response_body.data == "[REDACTED]"  # response withheld


async def test_legacy_unbounded_clear_columns_are_truncated_on_read(debug_database):
    """SAI-01 regression (blocker 1, PG clear columns): a LEGACY row whose CLEAR metadata columns were
    stored unbounded (before capture-time field budgets existed) must never TRANSFER unbounded clear metadata
    on read — not even when its ciphertext is over the ceiling and returned NULL (metadata-only view rendered
    from the clear columns alone). The SELECT bounds every attacker-influenced clear TEXT column in SQL
    (left(col, budget)) as a bounded TRANSFER, and the metadata-only view reasserts the exact per-field BYTE
    budget on the fetched value, so the returned view is provably BYTE-bounded regardless of what the row
    stored (byte-accurate even for MULTIBYTE content, where left() counts characters and can transfer up to
    ~4x the byte budget), and detail and list agree. Proven by overwriting the clear endpoint column with a
    huge MULTIBYTE attacker path and pushing the ciphertext over the ceiling. Postgres-marked; run by CI."""
    db = debug_database
    store = PostgresDebugStore(db.pool, db.cipher)
    legacy = row(model_id="boltz2")
    await store.record(legacy)
    # Simulate a legacy/attacker row: a huge MULTIBYTE clear endpoint path (each 'あ' is 3 UTF-8 bytes, so
    # left(col, budget) chars is ~3x the byte budget) AND a ciphertext over the ceiling so the read is
    # metadata-only and must render the endpoint from the clear column.
    await db.pool.execute(
        "UPDATE fs2_request_debug SET endpoint=$2, ciphertext=$3 WHERE id=$1",
        legacy.id,
        "/v1/" + "あ" * 500_000,
        b"\x00" * (_stored_payload_ceiling(None) + 1),
    )
    budget = _META_SCALAR_BUDGETS["endpoint"]
    detail = await store.get(legacy.id, "tenant-a")
    assert detail is not None  # never decrypted the over-ceiling ciphertext
    assert len(detail.endpoint.encode()) <= budget  # BYTE-exact bound on the fetched clear column
    assert "truncated" in detail.endpoint  # disclosed truncation marker
    summary = (await store.list(tenant_id="tenant-a")).items[0]
    assert summary.id == legacy.id and len(summary.endpoint.encode()) <= budget  # list agrees, byte-bounded


async def test_long_aad_identifiers_are_not_truncated_and_still_decrypt(debug_database):
    """SAI-01 regression (blocker 4, decryption-breaking regression): tenant_id and model_id are AES-GCM AAD.
    They must NEVER be truncated before decryption (via SQL left() or _bounded_text) or the AAD at decrypt time
    would not match what was sealed -> InvalidTag (an availability break). This proves a row whose tenant_id and
    model_id are LONGER than the old per-field truncation budget still decrypts cleanly on both detail and list
    (the AAD columns are fetched verbatim), while the whole-exchange ceiling still governs decryptability.
    Postgres-marked; run by CI."""
    db = debug_database
    store = PostgresDebugStore(db.pool, db.cipher, max_body_bytes=64 * 1024)
    long_tenant = "tenant-" + "x" * 2000  # far over the former 1024-byte truncation budget
    long_model = "model-" + "y" * 2000
    captured = row(
        tenant_id=long_tenant,
        model_id=long_model,
        request_body=body_capture(b'{"input":"served"}', "application/json", True),
    )
    await store.record(captured)  # AAD is sealed with the FULL tenant_id/model_id
    # Detail decrypts (no InvalidTag) because the AAD columns are fetched verbatim, not left()-truncated.
    detail = await store.get(captured.id, long_tenant)
    assert detail is not None and detail.request_body.data == '{"input":"served"}'
    assert detail.tenant_id == long_tenant and detail.model_id == long_model  # AAD identifiers intact on read
    # The list summary for the same row also decrypts and agrees (one shared derivation).
    summary = (await store.list(tenant_id=long_tenant)).items[0]
    assert summary.id == captured.id and summary.request_redacted is False


async def test_actual_generated_runtime_role_can_insert_list_and_decrypt(debug_database):
    db = debug_database
    suffix = uuid4().hex[:10]
    role = f"fs2_debug_runtime_{suffix}"
    await PostgresStore.migrate_database(
        os.environ["FS2_TEST_DATABASE_URL"],
        Path(__file__).parents[1] / "migrations",
        f"fs2_debug_reporting_{suffix}",
        role,
        f"fs2_debug_maintenance_{suffix}",
        f"fs2_debug_activation_{suffix}",
    )

    async def assume(connection):
        await connection.execute(f'SET ROLE "{role}"')  # noqa: S608 - locally generated test role

    pool = await asyncpg.create_pool(os.environ["FS2_TEST_DATABASE_URL"], min_size=1, max_size=1, init=assume)
    try:
        store = PostgresDebugStore(pool, db.cipher)
        captured = row()
        await store.record(captured)
        assert (await store.get(captured.id, "tenant-a")).response_body.data == captured.response_body.data
        assert len((await store.list(tenant_id="tenant-a")).items) == 1
        async with pool.acquire() as connection:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute("DELETE FROM fs2_request_debug")
    finally:
        await pool.close()

"""The customer history query uses real migrated PostgreSQL when configured."""

from datetime import UTC, datetime

import pytest
from test_users_apps_postgres import database as database
from test_users_apps_postgres import operation, token

from fs2_serve.auth import require_operation_access
from fs2_serve.models import Principal

pytestmark = pytest.mark.postgres


@pytest.mark.asyncio
async def test_customer_operation_history_postgres_acl_and_cursor(database):  # noqa: F811
    owner = await token(database)
    same_principal_other_key = await token(database)
    other_tenant = await token(database, tenant="other-lab")
    stamp = datetime(2026, 9, 18, tzinfo=UTC)
    own_ids = [
        await operation(database, owner, at=stamp, protocol=protocol)
        for protocol in ("openai-chat", "native", "scientific-batch-v1")
    ]
    sibling = await operation(database, same_principal_other_key, at=stamp)
    await operation(database, other_tenant, at=stamp)
    principal = Principal(
        token_id=owner.id,
        token_prefix=owner.prefix,
        principal_id=owner.principal_id,
        tenant_id=owner.tenant_id,
        scopes=frozenset(owner.scopes),
        models=frozenset(owner.models),
    )
    first = await database.list_customer_operations(principal, limit=2)
    second = await database.list_customer_operations(principal, limit=2, before=(first[-1].accepted_at, first[-1].id))
    assert [row.id for row in first + second] == sorted(own_ids, reverse=True)
    for row in first + second:
        require_operation_access(principal, row)
    admin = principal.model_copy(update={"scopes": frozenset({"tenant.admin"})})
    admin_rows = await database.list_customer_operations(admin, limit=200)
    assert {row.id for row in admin_rows} == set(own_ids + [sibling])
    with pytest.raises(ValueError):
        await database.list_customer_operations(principal, limit=202)

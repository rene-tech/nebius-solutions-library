"""Provider request contracts: exercise the generated SDK message types."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from grpc import StatusCode

from fs2_serve.user_storage_nebius import NebiusUserStorage, StorageOperationError


async def test_membership_has_no_name_and_explicit_key_is_recovered():
    provider = object.__new__(NebiusUserStorage)
    provider.project_id = "project-test"
    provider.prefix = "fs2-data"
    provider.accounts = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="sa-alice")))
    provider._operation = AsyncMock(return_value="membership-alice")
    provider.memberships = SimpleNamespace(
        list_members=AsyncMock(return_value=SimpleNamespace(memberships=[], next_page_token="")),
        create=Mock(),
    )
    name = provider.name("user", "tenant-a", "alice")
    provider.keys = SimpleNamespace(
        list_by_account=AsyncMock(
            return_value=SimpleNamespace(
                items=[SimpleNamespace(metadata=SimpleNamespace(name=name, id="key-alice"))],
                next_page_token="",
            )
        ),
        get_secret=AsyncMock(return_value=SimpleNamespace(aws_access_key_id="public-id", secret="test-secret")),
    )
    result = await provider.ensure_credentials("tenant-a", "alice", "group-a")
    request = provider.memberships.create.call_args.args[0]
    assert request.metadata.parent_id == "group-a"
    assert not request.metadata.name
    assert request.spec.member_id == "sa-alice"
    assert result["secret_access_key"] == "test-secret"
    assert provider.keys.get_secret.call_args.args[0].id == "key-alice"


async def test_completed_failed_operation_is_not_reported_as_success():
    op = SimpleNamespace(
        id="operation-fixture",
        resource_id="not-created",
        wait=AsyncMock(),
        successful=lambda: False,
        status=lambda: SimpleNamespace(code=StatusCode.RESOURCE_EXHAUSTED),
    )
    with pytest.raises(StorageOperationError) as caught:
        await NebiusUserStorage._operation(AsyncMock(return_value=op)())
    assert caught.value.code == "RESOURCE_EXHAUSTED"
    assert caught.value.operation_id == "operation-fixture"


async def test_successful_operation_returns_resource_id():
    op = SimpleNamespace(resource_id="resource-fixture", wait=AsyncMock(), successful=lambda: True)
    assert await NebiusUserStorage._operation(AsyncMock(return_value=op)()) == "resource-fixture"

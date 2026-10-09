"""Provider request contracts: exercise the generated SDK message types."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from grpc import StatusCode
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.storage import v1 as storage

from fs2_serve.user_storage_nebius import NebiusUserStorage, StorageOperationError


def naming_provider():
    provider = object.__new__(NebiusUserStorage)
    provider.project_id = "project-test"
    provider.prefix = "fs2-data"
    provider.region = "eu-north1"
    return provider


@pytest.mark.parametrize("tenant,owner,prefix", [
    ("kopra", "", "fs2-kopra-"),
    ("robotics", "timmothy", "fs2-robotics-timmothy-"),
    ("René Team", "Alice Smith", "fs2-rene-team-alice-smith-"),
    ("...", "???", "fs2-tenant-user-"),
])
def test_readable_bucket_name(tenant, owner, prefix):
    provider = naming_provider()
    name = provider.bucket_name(tenant, owner)
    assert name.startswith(prefix)
    assert len(name.rsplit("-", 1)[1]) == 16
    assert name == provider.bucket_name(tenant, owner)


def test_bucket_names_are_bounded_and_slug_collisions_are_disambiguated():
    provider = naming_provider()
    assert len(provider.bucket_name("t" * 100, "u" * 100)) == 63
    assert "-uuu" in provider.bucket_name("t" * 100, "u" * 100)
    assert len(provider.bucket_name("t" * 100, "")) == 63
    assert provider.bucket_name("tenant.a", "alice") != provider.bucket_name("tenant-a", "alice")
    assert provider.bucket_name("tenant", "alice") != provider.bucket_name("tenant", "")
    old = provider.bucket_name("tenant", "alice")
    provider.project_id = "another-project"
    assert provider.bucket_name("tenant", "alice") != old


async def test_existing_bucket_quota_change_preserves_immutable_name_and_iam_identity():
    provider = naming_provider()
    legacy = provider.name("bucket", "kopra", "")
    bucket = storage.Bucket(
        metadata=ResourceMetadata(id="bucket-same", parent_id="project-test", name=legacy,
                                  resource_version=7, labels={"fs2-storage-owner": legacy}),
        spec=storage.BucketSpec(max_size_bytes=5_000_000_000),
    )
    provider.groups = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="group-same")))
    provider._operation = AsyncMock(return_value="bucket-same")
    provider.buckets = SimpleNamespace(get=AsyncMock(return_value=bucket), update=Mock())
    result = await provider.ensure_bucket(
        "kopra", "", 6_000_000_000, existing={"bucket_id": "bucket-same", "group_id": "group-same"},
    )
    request = provider.buckets.update.call_args.args[0]
    assert request.metadata.id == "bucket-same"
    assert request.metadata.resource_version == 7
    assert request.metadata.labels["fs2-storage-owner"] == legacy
    assert request.metadata.name == legacy
    assert request.spec.max_size_bytes == 6_000_000_000
    assert result["bucket_id"] == "bucket-same"
    assert result["group_id"] == "group-same"
    # A crash between cloud quota update and DB update adopts the same identity.
    provider.buckets.update.reset_mock()
    await provider.ensure_bucket(
        "kopra", "", 6_000_000_000, existing={"bucket_id": "bucket-same", "group_id": "group-same"},
    )
    provider.buckets.update.assert_not_called()


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

"""Provider request contracts: exercise the generated SDK message types."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from grpc import StatusCode
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.storage import v1 as storage

from fs2_serve.crypto import KeyedHasher
from fs2_serve.user_storage_nebius import NebiusUserStorage, StorageOperationError


def naming_provider():
    provider = object.__new__(NebiusUserStorage)
    provider.project_id = "project-test"
    provider.prefix = "fs2-data"
    provider.region = "eu-north1"
    provider.key_ttl_days = 90
    provider.name_hasher = KeyedHasher(active_key_id="test", keys={"test": b"x" * 32})
    return provider


@pytest.mark.parametrize(
    "tenant,owner",
    [
        ("kopra", ""),
        ("robotics", "timmothy"),
        ("René Team", "Alice Smith"),
        ("...", "???"),
    ],
)
def test_opaque_bucket_name(tenant, owner):
    provider = naming_provider()
    name = provider.bucket_name(tenant, owner)
    assert name.startswith("fs2-")
    assert len(name) == 36
    if tenant.isascii() and tenant.isalnum():
        assert tenant.lower() not in name
    if owner.isascii() and owner.isalnum():
        assert owner.lower() not in name
    assert name == provider.bucket_name(tenant, owner)


def test_bucket_names_are_bounded_and_slug_collisions_are_disambiguated():
    provider = naming_provider()
    assert len(provider.bucket_name("t" * 100, "u" * 100)) == 36
    assert len(provider.bucket_name("t" * 100, "")) == 36
    assert provider.bucket_name("tenant.a", "alice") != provider.bucket_name("tenant-a", "alice")
    assert provider.bucket_name("tenant", "alice") != provider.bucket_name("tenant", "")
    old = provider.bucket_name("tenant", "alice")
    provider.project_id = "another-project"
    assert provider.bucket_name("tenant", "alice") != old


def test_s3_expiry_must_be_future_and_within_configured_ttl():
    provider = naming_provider()
    assert provider._require_bounded_expiry(datetime.now(UTC) + timedelta(days=89))
    for invalid in (None, datetime.now(UTC) - timedelta(seconds=1), datetime.now(UTC) + timedelta(days=91)):
        with pytest.raises(RuntimeError, match="expiry"):
            provider._require_bounded_expiry(invalid)


def test_customer_bucket_lifecycle_never_authorizes_object_deletion():
    rules = {rule.id: rule for rule in NebiusUserStorage.lifecycle().rules}

    assert set(rules) == {
        "expire-noncurrent-versions",
        "abort-incomplete-multipart-uploads",
    }
    assert all(rule.status == storage.LifecycleRule__Status.DISABLED for rule in rules.values())
    assert rules["expire-noncurrent-versions"].noncurrent_version_expiration.noncurrent_days == 30
    assert rules["abort-incomplete-multipart-uploads"].abort_incomplete_multipart_upload.days_after_initiation == 7


async def test_existing_unrelated_lifecycle_rule_is_preserved_but_forced_disabled():
    provider = naming_provider()
    legacy = provider.name("bucket", "kopra", "")
    unrelated = storage.LifecycleRule(
        id="operator-created-expiry",
        status=storage.LifecycleRule__Status.ENABLED,
        noncurrent_version_expiration=storage.LifecycleNoncurrentVersionExpiration(
            newer_noncurrent_versions=1,
            noncurrent_days=1,
        ),
    )
    historical = storage.LifecycleRule(
        id="expire-noncurrent-versions",
        status=storage.LifecycleRule__Status.ENABLED,
        noncurrent_version_expiration=storage.LifecycleNoncurrentVersionExpiration(
            newer_noncurrent_versions=9,
            noncurrent_days=91,
        ),
    )
    bucket = storage.Bucket(
        metadata=ResourceMetadata(
            id="bucket-same",
            parent_id="project-test",
            name=legacy,
            resource_version=7,
            labels={"fs2-storage-owner": legacy},
        ),
        spec=storage.BucketSpec(
            max_size_bytes=5_000_000_000,
            versioning_policy=storage.VersioningPolicy.ENABLED,
            lifecycle_configuration=storage.LifecycleConfiguration(
                rules=[unrelated, historical]
            ),
        ),
    )
    provider.groups = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="group-same")))
    provider._operation = AsyncMock(return_value="bucket-same")
    provider.buckets = SimpleNamespace(get=AsyncMock(return_value=bucket), update=Mock())

    await provider.ensure_bucket(
        "kopra",
        "",
        5_000_000_000,
        existing={"bucket_id": "bucket-same", "group_id": "group-same"},
    )

    request = provider.buckets.update.call_args.args[0]
    rules = {rule.id: rule for rule in request.spec.lifecycle_configuration.rules}
    assert set(rules) == {
        "operator-created-expiry",
        "expire-noncurrent-versions",
    }
    assert all(rule.status == storage.LifecycleRule__Status.DISABLED for rule in rules.values())
    assert rules["operator-created-expiry"].noncurrent_version_expiration.noncurrent_days == 1
    assert rules["expire-noncurrent-versions"].noncurrent_version_expiration.noncurrent_days == 91
    assert rules["expire-noncurrent-versions"].noncurrent_version_expiration.newer_noncurrent_versions == 9


async def test_existing_bucket_quota_change_preserves_immutable_name_and_iam_identity():
    provider = naming_provider()
    legacy = provider.name("bucket", "kopra", "")
    bucket = storage.Bucket(
        metadata=ResourceMetadata(
            id="bucket-same",
            parent_id="project-test",
            name=legacy,
            resource_version=7,
            labels={"fs2-storage-owner": legacy},
        ),
        spec=storage.BucketSpec(max_size_bytes=5_000_000_000),
    )
    provider.groups = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="group-same")))
    provider._operation = AsyncMock(return_value="bucket-same")
    provider.buckets = SimpleNamespace(get=AsyncMock(return_value=bucket), update=Mock())
    result = await provider.ensure_bucket(
        "kopra",
        "",
        6_000_000_000,
        existing={"bucket_id": "bucket-same", "group_id": "group-same"},
    )
    request = provider.buckets.update.call_args.args[0]
    assert request.metadata.id == "bucket-same"
    assert request.metadata.resource_version == 7
    assert request.metadata.labels["fs2-storage-owner"] == legacy
    assert request.metadata.name == legacy
    assert request.spec.max_size_bytes == 6_000_000_000
    assert len(request.spec.bucket_policy.rules) == 1
    assert request.spec.bucket_policy.rules[0].group_id == "group-same"
    assert list(request.spec.bucket_policy.rules[0].paths) == ["*"]
    assert list(request.spec.bucket_policy.rules[0].roles) == ["storage.object-editor"]
    assert all(
        rule.status == storage.LifecycleRule__Status.DISABLED for rule in request.spec.lifecycle_configuration.rules
    )
    assert result["bucket_id"] == "bucket-same"
    assert result["group_id"] == "group-same"
    # A crash between cloud quota update and DB update adopts the same identity.
    provider.buckets.update.reset_mock()
    await provider.ensure_bucket(
        "kopra",
        "",
        6_000_000_000,
        existing={"bucket_id": "bucket-same", "group_id": "group-same"},
    )
    provider.buckets.update.assert_not_called()


async def test_membership_has_no_name_and_explicit_key_is_recovered():
    provider = object.__new__(NebiusUserStorage)
    provider.project_id = "project-test"
    provider.prefix = "fs2-data"
    provider.key_ttl_days = 90
    provider.accounts = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="sa-alice")))
    provider._operation = AsyncMock(return_value="membership-alice")
    provider.memberships = SimpleNamespace(
        list_members=AsyncMock(return_value=SimpleNamespace(memberships=[], next_page_token="")),
        create=Mock(),
        delete=Mock(),
    )
    name = provider.name("user", "tenant-a", "alice")
    provider.keys = SimpleNamespace(
        list_by_account=AsyncMock(
            return_value=SimpleNamespace(
                items=[
                    SimpleNamespace(
                        metadata=SimpleNamespace(
                            name=name,
                            id="key-alice",
                            labels={"fs2-storage-owner": name},
                        ),
                        spec=SimpleNamespace(expires_at=datetime.now(UTC) + timedelta(days=90)),
                    )
                ],
                next_page_token="",
            )
        ),
        get=AsyncMock(
            side_effect=[
                SimpleNamespace(status=SimpleNamespace(state=SimpleNamespace(name="ACTIVE"))),
                SimpleNamespace(status=SimpleNamespace(state=SimpleNamespace(name="ACTIVE"))),
                SimpleNamespace(status=SimpleNamespace(state=SimpleNamespace(name="INACTIVE"))),
            ]
        ),
        get_secret=AsyncMock(return_value=SimpleNamespace(aws_access_key_id="public-id", secret="test-secret")),
        activate=Mock(),
        deactivate=Mock(),
    )
    result = await provider.ensure_credentials("tenant-a", "alice", "group-a")
    request = provider.memberships.create.call_args.args[0]
    assert request.metadata.parent_id == "group-a"
    assert not request.metadata.name
    assert request.spec.member_id == "sa-alice"
    assert result["secret_access_key"] == "test-secret"
    assert result["provider_state"] == "ACTIVE"
    assert provider.keys.get_secret.call_args.args[0].id == "key-alice"


async def test_unexpected_membership_fails_closed_without_create_or_delete():
    provider = object.__new__(NebiusUserStorage)
    expected = SimpleNamespace(
        metadata=SimpleNamespace(id="membership-expected"),
        spec=SimpleNamespace(member_id="sa-alice"),
    )
    unexpected = SimpleNamespace(
        metadata=SimpleNamespace(id="membership-unexpected"),
        spec=SimpleNamespace(member_id="sa-mallory"),
    )
    provider._operation = AsyncMock(return_value="membership-operation")
    provider.memberships = SimpleNamespace(
        list_members=AsyncMock(
            return_value=SimpleNamespace(
                memberships=[expected, unexpected], next_page_token=""
            )
        ),
        create=Mock(),
        delete=Mock(),
    )

    with pytest.raises(RuntimeError, match="unexpected memberships"):
        await provider.ensure_identity_access("group-a", "sa-alice")
    provider.memberships.create.assert_not_called()
    provider.memberships.delete.assert_not_called()


async def test_expired_existing_key_is_replaced_inactive_and_retained_as_predecessor():
    provider = object.__new__(NebiusUserStorage)
    provider.project_id = "project-test"
    provider.prefix = "fs2-data"
    provider.accounts = SimpleNamespace()
    provider._named = AsyncMock(return_value=SimpleNamespace(metadata=SimpleNamespace(id="sa-alice")))
    provider.ensure_identity_access = AsyncMock()
    name = provider.name("user", "tenant-a", "alice")
    expired = SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            id="key-expired",
            labels={"fs2-storage-owner": name},
        ),
        spec=SimpleNamespace(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
    )
    provider.keys = SimpleNamespace(
        list_by_account=AsyncMock(return_value=SimpleNamespace(items=[expired], next_page_token=""))
    )
    replacement = {
        "service_account_id": "sa-alice",
        "access_key_resource_id": "key-new",
        "access_key_id": "public-new",
        "secret_access_key": "secret-new",
        "expires_at": datetime.now(UTC) + timedelta(days=90),
        "provider_state": "INACTIVE",
    }
    provider.prepare_rotation = AsyncMock(return_value=replacement)
    provider.key_state = AsyncMock(return_value="EXPIRED")

    result = await provider.ensure_credentials("tenant-a", "alice", "group-a")

    assert result["access_key_resource_id"] == "key-new"
    assert result["provider_state"] == "INACTIVE"
    assert result["previous_access_key_resource_id"] == "key-expired"
    previous = provider.prepare_rotation.call_args.args[3]
    assert previous == {
        "service_account_id": "sa-alice",
        "access_key_resource_id": "key-expired",
    }


async def test_inventory_quarantines_every_extra_key_before_allowing_current():
    provider = naming_provider()
    name = provider.name("user", "tenant-a", "alice")
    current = SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            id="key-current",
            labels={"fs2-storage-owner": name},
        )
    )
    extra = SimpleNamespace(
        metadata=SimpleNamespace(name="unrelated-active-key", id="key-extra", labels={})
    )
    provider._account_keys = AsyncMock(return_value=[current, extra])
    provider._force_key_inactive = AsyncMock(return_value="INACTIVE")
    provider.key_state = AsyncMock(return_value="ACTIVE")

    verified = await provider.reconcile_key_inventory(
        "tenant-a",
        "alice",
        {
            "service_account_id": "sa-alice",
            "access_key_resource_id": "key-current",
            "replacement_access_key_resource_id": None,
            "previous_access_key_resource_id": None,
        },
        effective_enabled=True,
    )

    assert verified is True
    provider._force_key_inactive.assert_awaited_once_with("key-extra")


async def test_inventory_rejects_foreign_same_name_even_when_db_tracks_it():
    provider = naming_provider()
    name = provider.name("user", "tenant-a", "alice")
    foreign = SimpleNamespace(
        metadata=SimpleNamespace(name=name, id="key-current", labels={})
    )
    provider._account_keys = AsyncMock(return_value=[foreign])
    provider._force_key_inactive = AsyncMock(return_value="INACTIVE")

    with pytest.raises(RuntimeError, match="foreign same-name"):
        await provider.reconcile_key_inventory(
            "tenant-a",
            "alice",
            {
                "service_account_id": "sa-alice",
                "access_key_resource_id": "key-current",
                "replacement_access_key_resource_id": None,
                "previous_access_key_resource_id": None,
            },
            effective_enabled=True,
        )
    provider._force_key_inactive.assert_awaited_once_with("key-current")


async def test_off_inventory_quarantines_foreign_active_key_and_current():
    provider = naming_provider()
    name = provider.name("user", "tenant-a", "alice")
    current = SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            id="key-current",
            labels={"fs2-storage-owner": name},
        )
    )
    foreign = SimpleNamespace(
        metadata=SimpleNamespace(name="manual-key", id="key-manual", labels={})
    )
    provider._account_keys = AsyncMock(return_value=[current, foreign])
    provider._force_key_inactive = AsyncMock(return_value="INACTIVE")

    assert await provider.reconcile_key_inventory(
        "tenant-a",
        "alice",
        {
            "service_account_id": "sa-alice",
            "access_key_resource_id": "key-current",
            "replacement_access_key_resource_id": None,
            "previous_access_key_resource_id": None,
        },
        effective_enabled=False,
    )
    assert {call.args[0] for call in provider._force_key_inactive.await_args_list} == {
        "key-current",
        "key-manual",
    }


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

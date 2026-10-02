"""A customer view across existing users, usage, storage and workbench bindings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from .access_models import OperatorPrincipal, OperatorRole
from .admin_models import AdminContext
from .store import ConflictError, NotFoundError
from .workbench_models import CustomerProfile, WorkbenchBinding, WorkbenchCommand, WorkbenchRegistration

FRESHNESS_SECONDS = 600


def observed_state(value: dict[str, Any] | None) -> str:
    if value is None:
        return "unavailable"
    return "available" if (datetime.now(UTC) - value["observed_at"]).total_seconds() <= FRESHNESS_SECONDS else "stale"


class WorkbenchService:
    def __init__(self, repository: Any, users: Any) -> None:
        self.repository = repository
        self.users = users
        self.inventory: Any = None
        self.executor_enabled = False
        self.releases: dict[str, str] = {}
        self.protected_endpoints: set[str] = set()

    async def customers(self, identity: OperatorPrincipal, context: AdminContext, tenant_id: str | None = None) -> Any:
        tenant = await self.users.access.authorize(
            identity, OperatorRole.VIEWER, action="customer.list", tenant_id=tenant_id
        )
        users = await self.users.list(identity, context, tenant_id=tenant, limit=1000)
        profiles = await self.repository.profiles(tenant)
        bindings = await self.repository.bindings(tenant)
        usage = await self.repository.model_usage(tenant, context)
        storage = await self.repository.storage(tenant)
        observations = {item["resource_id"]: item for item in await self.repository.observations()}
        tenants = sorted(
            {user.tenant_id for user in users.items}
            | set(profiles)
            | {value.tenant_id for value in bindings}
            | {item["tenant_id"] for item in storage}
        )
        results = []
        for owner in tenants:
            members = [user for user in users.items if user.tenant_id == owner]
            buckets = []
            for item in storage:
                if item["tenant_id"] == owner:
                    observation = observations.get(item["bucket_id"])
                    buckets.append(
                        {
                            **item,
                            "observation": observation,
                            "observation_state": observed_state(observation),
                            "mode": "user" if item["owner_key"] else "tenant",
                        }
                    )
            clients = []
            for binding in bindings:
                if binding.tenant_id == owner:
                    observation = observations.get(binding.endpoint_id)
                    clients.append(
                        {
                            **binding.model_dump(mode="json"),
                            "observation": observation,
                            "observation_state": observed_state(observation),
                            "operations": [
                                value.model_dump(mode="json") for value in await self.repository.operations(binding.id)
                            ],
                        }
                    )
            model_usage = [item for item in usage if item["tenant_id"] == owner]
            results.append(
                {
                    "tenant_id": owner,
                    "profile": profiles.get(owner, {"display_name": owner, "purpose": "legacy", "archived": False}),
                    "users": [value.model_dump(mode="json") for value in members],
                    "model_usage": model_usage,
                    "requests": sum(item["requests"] for item in model_usage),
                    "active_keys": sum(value.active_key_count for value in members),
                    "last_request_at": max((item["last_request_at"] for item in model_usage), default=None),
                    "buckets": buckets,
                    "workbenches": clients,
                }
            )
        return {
            "items": results,
            "truncated": users.truncated,
            "inventory_available": self.inventory is not None,
            "inventory_error": self.inventory.last_error if self.inventory else "not_configured",
            "lifecycle_executor_available": self.executor_enabled,
            "releases": self.releases,
            "attribution": (
                "Accepted model operations in the selected window, not polls. "
                "Shared keys identify the inference owner, not individual LibreChat logins."
            ),
        }

    async def inventory_view(self, identity: OperatorPrincipal) -> Any:
        await self.users.access.authorize_global(identity, OperatorRole.VIEWER, action="workbench.inventory")
        bindings = {value.endpoint_id: value for value in await self.repository.bindings()}
        storage = {item["bucket_id"]: item for item in await self.repository.storage(None)}
        observations = await self.repository.observations()
        mounts: dict[str, list[str]] = {}
        for item in observations:
            if item["kind"] == "endpoint":
                for name in item.get("buckets", []):
                    mounts.setdefault(name, []).append(item["resource_id"])
        return {
            "items": [
                {
                    **item,
                    "observation_state": observed_state(item),
                    "workbench_id": str(bindings[item["resource_id"]].id) if item["resource_id"] in bindings else None,
                    "tenant_id": (
                        bindings[item["resource_id"]].tenant_id
                        if item["resource_id"] in bindings
                        else storage.get(item["resource_id"], {}).get("tenant_id")
                    ),
                    "mounted_by": mounts.get(item["name"], []) if item["kind"] == "bucket" else [],
                    "protected": bindings[item["resource_id"]].protected if item["resource_id"] in bindings else False,
                }
                for item in observations
            ],
            "last_error": self.inventory.last_error if self.inventory else "not_configured",
        }

    async def save_profile(self, identity: OperatorPrincipal, tenant: str, value: CustomerProfile) -> CustomerProfile:
        await self.users.access.authorize(identity, OperatorRole.OPERATOR, action="customer.update", tenant_id=tenant)
        known = await self.users.repository.list(tenant)
        if not known and not await self.repository.storage(tenant):
            raise NotFoundError("customer tenant not found; create an inference user first")
        await self.repository.save_profile(tenant, value)
        return value

    async def register(self, identity: OperatorPrincipal, value: WorkbenchRegistration) -> WorkbenchBinding:
        await self.users.access.authorize(
            identity, OperatorRole.OPERATOR, action="workbench.register", tenant_id=value.tenant_id
        )
        known = {user.principal_id for user in await self.users.repository.list(value.tenant_id)}
        if not set(value.principal_ids) <= known:
            raise ValueError("all workbench members must be existing inference owners in this tenant")
        observed = next(
            (
                item
                for item in await self.repository.observations()
                if item["resource_id"] == value.endpoint_id and item["kind"] == "endpoint"
            ),
            None,
        )
        if observed_state(observed) != "available" or observed is None:
            raise ConflictError("a fresh cloud observation is required before adopting an endpoint")
        if observed["project_id"] != value.project_id or not observed["is_workbench"]:
            raise ValueError("endpoint project or LibreChat image does not match")
        buckets = await self.repository.storage(value.tenant_id)
        allowed = {
            item["bucket_name"] for item in buckets if not item["owner_key"] or item["owner_key"] in value.principal_ids
        }
        mounted = set(observed["buckets"])
        if len(mounted) != 1 or not mounted <= allowed:
            raise ConflictError("workbench must mount this tenant's existing workspace; resolve legacy bindings first")
        now = datetime.now(UTC)
        if value.endpoint_id in self.protected_endpoints:
            value = value.model_copy(update={"protected": True, "protection_reason": "Owner hold: do not modify"})
        return await self.repository.register(
            WorkbenchBinding(
                **value.model_dump(),
                id=uuid4(),
                bucket_name=next(iter(mounted)),
                state_filesystem_id=observed.get("state_filesystem_id") if observed.get("persistent_state") else None,
                created_at=now,
                updated_at=now,
            )
        )

    async def command(self, identity: OperatorPrincipal, workbench_id: UUID, value: WorkbenchCommand) -> Any:
        binding = await self.repository.binding(workbench_id)
        await self.users.access.authorize(
            identity, OperatorRole.OPERATOR, action="workbench.command", tenant_id=binding.tenant_id
        )
        if binding.protected:
            raise ConflictError("workbench is protected: no lifecycle changes permitted")
        if binding.management != "managed":
            raise ConflictError("customer-owned instance: remote lifecycle management has not been delegated")
        if not self.executor_enabled:
            raise ConflictError("lifecycle executor is not configured; no operation was queued")
        if value.kind != "upgrade":
            raise ConflictError("only state-preserving upgrades are available; no operation was queued")
        if not binding.state_filesystem_id:
            raise ConflictError("legacy local state: export and migrate this instance before enabling upgrades")
        if value.target_release not in self.releases:
            raise ValueError("select a qualified workbench release")
        if not value.confirm_interruption:
            raise ValueError("confirm the brief LibreChat interruption; finish active chat turns before upgrading")
        return await self.repository.command(binding, value, identity.subject)

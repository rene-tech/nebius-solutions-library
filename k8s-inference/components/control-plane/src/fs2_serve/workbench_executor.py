"""Durable Serverless replacement executor for persistent-state LibreChat clients.

Legacy clients are inventory-only until explicitly migrated. The source cloud
spec is read at execution time, copied without exposing credentials, and retained
as a stopped resource. Deterministic successor names make interrupted work
reconcilable without duplicate endpoints. No buckets, keys or users are deleted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextlib import suppress
from typing import Any

import httpx

from .workbench_inventory import endpoint_observation
from .workbench_repository import document

LOG = logging.getLogger(__name__)


class NebiusWorkbenchRuntime:
    def __init__(self, sdk: Any) -> None:
        from nebius.api.nebius.ai import v1 as ai
        from nebius.api.nebius.common.v1 import ResourceMetadata

        self.ai = ai
        self.metadata = ResourceMetadata
        self.client = ai.EndpointServiceClient(sdk)

    async def get(self, endpoint_id: str) -> Any:
        return await self.client.get(self.ai.GetEndpointRequest(id=endpoint_id))

    async def stop(self, endpoint_id: str) -> None:
        value = await self.get(endpoint_id)
        state = endpoint_observation(value).state
        if state == "STOPPED":
            return
        if state == "STOPPING":
            for _ in range(120):
                await asyncio.sleep(2)
                if endpoint_observation(await self.get(endpoint_id)).state == "STOPPED":
                    return
            raise TimeoutError("endpoint_stop_timeout")
        operation = await self.client.stop(self.ai.StopEndpointRequest(id=endpoint_id))
        await operation.wait()
        if not operation.successful():
            raise RuntimeError("endpoint_stop_failed")
        if endpoint_observation(await self.get(endpoint_id)).state != "STOPPED":
            raise RuntimeError("endpoint_not_stopped")

    async def successor(self, source: Any, name: str, image: str, operation_id: str, *, rollback: bool = False) -> str:
        from grpc import StatusCode
        from nebius.aio.service_error import RequestError

        try:
            existing = await self.client.get_by_name(
                self.ai.GetEndpointByNameRequest(parent_id=str(source.metadata.parent_id), name=name)
            )
        except RequestError as exc:
            if exc.status.code != StatusCode.NOT_FOUND:
                raise
        else:
            if str(existing.spec.image) != image or endpoint_observation(existing).state_filesystem_id != (
                endpoint_observation(source).state_filesystem_id
            ):
                raise RuntimeError("successor_identity_conflict")
            return str(existing.metadata.id)
        spec = copy.deepcopy(source.spec)
        spec.image = image
        env = [
            copy.deepcopy(item)
            for item in source.spec.environment_variables
            if str(item.name)
            not in {"SCIENTIFIC_STATE_SNAPSHOT", "SCIENTIFIC_STATE_RESTORE", "SCIENTIFIC_STATE_RESTORE_IF_PRESENT"}
        ]
        # Use the existing protobuf field type, not guessed JSON. Secret refs and
        # unrelated environment entries remain byte-for-byte equivalent.
        prototype = copy.deepcopy(
            next(
                item
                for item in source.spec.environment_variables
                if str(item.name) == "SCIENTIFIC_REQUIRE_PERSISTENT_STATE"
            )
        )
        prototype.name = "SCIENTIFIC_STATE_RESTORE_IF_PRESENT" if rollback else "SCIENTIFIC_STATE_SNAPSHOT"
        prototype.value = operation_id
        env.append(prototype)
        spec.environment_variables = env
        operation = await self.client.create(
            self.ai.CreateEndpointRequest(
                metadata=self.metadata(
                    parent_id=str(source.metadata.parent_id),
                    name=name,
                    labels={"managed-by": "fs2-workbenches", "workbench-operation": operation_id},
                ),
                spec=spec,
            )
        )
        await operation.wait()
        if not operation.successful():
            raise RuntimeError("successor_create_failed")
        return str(operation.resource_id)

    async def ready(self, endpoint_id: str) -> Any:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            for _ in range(180):
                observed = endpoint_observation(await self.get(endpoint_id))
                if observed.state in {"FAILED", "ERROR", "STOPPED"}:
                    raise RuntimeError("successor_unhealthy")
                if observed.state == "RUNNING" and observed.url:
                    try:
                        response = await client.get(observed.url.rstrip("/") + "/health")
                        if response.status_code == 200 and "<!doctype" not in response.text[:100].lower():
                            return observed
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(5)
        raise TimeoutError("successor_readiness_timeout")


class WorkbenchExecutor:
    def __init__(self, repository: Any, provider: Any, releases: dict[str, str], protected: set[str]) -> None:
        self.repository, self.provider = repository, provider
        self.releases, self.protected = releases, protected
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self.run(), name="workbench-executor")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # Provider exceptions may embed customer secrets/specifications.
                LOG.warning("workbench reconciliation failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(5)

    async def tick(self) -> None:
        async with self.repository.pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT * FROM fs2_workbench_operations WHERE state IN ('queued','running') "
                "ORDER BY created_at LIMIT 10"
            )
            for row in rows:
                identity = str(row["id"])
                locked = await connection.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,44))", identity)
                if not locked:
                    continue
                try:
                    current = await connection.fetchrow("SELECT * FROM fs2_workbench_operations WHERE id=$1", row["id"])
                    if current["state"] in {"queued", "running"}:
                        await self.execute(connection, current)
                finally:
                    await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1,44))", identity)

    async def execute(self, connection: Any, row: Any) -> None:
        identity = str(row["id"])
        binding = await self.repository.binding(row["workbench_id"])
        command, progress = document(row["specification"]), document(row["progress"])

        async def record(state: str = "running", error: str | None = None) -> None:
            await connection.execute(
                """UPDATE fs2_workbench_operations SET state=$2,progress=$3::jsonb,error_code=$4,updated_at=now()
                WHERE id=$1""",
                row["id"],
                state,
                json.dumps(progress),
                error,
            )

        try:
            if binding.protected or binding.endpoint_id in self.protected or binding.management != "managed":
                raise ValueError("protected_or_unmanaged")
            if row["kind"] != "upgrade" or not binding.state_filesystem_id:
                raise ValueError("legacy_state_requires_migration")
            image = self.releases[command["target_release"]]
            source_id = progress.get("source_endpoint", binding.endpoint_id)
            source = await self.provider.get(source_id)
            observed = endpoint_observation(source)
            if observed.state_filesystem_id != binding.state_filesystem_id or not observed.persistent_state:
                raise ValueError("persistent_state_binding_changed")
            progress.update(source_endpoint=source_id, stage="stopping_predecessor")
            await record()
            # stop is safe only for the validated independent state filesystem.
            await self.provider.stop(source_id)
            progress["source_stopped"] = True
            progress["stage"] = "creating_successor"
            await record()
            new_id = await self.provider.successor(source, f"fs2-wb-{identity}", image, identity)
            progress.update(successor_endpoint=new_id, stage="waiting_for_successor")
            await record()
            ready = await self.provider.ready(new_id)
            async with connection.transaction():
                await connection.execute(
                    """UPDATE fs2_workbenches SET endpoint_id=$2,desired_release=$3,revision=revision+1,
                    updated_at=now() WHERE id=$1 AND endpoint_id=$4""",
                    binding.id,
                    new_id,
                    command["target_release"],
                    source_id,
                )
                progress.update(stage="complete", url=ready.url, snapshot_id=identity, predecessor_retained=True)
                await record("succeeded")
        except asyncio.CancelledError:
            raise  # Durable progress is reconciled by the next process.
        except Exception as exc:
            if progress.get("source_stopped"):
                try:
                    if progress.get("successor_endpoint"):
                        await self.provider.stop(progress["successor_endpoint"])
                    source = await self.provider.get(progress["source_endpoint"])
                    recovery = await self.provider.successor(
                        source,
                        f"fs2-wb-rollback-{identity}",
                        str(source.spec.image),
                        identity,
                        rollback=True,
                    )
                    ready = await self.provider.ready(recovery)
                    await connection.execute(
                        "UPDATE fs2_workbenches SET endpoint_id=$2,revision=revision+1,updated_at=now() WHERE id=$1",
                        binding.id,
                        recovery,
                    )
                    progress.update(stage="rolled_back", recovery_endpoint=recovery, url=ready.url)
                except Exception as recovery_error:
                    progress.update(stage="recovery_required", recovery_error=type(recovery_error).__name__)
            await record("failed", type(exc).__name__)

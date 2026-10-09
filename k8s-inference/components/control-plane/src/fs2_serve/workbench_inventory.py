"""Read-only Nebius discovery. Project credentials never become API responses."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from typing import Any

from .workbench_models import BucketObservation, EndpointObservation

LOG = logging.getLogger(__name__)


def endpoint_observation(value: Any) -> EndpointObservation:
    spec, status, metadata = value.spec, value.status, value.metadata
    urls = [str(url) for url in status.public_endpoints if str(url).startswith("https://")]
    image = str(spec.image)
    buckets = sorted({str(volume.source)[5:] for volume in spec.volumes if str(volume.source).startswith("s3://")})
    state = status.state.name if hasattr(status.state, "name") else str(status.state)
    state_volumes = [
        str(v.source)
        for v in spec.volumes
        if str(getattr(v, "container_path", "")) == "/data" and str(v.source).startswith("computefilesystem-")
    ]
    durable = any(
        str(getattr(v, "name", "")) == "SCIENTIFIC_REQUIRE_PERSISTENT_STATE" and str(getattr(v, "value", "")) == "true"
        for v in getattr(spec, "environment_variables", [])
        if not isinstance(v, str)
    )
    return EndpointObservation(
        resource_id=str(metadata.id),
        project_id=str(metadata.parent_id),
        name=str(metadata.name),
        state=state,
        image=image,
        platform=str(spec.platform),
        preset=str(spec.preset),
        url=next((url for url in urls if "port3080-" in url), urls[0] if urls else None),
        buckets=buckets,
        is_workbench=bool(re.search(r"/(?:lc|hcls-librechat|scientific-ai-agent)(?:[:@]|$)", image)),
        state_filesystem_id=state_volumes[0] if len(state_volumes) == 1 else None,
        persistent_state=durable and len(state_volumes) == 1,
    )


def bucket_observation(value: Any) -> BucketObservation:
    labels = value.metadata.labels
    counters = getattr(value.status, "counters", None)
    current_bytes = None
    if counters:
        current_bytes = sum(
            int(getattr(item.counters, "simple_objects_size", 0))
            + int(getattr(item.counters, "multipart_objects_size", 0))
            for item in counters
        )
    return BucketObservation(
        resource_id=str(value.metadata.id),
        project_id=str(value.metadata.parent_id),
        name=str(value.metadata.name),
        size_bytes=current_bytes,
        quota_bytes=int(value.spec.max_size_bytes) or None,
        purpose=labels.get("purpose"),
        managed_by=labels.get("managed-by"),
    )


class NebiusWorkbenchInventory:
    def __init__(self, sdk: Any, project_id: str) -> None:
        from nebius.api.nebius.ai import v1 as ai
        from nebius.api.nebius.storage import v1 as storage

        self.ai = ai
        self.storage = storage
        self.project_id = project_id
        self.endpoints = ai.EndpointServiceClient(sdk)
        self.buckets = storage.BucketServiceClient(sdk)

    async def snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        # Always exhaust both paginated inventories before replacing a snapshot.
        for client, request_type, convert in (
            (self.endpoints, self.ai.ListEndpointsRequest, endpoint_observation),
            (self.buckets, self.storage.ListBucketsRequest, bucket_observation),
        ):
            token = ""
            seen: set[str] = set()
            while True:
                response = await client.list(request_type(parent_id=self.project_id, page_token=token, page_size=100))
                result.extend(convert(value).model_dump(mode="json") for value in response.items)
                token = str(response.next_page_token)
                if not token:
                    break
                if token in seen:
                    raise ValueError("cloud inventory pagination repeated a token")
                seen.add(token)
        return result


class WorkbenchInventoryWorker:
    def __init__(self, repository: Any, provider: Any, interval: float = 120) -> None:
        self.repository = repository
        self.provider = provider
        self.interval = interval
        self.task: asyncio.Task[None] | None = None
        self.lock = asyncio.Lock()
        self.last_error: str | None = None

    async def refresh(self) -> dict[str, Any]:
        async with self.lock, asyncio.timeout(60):
            try:
                values = await self.provider.snapshot()
                await self.repository.observe(self.provider.project_id, values)
                self.last_error = None
                return {"resources": len(values), "project_id": self.provider.project_id}
            except Exception as exc:
                self.last_error = type(exc).__name__
                raise

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="workbench-inventory")

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                LOG.warning("workbench inventory unavailable error_type=%s", type(exc).__name__)
            await asyncio.sleep(self.interval)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None

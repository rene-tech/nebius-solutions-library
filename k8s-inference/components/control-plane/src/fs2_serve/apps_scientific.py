"""Independent scientific app identities over unchanged qualified runtimes.

Source profiles, plan adapters, artifact receipts and startup bindings remain
canonical. Only the outer public/dispatch identity changes. In particular this
does not clone or requalify model weights, execute a second batch, or rewrite a
GPU snapshot. Frozen stage bindings retain the exact source runtime commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from .apps_models import AppRecord
from .apps_repository import AppsRepository
from .scientific_batch.execution import FileScientificManifestRenderer
from .scientific_batch.models import AdapterExecutionPlan
from .scientific_batch.profile_catalog import ScientificProfileCatalog, ScientificWorkloadProfile
from .scientific_batch.scheduling import SchedulingContractResolver


LOGGER = logging.getLogger(__name__)
SCIENTIFIC_APPS_REFRESH_TTL_SECONDS = 5.0
SCIENTIFIC_APPS_REFRESH_FAILURE_RETRY_SECONDS = 1.0


class ScientificAppsRefreshError(RuntimeError):
    """Fail closed when durable app policy cannot refresh safely."""


class ScientificAppsInventory:
    def __init__(
        self,
        repository: AppsRepository,
        *,
        refresh_ttl_seconds: float = SCIENTIFIC_APPS_REFRESH_TTL_SECONDS,
        refresh_failure_retry_seconds: float = SCIENTIFIC_APPS_REFRESH_FAILURE_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(refresh_ttl_seconds) or refresh_ttl_seconds <= 0:
            raise ValueError("scientific Apps refresh TTL must be finite and positive")
        if (
            not math.isfinite(refresh_failure_retry_seconds)
            or refresh_failure_retry_seconds <= 0
            or refresh_failure_retry_seconds > refresh_ttl_seconds
        ):
            raise ValueError("scientific Apps refresh retry must be positive and no greater than the TTL")
        self.repository = repository
        self.records: dict[str, AppRecord] = {}
        self.discoverable_records: dict[str, AppRecord] = {}
        self.refresh_ttl_seconds = refresh_ttl_seconds
        self.refresh_failure_retry_seconds = refresh_failure_retry_seconds
        self._clock = clock
        self._refresh_lock = asyncio.Lock()
        self._refresh_generation = 0
        self._refreshed_at: float | None = None
        self._retry_after = 0.0

    def _fresh(self, now: float) -> bool:
        return self._refreshed_at is not None and now - self._refreshed_at < self.refresh_ttl_seconds

    async def refresh(self, *, force: bool = False) -> None:
        """Refresh once per bounded TTL and collapse concurrent repository reads.

        A failed refresh invalidates freshness while retaining the last complete
        snapshot for diagnostics. Callers never proceed through ``refresh``
        with that stale snapshot: they receive a generic availability error
        until a complete two-query refresh succeeds.
        """

        requested_generation = self._refresh_generation
        now = self._clock()
        if not force and self._fresh(now):
            return
        async with self._refresh_lock:
            now = self._clock()
            if not force and self._refresh_generation != requested_generation:
                return
            if not force and self._fresh(now):
                return
            if now < self._retry_after:
                raise ScientificAppsRefreshError("scientific Apps inventory refresh unavailable")
            try:
                all_records = await self.repository.list_records()
                discoverable = await self.repository.list_discoverable_records()
            except Exception:
                self._refreshed_at = None
                self._retry_after = self._clock() + self.refresh_failure_retry_seconds
                LOGGER.exception("scientific Apps inventory refresh failed")
                raise ScientificAppsRefreshError("scientific Apps inventory refresh unavailable") from None
            records = {
                item.public_model_id: item
                for item in all_records
                if item.execution_mode == "scientific" and item.public_model_id != item.model_ref
            }
            discoverable_records = {
                item.public_model_id: item
                for item in discoverable
                if item.execution_mode == "scientific" and item.public_model_id != item.model_ref
            }
            self.records = records
            self.discoverable_records = discoverable_records
            self._refreshed_at = self._clock()
            self._retry_after = 0.0
            self._refresh_generation += 1

    def source(self, model_id: str) -> str:
        record = self.records.get(model_id)
        return record.model_ref if record else model_id


@dataclass(frozen=True, slots=True)
class AppScientificProfile(ScientificWorkloadProfile):
    app: AppRecord

    @property
    def model_id(self) -> str:
        return self.app.public_model_id

    @property
    def display_name(self) -> str:
        return self.app.display_name

    @property
    def mcp_tool_name(self) -> str:
        return f"app_{self.app.app_id.hex}"


class AppScientificProfiles(ScientificProfileCatalog):
    def __init__(self, source: ScientificProfileCatalog, inventory: ScientificAppsInventory) -> None:
        self.source = source
        self.inventory = inventory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def get(self, model_id: str, *, runnable: bool = True) -> ScientificWorkloadProfile:
        profile = self.source.get(self.inventory.source(model_id), runnable=runnable)
        app = self.inventory.records.get(model_id)
        if app is not None and runnable and model_id not in self.inventory.discoverable_records:
            raise RuntimeError("scientific App is paused and unavailable for submission")
        return AppScientificProfile(value=profile.value, app=app) if app else profile

    def list(self, *, runnable_only: bool = True) -> tuple[ScientificWorkloadProfile, ...]:
        profiles = self.source.list(runnable_only=runnable_only)
        sources = {profile.model_id for profile in profiles}
        return (
            *profiles,
            *(
                self.get(model_id, runnable=runnable_only)
                for model_id, app in sorted(self.inventory.discoverable_records.items())
                if app.model_ref in sources
            ),
        )


class AppScientificExecution(FileScientificManifestRenderer):
    """Translate only app identity at the source adapter's external boundary."""

    def __init__(self, source: FileScientificManifestRenderer, inventory: ScientificAppsInventory) -> None:
        self.source = source
        self.inventory = inventory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    @staticmethod
    def _profile(profile: ScientificWorkloadProfile) -> ScientificWorkloadProfile:
        return ScientificWorkloadProfile(profile.value) if isinstance(profile, AppScientificProfile) else profile

    def _plan(self, plan: AdapterExecutionPlan) -> AdapterExecutionPlan:
        model_id = self.inventory.source(plan.model_id)
        return replace(plan, model_id=model_id) if model_id != plan.model_id else plan

    @staticmethod
    def _public(plan: AdapterExecutionPlan, profile: ScientificWorkloadProfile) -> AdapterExecutionPlan:
        return replace(plan, model_id=profile.model_id) if plan.model_id != profile.model_id else plan

    def variant_id(self, model_id: str) -> str:
        return self.source.variant_id(self.inventory.source(model_id))

    def workload_namespace(self, model_id: str) -> str:
        return self.source.workload_namespace(self.inventory.source(model_id))

    def collector_id(self, model_id: str, stage_id: str) -> str:
        return self.source.collector_id(self.inventory.source(model_id), stage_id)

    def startup_policy_options(self, model_id: str) -> dict[str, list[str]]:
        return self.source.startup_policy_options(self.inventory.source(model_id))

    def validate_startup_policy_overrides(self, *, model_id: str, overrides: Any) -> Any:
        return self.source.validate_startup_policy_overrides(
            model_id=self.inventory.source(model_id),
            overrides=overrides,
        )

    def access_context(self, profile: ScientificWorkloadProfile, *, tenant_id: str) -> Any:
        return self.source.access_context(self._profile(profile), tenant_id=tenant_id)

    def plan(self, profile: ScientificWorkloadProfile, request: Any, **kwargs: Any) -> AdapterExecutionPlan:
        return self._public(self.source.plan(self._profile(profile), request, **kwargs), profile)

    def verify_runtime_artifacts(
        self, profile: ScientificWorkloadProfile, plan: AdapterExecutionPlan, access: Any
    ) -> Any:
        return self.source.verify_runtime_artifacts(self._profile(profile), self._plan(plan), access)

    def bind_runtime_artifacts(
        self,
        profile: ScientificWorkloadProfile,
        plan: AdapterExecutionPlan,
        access: Any,
        localizations: Any,
    ) -> AdapterExecutionPlan:
        bound = self.source.bind_runtime_artifacts(self._profile(profile), self._plan(plan), access, localizations)
        return self._public(bound, profile)

    def bind_startup_policies(
        self,
        profile: ScientificWorkloadProfile,
        plan: AdapterExecutionPlan,
        overrides: Any,
    ) -> AdapterExecutionPlan:
        bound = self.source.bind_startup_policies(self._profile(profile), self._plan(plan), overrides)
        return self._public(bound, profile)

    def render(self, resource: Any) -> Any:
        source_id = self.inventory.source(resource.model_id)
        # Capabilities and collector receipts bind the public app. The runtime
        # marker instead describes the immutable, qualified image's model.
        if source_id != resource.model_id:
            # Run the canonical renderer's source-specific validation first
            # (notably AF3 whole-reference-root validation). Rendering is pure:
            # this creates no workload and its source-scoped credentials are
            # discarded. Only the second app-scoped manifest is submitted.
            self.source.render(replace(resource, model_id=source_id))
        manifest = self.source.render(resource)
        if source_id == resource.model_id:
            return manifest
        manifest = deepcopy(manifest)
        templates = (
            [manifest["spec"]["template"]]
            if manifest["kind"] == "Job"
            else [job["template"]["spec"]["template"] for job in manifest["spec"]["replicatedJobs"]]
        )
        for template in templates:
            pod = template["spec"]
            for container in [*pod.get("initContainers", []), *pod["containers"]]:
                for environment in container.get("env", []):
                    if environment["name"] == "FS2_RUNTIME_ARTIFACTS_JSON":
                        marker = json.loads(environment["value"])
                        marker["model_id"] = source_id
                        environment["value"] = json.dumps(marker, sort_keys=True, separators=(",", ":"))
        return manifest


class AppScientificScheduling(SchedulingContractResolver):
    def __init__(self, source: SchedulingContractResolver, inventory: ScientificAppsInventory) -> None:
        self.source = source
        self.inventory = inventory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    def freeze(self, *, model_id: str, **kwargs: Any) -> Any:
        # Inherit the source's GPU eligibility and exact namespace/LocalQueue
        # placement. The independent app's dispatch policy is a separate row,
        # not a Kueue quota increase or a second physical GPU inventory.
        frozen = self.source.freeze(model_id=self.inventory.source(model_id), **kwargs)
        return replace(frozen, model_lane=model_id) if frozen.model_lane != model_id else frozen


class AppScientificCluster:
    """Refresh durable app mappings before an independent worker renders work."""

    def __init__(self, source: Any, inventory: ScientificAppsInventory) -> None:
        self.source = source
        self.inventory = inventory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)

    async def apply(self, resource: Any, *, controller_fence: int) -> Any:
        if resource.model_id.startswith("app-"):
            # Never render a workload from a potentially stale app-to-runtime
            # mapping, even when the request-path cache is still within TTL.
            await self.inventory.refresh(force=True)
        return await self.source.apply(resource, controller_fence=controller_fence)


class AppScientificModels:
    """Admin readiness keeps source qualification and projects app identity."""

    def __init__(self, source: Any, inventory: ScientificAppsInventory) -> None:
        self.source = source
        self.inventory = inventory

    async def list_models(self, *, tenant_id: str | None = None) -> Any:
        # Admin sessions do not pass through PAT authentication. A different
        # replica can therefore see a new durable policy before its in-memory
        # source mapping; refresh before catalog and startup-policy projection.
        await self.inventory.refresh()
        snapshot = await self.source.list_models(tenant_id=tenant_id)
        by_source = {model.model_id: model for model in snapshot.data.items}
        aliases = [
            by_source[app.model_ref].model_copy(
                update={
                    "model_id": app.public_model_id,
                    "display_name": app.display_name,
                }
            )
            for app in self.inventory.records.values()
            if app.model_ref in by_source and app.public_model_id not in by_source
        ]
        return replace(snapshot, data=snapshot.data.model_copy(update={"items": [*snapshot.data.items, *aliases]}))

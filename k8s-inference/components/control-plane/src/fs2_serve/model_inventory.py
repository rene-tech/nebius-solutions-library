"""Unified operator inventory without silently dropping undeployed models.

The inventory joins existing serving and scientific projections. It does not
grant inference access, synthesize readiness, or replace their control APIs.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from .admin_models import AdminModelSummary
from .models import StrictModel
from .registry import OperationalModel
from .scientific_admin_models import ScientificModelReadiness


class ModelInventoryItem(StrictModel):
    model_id: str
    display_name: str
    family: str
    availability: str
    reason: str
    configured: bool
    serving_enabled: bool
    serving_state: str | None = None
    batch_readiness: str | None = None
    ready_replicas: int | None = None
    desired_replicas: int | None = None
    runtime_image_digest: str | None = None
    gpu_snapshot: Literal["verified", "candidate", "unsupported", "unavailable", "not-reported"] = "not-reported"
    snapshot_reason: str = "No GPU snapshot capability is published for this runtime."
    management_path: str | None = None


class ModelInventory(StrictModel):
    items: list[ModelInventoryItem] = Field(max_length=512)
    total: int
    configured: int
    not_deployed: int
    scientific_projection_available: bool


def build_model_inventory(
    catalog: Sequence[OperationalModel],
    serving: Sequence[AdminModelSummary],
    scientific: Sequence[ScientificModelReadiness],
    *,
    scientific_projection_available: bool,
) -> ModelInventory:
    """Preserve every known ID, merging shared IDs rather than double-counting."""

    records = {model.id: model for model in catalog}
    deployments = {model.identity.id: model for model in serving}
    profiles = {model.model_id: model for model in scientific}
    items: list[ModelInventoryItem] = []
    for model_id in sorted(records.keys() | deployments.keys() | profiles.keys()):
        record = records.get(model_id)
        deployed = deployments.get(model_id)
        profile = profiles.get(model_id)
        display_name = (
            profile.display_name
            if profile
            else (deployed.identity.display_name if deployed else record.gateway.display_name if record else model_id)
        )
        configured = deployed is not None or (profile is not None and profile.workload_profile == "published")
        enabled = deployed is not None and deployed.identity.enabled
        state = deployed.runtime.state.value if deployed else None
        if enabled and deployed is not None:
            availability, reason = state or "unknown", deployed.runtime.reason
        elif profile is not None and profile.readiness == "qualified":
            availability = "batch-ready"
            reason = "Qualified batch profile; GPU workers start when a request is dispatched."
        elif profile is not None and profile.workload_profile == "published":
            availability, reason = profile.readiness, profile.readiness_reason
        elif deployed is not None:
            availability, reason = "disabled", "Configured in this cluster but inference is disabled."
        else:
            availability = "not-deployed"
            reason = "Present in the catalog, but no serving deployment or published batch profile is configured."
            if not scientific_projection_available:
                availability = "unknown"
                reason = "No serving deployment is configured; scientific availability could not be checked."
        items.append(
            ModelInventoryItem(
                model_id=model_id,
                display_name=display_name,
                family=record.gateway.family if record else "scientific-batch",
                availability=availability,
                reason=reason,
                configured=configured,
                serving_enabled=enabled,
                serving_state=state,
                batch_readiness=profile.readiness if profile else None,
                ready_replicas=deployed.runtime.ready_replicas if deployed else None,
                desired_replicas=deployed.runtime.desired_replicas if deployed else None,
                runtime_image_digest=(
                    deployed.identity.runtime_image_digest
                    if deployed
                    else profile.backend.runtime_image_digest
                    if profile
                    else None
                ),
                gpu_snapshot=profile.caching.gpu_snapshot if profile else "not-reported",
                snapshot_reason=profile.caching.reason
                if profile
                else "No GPU snapshot capability is published for this runtime.",
                management_path=(
                    f"/admin/models/{model_id}" if deployed else "/admin/scientific-runs" if profile else None
                ),
            )
        )
    return ModelInventory(
        items=items,
        total=len(items),
        configured=sum(item.configured for item in items),
        not_deployed=sum(item.availability == "not-deployed" for item in items),
        scientific_projection_available=scientific_projection_available,
    )

"""Unified operator inventory without silently dropping undeployed models.

The inventory joins existing serving and scientific projections. It does not
grant inference access, synthesize readiness, or replace their control APIs.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .admin_models import AdminModelSummary
from .models import StrictModel
from .registry import OperationalModel
from .scientific_admin_models import ScientificModelReadiness


class SnapshotStartupMeasurement(StrictModel):
    clock: str
    n: int = Field(ge=1)
    median_seconds: float = Field(ge=0)
    min_seconds: float | None = Field(default=None, ge=0)
    max_seconds: float | None = Field(default=None, ge=0)
    cache: str


class SnapshotInventory(StrictModel):
    gpu_snapshot: Literal["verified", "candidate", "unsupported", "unavailable", "not-reported"] = "not-reported"
    snapshot_reason: str = "No GPU snapshot capability is published for this runtime."
    snapshot_evidence_scope: Literal[
        "not-reported",
        "unqualified",
        "historical",
        "isolated-qualified",
        "tested-incompatible",
        "not-applicable",
        "runtime-mismatch",
        "configured-qualified",
    ] = "not-reported"
    snapshot_selectable: bool = False
    snapshot_bundle_ids: list[str] = Field(default_factory=list, max_length=32)
    snapshot_normal_startup: SnapshotStartupMeasurement | None = None
    snapshot_restore_startup: SnapshotStartupMeasurement | None = None


class ModelInventoryItem(SnapshotInventory):
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
    management_path: str | None = None


class ModelInventory(StrictModel):
    items: list[ModelInventoryItem] = Field(max_length=512)
    total: int
    configured: int
    not_deployed: int
    scientific_projection_available: bool


def load_snapshot_capabilities(path: Path) -> dict[str, dict[str, Any]]:
    """Read the single packaged evidence projection; no duplicated catalog."""
    try:
        document = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}
    if not isinstance(document, dict) or document.get("schema") != "fs2-serve.nebius.ai/fleet-snapshot-capabilities/v1":
        raise ValueError("snapshot capability schema differs")
    models = document.get("models")
    if not isinstance(models, list) or any(
        not isinstance(model, dict) or not isinstance(model.get("model_id"), str) for model in models
    ):
        raise ValueError("snapshot capability models differ")
    return {model["model_id"]: model for model in models}


def _image_digest(value: str | None) -> str | None:
    return value.rsplit("@", 1)[-1] if value else None


def _snapshot_inventory(
    model_id: str,
    runtime_image: str | None,
    profile: ScientificModelReadiness | None,
    capabilities: Mapping[str, Mapping[str, Any]],
    bundles: Mapping[str, Mapping[str, Any]],
    serving: AdminModelSummary | None = None,
    serving_bundles: Mapping[str, Mapping[str, Any]] | None = None,
) -> SnapshotInventory:
    evidence = capabilities.get(model_id)
    if evidence is None:
        return SnapshotInventory(
            gpu_snapshot=profile.caching.gpu_snapshot if profile else "not-reported",
            snapshot_reason=profile.caching.reason
            if profile
            else "No GPU snapshot capability is published for this runtime.",
        )
    status = evidence.get("status")
    projection = SnapshotInventory(
        gpu_snapshot="candidate",
        snapshot_reason=str(evidence.get("reason", "This runtime has no qualified GPU snapshot option.")),
        snapshot_evidence_scope="unqualified",
        snapshot_normal_startup=evidence.get("normal_startup"),
        snapshot_restore_startup=evidence.get("restore_startup"),
    )
    measured_image = _image_digest(evidence.get("runtime_image_digest"))
    if measured_image is not None and runtime_image is not None and measured_image != _image_digest(runtime_image):
        projection.snapshot_evidence_scope = "runtime-mismatch"
        projection.snapshot_reason = (
            "Published snapshot evidence belongs to a different runtime image. " + projection.snapshot_reason
        )
        # Do not present another image's timings as this runtime's expectation.
        projection.snapshot_normal_startup = None
        projection.snapshot_restore_startup = None
        return projection
    if status == "not-applicable":
        projection.gpu_snapshot = "unsupported"
        projection.snapshot_evidence_scope = "not-applicable"
    elif status == "tested-incompatible":
        projection.gpu_snapshot = "unsupported"
        projection.snapshot_evidence_scope = "tested-incompatible"
    elif status == "historical-isolated-proof":
        projection.snapshot_evidence_scope = "historical"
    elif status == "fresh-pod-qualified":
        projection.snapshot_evidence_scope = "isolated-qualified"
        captured = evidence.get("bundle") or {}
        if (
            measured_image is not None
            and runtime_image is not None
            and profile is not None
            and profile.workload_profile == "published"
            and profile.readiness == "qualified"
        ):
            projection.snapshot_bundle_ids = sorted(
                bundle_id
                for bundle_id, bundle in bundles.items()
                if bundle.get("qualified") is True
                and bundle.get("model_id") == model_id
                and _image_digest(bundle.get("runtime_image")) == _image_digest(runtime_image) == measured_image
                and bundle.get("profile_model_revision") == profile.backend.model_revision
                and bundle.get("model_revision") == evidence.get("model_revision")
                and bundle_id == captured.get("id")
                and bundle.get("manifest_sha256") == captured.get("sha256")
                and bundle.get("qualification_receipt_sha256") == captured.get("qualification_receipt_sha256")
                and evidence.get("fresh_pod_restore_passed") is True
                and evidence.get("distinct_inputs_passed", 0) >= 2
            )
        elif serving is not None and measured_image is not None and runtime_image is not None:
            if serving.identity.gpu_count != 1 or serving.identity.gpu_class not in evidence.get(
                "accelerator_classes", []
            ):
                projection.snapshot_evidence_scope = "runtime-mismatch"
                projection.snapshot_reason = (
                    "Published snapshot measurements do not qualify this configured GPU class/count."
                )
                projection.snapshot_normal_startup = None
                projection.snapshot_restore_startup = None
                return projection
            projection.snapshot_bundle_ids = sorted(
                bundle_id
                for bundle_id, bundle in (serving_bundles or {}).items()
                if bundle.get("qualified") is True
                and bundle.get("model_ref") == model_id
                and _image_digest(bundle.get("runtime_image")) == _image_digest(runtime_image) == measured_image
                and bundle.get("model_revision") == serving.identity.model_revision == evidence.get("model_revision")
                and serving.identity.gpu_class in bundle.get("accelerator_classes", [])
                and bundle.get("compatibility") == evidence.get("compatibility")
                and bundle_id == captured.get("id")
                and bundle.get("manifest_sha256") == captured.get("sha256")
                and bundle.get("qualification_receipt_sha256") == captured.get("qualification_receipt_sha256")
                and evidence.get("fresh_pod_restore_passed") is True
                and evidence.get("distinct_inputs_passed", 0) >= 2
            )
        if projection.snapshot_bundle_ids:
            projection.gpu_snapshot = "verified"
            projection.snapshot_selectable = True
            projection.snapshot_evidence_scope = "configured-qualified"
            projection.snapshot_reason = (
                "A measured fresh-Pod snapshot bundle matching this published runtime is configured and selectable. "
                "Normal loading remains the default; configured availability does not mean "
                "the current run used a snapshot."
            )
            if serving is not None:
                projection.snapshot_reason += (
                    " GPU class is qualified; the exact captured driver/kernel is checked again inside each Pod. "
                    "Prefer permits normal fallback; this is not evidence of successful restore on another driver."
                )
    return projection


def build_model_inventory(
    catalog: Sequence[OperationalModel],
    serving: Sequence[AdminModelSummary],
    scientific: Sequence[ScientificModelReadiness],
    *,
    scientific_projection_available: bool,
    snapshot_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
    snapshot_bundles: Mapping[str, Mapping[str, Any]] | None = None,
    serving_snapshot_bundles: Mapping[str, Mapping[str, Any]] | None = None,
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
        runtime_image = (
            deployed.identity.runtime_image_digest
            if deployed
            else profile.backend.runtime_image_digest
            if profile
            else None
        )
        snapshot = _snapshot_inventory(
            model_id,
            runtime_image,
            profile if scientific_projection_available else None,
            snapshot_capabilities or {},
            snapshot_bundles or {},
            deployed,
            serving_snapshot_bundles or {},
        )
        items.append(
            ModelInventoryItem(
                **snapshot.model_dump(),
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
                runtime_image_digest=runtime_image,
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

"""Snapshot configuration must not masquerade as conventional loading."""

from datetime import UTC, datetime

import pytest
from test_serving_snapshot import fixture

from fs2_serve.fast_start_mechanisms import FastStartMechanism
from fs2_serve.model_deployment import SnapshotPreference, canonical_digest, validate_model_deployment
from fs2_serve.model_deployment_controller import _fast_start_status


def status_for(source, infrastructure, *, converged=True):
    now = datetime(2026, 9, 19, tzinfo=UTC)
    decision = validate_model_deployment(source, infrastructure, evaluation_time=now)
    result = _fast_start_status(
        spec=source,
        envelope=infrastructure,
        assessment=decision.fast_start,
        converged=converged,
        ready_replicas=0,
        previous_status={},
        history=None,
        now=now,
        mechanism_decision=decision.fast_start_mechanism,
    )
    assert result is not None
    return result.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize("preference", [SnapshotPreference.PREFER, SnapshotPreference.REQUIRE])
@pytest.mark.parametrize("converged", [False, True])
def test_exact_snapshot_is_configured_without_promoting_a_level_or_claiming_restore(preference, converged):
    source, infrastructure, _template, bundle, _context = fixture()
    source.cache.snapshot_preference = preference
    infrastructure.pools["pool-a"].node_selector["snapshot.fs2.nebius/eligible"] = "false"
    before = infrastructure.model_dump(mode="json", by_alias=True)

    result = status_for(source, infrastructure, converged=converged)

    mechanisms = result["cacheMechanisms"]
    assert mechanisms["conventional"]["selected"] is False
    assert mechanisms["conventional"]["reason"] == "ConventionalLoaderAvailable"
    snapshot = mechanisms["shared-restore"]
    assert snapshot["selected"] is True
    assert snapshot["state"] == ("Configured" if converged else "Pending")
    assert snapshot["reason"] == ("ServingSnapshotRenderConverged" if converged else "ServingSnapshotRenderPending")
    assert snapshot["configDigest"] == canonical_digest(bundle.model_dump(mode="json", by_alias=True))
    assert snapshot["pools"]["pool-a"]["reason"] == "ExactServingSnapshotBundleQualified"
    assert snapshot["pools"]["pool-a"]["evidenceSelector"] == {}
    assert snapshot["pools"]["pool-a"]["mechanismConfigDigest"] is None
    assert snapshot["telemetryState"] == "Unavailable"
    assert result["qualifiedLevel"] == "Off"
    assert result["effectiveLevel"] in (None, "Off")
    assert result["hot"] is False
    assert infrastructure.model_dump(mode="json", by_alias=True) == before


@pytest.mark.parametrize("mismatch", ["image", "artifact", "gpu", "digest", "bundle", "pool", "never", "loader"])
def test_absent_disabled_or_incompatible_bundle_cannot_claim_snapshot_configuration(mismatch):
    source, infrastructure, _template, _bundle, _context = fixture()
    if mismatch == "image":
        source.runtime.image = "registry.example/other@sha256:" + "9" * 64
    elif mismatch == "artifact":
        source.artifact.revision = "other-revision"
    elif mismatch == "gpu":
        infrastructure.pools["pool-a"].accelerator_class = "other-gpu"
    elif mismatch == "digest":
        source.cache.snapshot_ref.digest = "sha256:" + "9" * 64
    elif mismatch == "bundle":
        infrastructure.qualifications[source.model_ref].gpu_snapshot_bundles = {}
    elif mismatch == "pool":
        source.placement.pool_refs = ["unknown-pool"]
    elif mismatch == "never":
        source.cache.snapshot_preference = SnapshotPreference.NEVER
        source.cache.snapshot_ref = None
    else:
        source.cache.mechanism = FastStartMechanism.CONVENTIONAL

    result = status_for(source, infrastructure)

    snapshot = result["cacheMechanisms"].get("shared-restore")
    assert snapshot is None or snapshot["selected"] is False
    assert snapshot is None or snapshot["state"] not in ("Configured", "Pending")

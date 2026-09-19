"""Snapshot configuration must not masquerade as conventional loading."""

from datetime import UTC, datetime

import pytest
import yaml
from jsonschema import ValidationError, validate
from test_model_deployment import CRD
from test_serving_snapshot import fixture

from fs2_serve.fast_start import FastStartStatus
from fs2_serve.fast_start_mechanisms import FastStartMechanism
from fs2_serve.model_deployment import (
    SnapshotPreference,
    canonical_digest,
    plan_reconciliation,
    validate_model_deployment,
)
from fs2_serve.model_deployment_controller import (
    ControllerFiles,
    Discovery,
    _fast_start_status,
    _fast_start_status_payload,
    build_status,
)


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


@pytest.mark.parametrize("converged", [False, True])
def test_snapshot_status_wire_omits_empty_selectors_without_changing_domain_evidence(converged):
    source, infrastructure, _template, _bundle, _context = fixture()
    model = FastStartStatus.model_validate(status_for(source, infrastructure, converged=converged))
    before = model.model_dump(mode="json", by_alias=True, exclude_none=True)

    payload = _fast_start_status_payload(model)

    for name, mechanism in before["cacheMechanisms"].items():
        for pool_ref, pool in mechanism["pools"].items():
            actual = payload["cacheMechanisms"][name]["pools"][pool_ref]
            if pool["evidenceSelector"]:
                assert actual["evidenceSelector"] == pool["evidenceSelector"]
            else:
                assert "evidenceSelector" not in actual
    assert FastStartStatus.model_validate(payload) == model
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == before
    assert "mechanismConfigDigest" not in payload["cacheMechanisms"]["shared-restore"]["pools"]["pool-a"]


def test_build_status_snapshot_selector_transition_conforms_to_unchanged_crd():
    source, infrastructure, template, _bundle, context = fixture()
    plan = plan_reconciliation(
        generation=context.generation,
        deleting=False,
        spec=source,
        envelope=infrastructure,
        renderer=ControllerFiles(infrastructure_envelope=infrastructure, bundles=[template]).renderer(),
        render_context=context,
        observed=[],
        discovery_complete=True,
    )
    status = build_status(
        spec=source,
        owner_uid=context.uid,
        generation=context.generation,
        plan=plan,
        discovery=Discovery(resources=[], complete=True),
        previous_status={},
        drain=None,
        envelope=infrastructure,
    )
    pool = status["fastStart"]["cacheMechanisms"]["shared-restore"]["pools"]["pool-a"]
    assert "evidenceSelector" not in pool
    schema = yaml.safe_load(CRD.read_text())["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    fast_start_schema = schema["properties"]["status"]["properties"]["fastStart"]
    validate(status["fastStart"], fast_start_schema)
    mechanism_schema = fast_start_schema["properties"]["cacheMechanisms"]["additionalProperties"]
    pool_schema = mechanism_schema["properties"]["pools"]["additionalProperties"]
    # Live Strict SSA rejected this exact null shape when the old nonempty
    # snapshot eligibility selector was removed using {}. Do not relax the CRD.
    with pytest.raises(ValidationError, match="not of type 'object'"):
        validate({**pool, "evidenceSelector": None}, pool_schema)

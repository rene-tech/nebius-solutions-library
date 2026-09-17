"""Synthetic tests; private cluster snapshots are not repository fixtures."""

import copy

import pytest
from prepare_contract import (
    MODEL,
    TEMPLATE_NAME,
    VOICE_MODELS,
    immutable_configmap,
    merge_contract,
    terraform_digest,
    validate_specs,
)
from test_model_deployment import envelope as base_envelope
from test_model_deployment import model_spec, renderer

from fs2_serve.model_deployment import canonical_digest


def fixture():
    seed = next(iter(renderer()._bundles.values())).model_dump(mode="json", by_alias=True)
    old = terraform_digest(seed["resources"])
    original = base_envelope().model_dump(mode="json", by_alias=True)
    qualification = original["qualifications"]["qwen.3-8b"]
    identities = [MODEL, *[f"sibling-{i:02d}" for i in range(14)], *sorted(VOICE_MODELS)]
    bundles = []
    original["qualifications"] = {}
    for identity in identities:
        bundles.append({**copy.deepcopy(seed), "modelRef": identity, "templateDigest": old})
        original["qualifications"][identity] = {
            **copy.deepcopy(qualification), "modelRef": identity, "templateDigests": [old],
            "templateRefs": {identity + ".legacy-v1": old}, "templateCacheTiers": {old: "NodeLocal"},
        }
    before = copy.deepcopy(original)
    before["qualifications"] = {k: v for k, v in before["qualifications"].items() if k not in VOICE_MODELS}
    before_bundles = copy.deepcopy(bundles[:15])
    # The real voice baseline retains two additional speech template revisions.
    for identity in sorted(VOICE_MODELS)[:2]:
        extra = copy.deepcopy(next(b for b in bundles if b["modelRef"] == identity))
        extra["resources"][0]["metadata"]["annotations"] = {"fixture": "voice-v2"}
        extra["templateDigest"] = terraform_digest(extra["resources"])
        bundles.append(extra)
        q = original["qualifications"][identity]
        q["templateDigests"].append(extra["templateDigest"])
        q["templateRefs"][identity + ".voice-v2"] = extra["templateDigest"]
        q["templateCacheTiers"][extra["templateDigest"]] = "NodeLocal"
    planned, planned_bundles = copy.deepcopy((before, before_bundles))
    changed = next(b for b in planned_bundles if b["modelRef"] == MODEL)
    changed["resources"][0]["metadata"]["annotations"] = {"fixture": "Cosmos <&> revision"}
    new = terraform_digest(changed["resources"])
    changed["templateDigest"] = new
    planned["qualifications"][MODEL].update(templateDigests=[new],
        templateRefs={MODEL + ".legacy-v1": new}, templateCacheTiers={new: "NodeLocal"})
    for document in (original, before, planned):
        document.pop("revision")
        document["revision"] = canonical_digest(document)
    spec = model_spec().model_dump(mode="json", by_alias=True)
    spec["modelRef"] = MODEL
    spec["runtime"]["templateRef"] = {"name": MODEL + ".legacy-v1", "digest": old}
    deployment = {"metadata": {"name": MODEL, "namespace": "fs2-models", "generation": 14, "uid": "unit-uid"},
                  "spec": spec}
    return [original, bundles, before, before_bundles, planned, planned_bundles], deployment, old, new


def test_complete_additive_candidate_preserves_all_current_revisions_and_settings():
    inputs, deployment, old, new = fixture()
    prior = copy.deepcopy(inputs)
    candidate, bundles = merge_contract(*inputs, old=old, new=new)
    assert inputs == prior
    assert len(candidate["qualifications"]) == 20 and len(bundles) == 23
    assert bundles[:-1] == inputs[1]
    assert all(candidate["qualifications"][m] == inputs[0]["qualifications"][m]
               for m in candidate["qualifications"] if m != MODEL)
    qualification = candidate["qualifications"][MODEL]
    assert qualification["templateRefs"] == {MODEL + ".legacy-v1": old, TEMPLATE_NAME: new}
    proposal, receipt = validate_specs(inputs[0], candidate, bundles, [deployment], new=new)
    expected = copy.deepcopy(deployment["spec"])
    expected["runtime"]["templateRef"] = {"name": TEMPLATE_NAME, "digest": new}
    assert proposal["spec"] == expected
    assert receipt["current_modeldeployments"][0]["before"] == "accepted"
    assert receipt["current_modeldeployments"][0]["after"] == "accepted"
    assert all(row["resource_count"] > 0 for row in receipt["cosmos_renders"].values())
    first = immutable_configmap("fs2-cosmos-envelope-", "infrastructure-envelope.json", candidate)
    assert first == immutable_configmap("fs2-cosmos-envelope-", "infrastructure-envelope.json", candidate)
    assert first["immutable"] is True


@pytest.mark.parametrize("mutation,error", [
    ("sibling_bundle", "unrelated_planned_bundle_change"),
    ("sibling_qualification", "unrelated_planned_qualification_change"),
    ("limit", "infrastructure_or_customer_settings_changed"),
    ("voice_missing", "expected_complete_twenty_model_live_baseline"),
    ("digest", "planned_cosmos_resource_digest_mismatch"),
    ("qualification", "non_template_cosmos_qualification_changed"),
    ("duplicate", "duplicate_template_identity"),
])
def test_candidate_refuses_sibling_loss_drift_and_tampered_template(mutation, error):
    inputs, _, old, new = fixture()
    if mutation == "sibling_bundle":
        inputs[5][1]["primaryServicePort"] += 1
    elif mutation == "sibling_qualification":
        inputs[4]["qualifications"]["sibling-00"]["scaleToZeroQualified"] = False
    elif mutation == "limit":
        inputs[4]["maxAcceleratorsPerModel"] += 1
    elif mutation == "voice_missing":
        del inputs[0]["qualifications"][sorted(VOICE_MODELS)[0]]
    elif mutation == "digest":
        inputs[5][0]["resources"][0]["metadata"]["annotations"]["fixture"] = "tampered"
    elif mutation == "qualification":
        inputs[4]["qualifications"][MODEL]["scaleToZeroQualified"] = False
    elif mutation == "duplicate":
        inputs[1].append(copy.deepcopy(inputs[1][0]))
    with pytest.raises(ValueError, match=error):
        merge_contract(*inputs, old=old, new=new)

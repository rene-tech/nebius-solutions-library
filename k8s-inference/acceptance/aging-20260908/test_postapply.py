"""Offline postapply comparisons; no deployment or network side effects."""

import copy

import postapply
import pytest


def settings():
    return {
        "app_id": "fixed-app",
        "app_revision": 3,
        "display_name": "Clinical PhenoAge",
        "academic_required": False,
        "execution_mode": "serving",
        "serving": {
            "namespace": "models",
            "name": "phenoage",
            "tenant_id": "tenant",
            "revision": 2,
            "etag": "sha256:fixed",
            "action": "update",
            "created_at": "fixed",
            "created_by": "owner",
            "previous_revision": 1,
            "spec": {
                "modelRef": "phenoage",
                "availability": {"minReplicas": 0, "maxReplicas": 1, "idleSeconds": 300, "cooldownSeconds": 300},
                "placement": {"poolRefs": ["cpu"]},
                "runtime": {"profile": "cpu", "image": "exact-image"},
                "cache": {"tier": "None"},
                "snapshot": {"enabled": False},
            },
        },
    }


def metadata():
    surface = {
        "id": "phenoage",
        "enabled": True,
        "capabilities": ["native"],
        "revision": "dynamic:sha256:fixed",
        "active_runtime": {"variant_id": "phenoage-clinical-cpu-v1"},
        "qualification": {
            "authority": "explicit-deployment-runtime-record",
            "states": {
                "registered": True,
                "runtime_ready": True,
                "semantic_qualified": True,
                **dict.fromkeys(postapply.PROMOTED, True),
            },
        },
    }
    option = {
        "model_ref": "phenoage",
        "scale_to_zero_qualified": True,
        "scale_to_zero_warning": None,
        "default_spec": {"runtime": {"profile": "cpu", "image": "exact-image"}},
        "gpu_snapshot_choices": [],
        "fast_start_qualified_level": "Off",
    }
    return surface, option


def test_exact_saved_policy_revision_preserved_but_new_readonly_metadata_is_allowed():
    before = settings()
    after = {**copy.deepcopy(before), "qualification": {"cold_start_qualified": True}, "observed_at": "new"}
    assert postapply.saved_settings("phenoage", before, after)["saved_policy_identity_and_revisions_unchanged"]


@pytest.mark.parametrize("kind", ["uuid", "app_revision", "serving_revision", "pool", "snapshot", "image"])
def test_no_persisted_policy_or_identity_change_can_be_hidden(kind):
    before, after = settings(), settings()
    if kind == "uuid":
        after["app_id"] = "different"
    elif kind == "app_revision":
        after["app_revision"] += 1
    elif kind == "serving_revision":
        after["serving"]["revision"] += 1
    elif kind == "pool":
        after["serving"]["spec"]["placement"]["poolRefs"] = ["new-pool"]
    elif kind == "snapshot":
        after["serving"]["spec"]["snapshot"]["enabled"] = True
    else:
        after["serving"]["spec"]["runtime"]["image"] = "different-image"
    with pytest.raises(AssertionError, match="saved_"):
        postapply.saved_settings("phenoage", before, after)


def test_promoted_discovery_and_configuration_exact_runtime():
    model, option = metadata()
    assert (
        postapply.qualified_metadata("phenoage", model, model, option, settings())["qualified_variant_id"]
        == "phenoage-clinical-cpu-v1"
    )


@pytest.mark.parametrize("kind", ["public_flag", "scale_option", "image", "variant", "etag"])
def test_missing_qualification_or_wrong_runtime_fails(kind):
    model, option = metadata()
    if kind == "public_flag":
        model["qualification"]["states"]["cold_start_qualified"] = False
    elif kind == "scale_option":
        option["scale_to_zero_qualified"] = False
    elif kind == "image":
        option["default_spec"]["runtime"]["image"] = "wrong"
    elif kind == "variant":
        model["active_runtime"]["variant_id"] = "wrong"
    else:
        model["revision"] = "dynamic:changed"
    with pytest.raises(AssertionError):
        postapply.qualified_metadata("phenoage", model, model, option, settings())

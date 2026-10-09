import copy

import pytest

from activate_bridge import (
    AFTER_SHA, BASE, BEFORE_SHA, IMAGE, NEW_CM, NS, OLD, OLD_CM, PREFIX,
    prepare, template,
)


def workload(role):
    suffix = "" if role == "control-plane" else "-" + role
    spec = {"containers": [{"name": role, "image": OLD, "env": [
        {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "unchanged", "key": "url"}}}
    ], "resources": {"limits": {"cpu": "2"}}}],
        "volumes": [{"name": "unrelated", "configMap": {"name": "preserved-idle-map"}}]}
    if role == "control-plane":
        spec["containers"][0]["env"] += [
            {"name": "FS2_SCIENTIFIC_BATCH_TOOLS_IMAGE", "value": OLD},
            {"name": "FS2_SCIENTIFIC_BATCH_SCHEDULING_CONTRACT_SHA256", "value": BEFORE_SHA},
        ]
        spec["initContainers"] = [{"name": "install-starter-pack", "image": "starter@sha256:unchanged"},
                                  {"name": "wait-schema", "image": OLD}]
        spec["volumes"].append({"name": "scientific-batch-scheduling", "configMap": {"name": OLD_CM}})
    obj = {"kind": "Deployment", "metadata": {"name": BASE + suffix, "namespace": NS,
           "uid": "uid", "resourceVersion": "123"},
           "spec": {"replicas": 3, "paused": False, "template": {
               "metadata": {"annotations": {"keep": "yes"}}, "spec": spec}}}
    if role == "maintenance":
        obj["kind"] = "CronJob"
        obj["spec"] = {"schedule": "*/1 * * * *", "suspend": False,
                       "jobTemplate": {"spec": {"template": obj["spec"]["template"]}}}
    return obj


@pytest.mark.parametrize("role", ["control-plane", "model-controller", "maintenance"])
def test_only_reviewed_template_fields_change_with_inverse(role):
    obj = workload(role)
    original = copy.deepcopy(obj)
    forward, inverse = prepare(obj)
    assert obj == original
    assert forward[0] == {"op": "test", "path": "/metadata/uid", "value": "uid"}
    assert forward[1] == (
        {"op": "test", "path": "/spec", "value": obj["spec"]} if role == "maintenance"
        else {"op": "test", "path": "/metadata/resourceVersion", "value": "123"})
    assert forward[-2]["value"] == template(obj)
    desired = copy.deepcopy(forward[-1]["value"])
    assert desired["spec"]["containers"][0]["image"] == IMAGE
    assert desired["metadata"]["annotations"].pop(PREFIX + "image-digest") == IMAGE.split("@")[1]
    desired["spec"]["containers"][0]["image"] = OLD
    if role == "control-plane":
        assert desired["spec"]["initContainers"][1]["image"] == IMAGE
        desired["spec"]["initContainers"][1]["image"] = OLD
        values = desired["spec"]["containers"][0]["env"]
        assert values[-2]["value"] == IMAGE
        assert values[-1]["value"] == AFTER_SHA
        values[-2]["value"], values[-1]["value"] = OLD, BEFORE_SHA
        assert desired["spec"]["volumes"][-1]["configMap"]["name"] == NEW_CM
        desired["spec"]["volumes"][-1]["configMap"]["name"] = OLD_CM
        for key in ("scientific-scheduling-sha256", "scientific-profiles-sha256", "catalog-rollout-digest"):
            desired["metadata"]["annotations"].pop(PREFIX + key)
    assert desired == template(original)
    assert inverse[-1]["value"] == template(original)
    assert inverse[-2]["value"] == forward[-1]["value"]
    assert all("replicas" not in row["path"] and "suspend" not in row["path"] for row in forward)


@pytest.mark.parametrize("field", ["image", "init", "tools", "sha", "volume", "sidecar"])
def test_unreviewed_reader_or_binding_fails_closed(field):
    obj = workload("control-plane")
    spec = template(obj)["spec"]
    if field == "image":
        spec["containers"][0]["image"] = IMAGE
    elif field == "init":
        spec["initContainers"][1]["image"] = IMAGE
    elif field == "tools":
        spec["containers"][0]["env"][-2]["value"] = IMAGE
    elif field == "sha":
        spec["containers"][0]["env"][-1]["value"] = AFTER_SHA
    elif field == "volume":
        spec["volumes"][-1]["configMap"]["name"] = NEW_CM
    else:
        spec["containers"].append({"name": "unexpected", "image": OLD})
    with pytest.raises(ValueError):
        prepare(obj)


def test_no_workshop_mutation():
    obj = workload("control-plane")
    obj["metadata"]["name"] = "fs2-mindeval-workshop"
    with pytest.raises(ValueError, match="Unexpected bridge target"):
        prepare(obj)


def test_maintenance_status_does_not_invalidate_exact_config_guard():
    first = workload("maintenance")
    second = copy.deepcopy(first)
    second["metadata"]["resourceVersion"] = "124"
    second["status"] = {"lastScheduleTime": "2026-10-05T19:30:00Z"}
    assert prepare(first) == prepare(second)
    second["spec"]["suspend"] = True
    assert prepare(first)[0][1] != prepare(second)[0][1]

"""Protect mutation ownership, real failure evidence, and unchanged cohorts."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("recovery_acceptance", Path(__file__).with_name("run_acceptance.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
OP = "92f3f692-0d68-45cf-a138-376100000001"
AT = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)


def node():
    return {"pool": "h100-1x", "ready": {"status": "Unknown", "reason": "NodeStatusUnknown", "lastTransitionTime": "2026-09-25T05:00:00Z"},
            "autoscaler": {"name": "dead-group", "health": {"status": "Unhealthy", "lastProbeTime": "2026-09-25T05:59:30Z",
            "cloudProviderTarget": 4, "nodeCounts": {"registered": {"ready": 0, "notStarted": 0, "total": 4}, "unregistered": 0, "longUnregistered": 0}},
            "scaleUp": {"status": "Unhealthy"}}}


def job():
    return {"metadata": {"name": "fs2-workflow-window-01-a1-abc", "namespace": "fs2-models", "uid": "job-uid", "resourceVersion": "12",
        "labels": {m.PREFIX + "tenant-id": m.TENANT, m.PREFIX + "operation-id": OP, m.PREFIX + "model-id": "gromacs",
                   m.PREFIX + "stage-id": "workflow", m.PREFIX + "attempt-id": "attempt-1"},
        "annotations": {m.PREFIX + "pool-preference": "h100-ondemand-1x,h100-1x"}},
        "spec": {"suspend": True, "template": {"spec": {"affinity": {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                {"matchExpressions": [{"key": m.POOL, "operator": "In", "values": ["h100-1x", "h100-ondemand-1x"]}]},
                {"matchExpressions": [{"key": "zone", "operator": "In", "values": ["test"]}]}]}}}}}}}


def test_patch_is_atomic_preserves_original_and_constrains_every_or_term():
    original = job()
    before = deepcopy(original)
    patch = m.injection_patch(original, [], [], m.TENANT, OP, "h100-1x")
    assert original == before
    assert {p["path"] for p in patch if p["op"] == "test"} == {
        "/metadata/uid", "/metadata/resourceVersion", "/metadata/name", "/metadata/namespace", "/spec/suspend"}
    terms = patch[-1]["value"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert all(term["matchExpressions"][-1] == {"key": m.POOL, "operator": "In", "values": ["h100-1x"]} for term in terms)
    assert patch[-1]["path"] == "/spec/template/spec/affinity"


@pytest.mark.parametrize("change", [
    lambda j: j["metadata"]["labels"].update({m.PREFIX + "tenant-id": "customer"}),
    lambda j: j["metadata"]["labels"].update({m.PREFIX + "operation-id": "another-operation"}),
    lambda j: j["metadata"]["labels"].update({m.PREFIX + "model-id": "namd"}),
    lambda j: j["metadata"].update(name="fs2-workflow-window-01-a2-abc"),
    lambda j: j["spec"].update(suspend=False),
    lambda j: j.update(status={"startTime": "2026-09-25T05:00:00Z"}),
    lambda j: j["metadata"].update(deletionTimestamp="2026-09-25T05:00:00Z"),
    lambda j: j["metadata"]["annotations"].update({m.PREFIX + "pool-preference": "h100-ondemand-1x"}),
])
def test_patch_refuses_wrong_owner_started_retried_or_racing_job(change):
    candidate = job()
    change(candidate)
    with pytest.raises(m.GateError):
        m.injection_patch(candidate, [], [], m.TENANT, OP, "h100-1x")


@pytest.mark.parametrize("kind", ["pod", "workload"])
def test_patch_refuses_objects_already_created_from_job(kind):
    resource = {"metadata": {"ownerReferences": [{"uid": "job-uid"}]}}
    with pytest.raises(m.GateError):
        m.injection_patch(job(), [resource] if kind == "pod" else [], [resource] if kind == "workload" else [], m.TENANT, OP, "h100-1x")


def test_dead_pool_requires_nonempty_all_unknown_plus_fresh_autoscaler_evidence():
    assert m.dead_pool([node()], "h100-1x", instant=AT)
    assert not m.dead_pool([], "h100-1x", instant=AT)
    ready = node()
    ready["ready"]["status"] = "True"
    assert not m.dead_pool([node(), ready], "h100-1x", instant=AT)


@pytest.mark.parametrize("change", [
    lambda n: n["ready"].update(status="False"),
    lambda n: n["ready"].update(reason="KubeletNotReady"),
    lambda n: n["ready"].update(lastTransitionTime="2026-09-25T05:59:00Z"),
    lambda n: n.update(autoscaler=None),
    lambda n: n["autoscaler"]["health"].update(lastProbeTime="2026-09-25T05:57:00Z"),
    lambda n: n["autoscaler"]["health"].update(cloudProviderTarget=5),
    lambda n: n["autoscaler"]["health"]["nodeCounts"].update(unregistered=1),
    lambda n: n["autoscaler"]["health"]["nodeCounts"]["registered"].update(notStarted=1),
    lambda n: n["autoscaler"]["scaleUp"].update(status="InProgress"),
])
def test_starting_or_unconfirmed_pools_are_not_dead(change):
    candidate = node()
    change(candidate)
    assert not m.dead_pool([candidate], "h100-1x", instant=AT)


def recovered_case():
    first = {"attempt_id": "one", "attempt_number": 1, "shard_id": "window-01", "outcome": "failed", "failure_kind": "infrastructure",
             "failure_code": m.FAILURE, "resource_released": True, "workload_uid": "old-job", "scheduling_admission": {
                 "resolved_pool_id": "h100-1x", "admitted_at": "2026-09-25T05:57:55Z"},
             "recovery": {"state": "recovered", "cause": m.FAILURE, "failed_pool_id": "h100-1x", "avoided_pool_ids": ["h100-1x"],
                          "eligible_pool_ids": ["h100-ondemand-1x"], "admitted_wait_seconds": 125, "max_attempts": 2}}
    second = {"attempt_id": "two", "attempt_number": 2, "shard_id": "window-01", "outcome": "succeeded", "failure_kind": None,
              "failure_code": None, "resource_released": True, "workload_uid": "new-job", "scheduling_admission": {
                  "resolved_pool_id": "h100-ondemand-1x", "admitted_at": "2026-09-25T06:00:15Z"}}
    status = {"batch": {"stages": [{"stage_id": "workflow", "attempts": [first, second]}]}}
    intermediate = deepcopy(status)
    intermediate["batch"]["stages"][0]["attempts"][0]["recovery"]["retry_not_before"] = "2026-09-25T06:00:15Z"
    snapshots = [{"at": AT.isoformat(), "nodes": [node()], "jobs": [], "workloads": [],
                  "pods": [{"attempt_id": "one", "node": None, "containers": []}], "public_status": intermediate},
                 {"at": "2026-09-25T06:00:20+00:00", "nodes": [node()], "pods": [], "workloads": [],
                  "jobs": [{"attempt_id": "two", "pool_preference": ["h100-ondemand-1x"], "created_at": "2026-09-25T06:00:16Z"}]}]
    return status, snapshots


def test_recovery_needs_both_historical_failure_and_observed_pool_exclusion():
    status, snapshots = recovered_case()
    assert m.verify_recovery(status, snapshots, "h100-1x")[0]["replacement_attempt"] == "two"
    snapshots[1]["jobs"][0]["pool_preference"].append("h100-1x")
    with pytest.raises(m.GateError, match="pool_exclusion"):
        m.verify_recovery(status, snapshots, "h100-1x")


def test_recovery_observer_accepts_null_before_failure_without_losing_backoff_evidence():
    status, snapshots = recovered_case()
    before_failure = deepcopy(snapshots[0])
    before_failure["public_status"]["batch"]["stages"][0]["attempts"][0]["recovery"] = None
    snapshots.insert(0, before_failure)
    assert m.verify_recovery(status, snapshots, "h100-1x")[0]["replacement_attempt"] == "two"
    snapshots[1]["public_status"]["batch"]["stages"][0]["attempts"][0]["recovery"] = None
    with pytest.raises(m.GateError, match="public_retry_backoff_not_observed"):
        m.verify_recovery(status, snapshots, "h100-1x")


@pytest.mark.parametrize("kind", ["jobs", "pods", "workloads"])
def test_replacement_cannot_overlap_old_job_pod_or_quota(kind):
    status, snapshots = recovered_case()
    stale = {"attempt_id": "one"} if kind != "workloads" else {"owners": [{"uid": "old-job"}]}
    if kind == "pods":
        stale.update(node=None, containers=[])
    snapshots[1][kind].append(stale)
    with pytest.raises(m.GateError, match="overlap"):
        m.verify_recovery(status, snapshots, "h100-1x")


def test_observer_does_not_turn_warm_success_or_unreleased_failure_into_recovery():
    status, snapshots = recovered_case()
    with pytest.raises(m.GateError, match="unscheduled"):
        m.verify_recovery(status, snapshots[1:], "h100-1x")
    status["batch"]["stages"][0]["attempts"][0]["resource_released"] = False
    with pytest.raises(m.GateError, match="released"):
        m.verify_recovery(status, snapshots, "h100-1x")


def test_replacement_must_respect_public_persisted_backoff():
    status, snapshots = recovered_case()
    snapshots[1]["jobs"][0]["created_at"] = "2026-09-25T06:00:14Z"
    with pytest.raises(m.GateError, match="backoff"):
        m.verify_recovery(status, snapshots, "h100-1x")


def test_admin_scopes_and_wildcard_model_grants_are_rejected():
    policy = {"tenant_id": m.TENANT, "principal_id": "test", "models": ["gromacs"], "max_concurrency": 3,
              "scopes": ["artifacts.write", "catalog.read", "inference.invoke", "mcp.invoke", "operations.read", "operations.result", "operations.cancel"]}
    assert m.ordinary_policy(policy, m.TENANT)["models"] == ["gromacs"]
    for key, value in (("models", ["*"]), ("scopes", policy["scopes"] + ["tenant.admin"])):
        with pytest.raises(m.GateError):
            m.ordinary_policy({**policy, key: value}, m.TENANT)


def test_two_cohorts_require_same_release_and_consecutive_distinct_runs(tmp_path):
    receipt = {"state": "passed", "scenario": "recovery", "operation_id": OP, "release_sha256": "digest", "release": {},
               "tenant_policy": {}, "key_id": "key", "model_revision": "model", "fixture": {}, "errors": [],
               "frozen_max_attempts_per_stage": 2, "owned_resources_remaining": 0, "idempotent_replay_verified": True,
               "started_at": "2026-09-25T05:00:00+00:00", "finished_at": "2026-09-25T05:05:00+00:00"}
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    m.save(first, receipt)
    receipt = {**receipt, "operation_id": "92f3f692-0d68-45cf-a138-376100000002", "started_at": "2026-09-25T05:05:01+00:00", "finished_at": "2026-09-25T05:10:00+00:00"}
    m.save(second, receipt)
    assert m.combine([first, second])["state"] == "two-clean-recovery-cohorts"
    third = tmp_path / "changed.json"
    m.save(third, {**receipt, "release_sha256": "new-digest"})
    with pytest.raises(m.GateError, match="identity_changed"):
        m.combine([first, third])
    with pytest.raises(m.GateError, match="duplicate"):
        m.combine([first, first])


def test_readback_normalizes_only_kubernetes_empty_egress_omission():
    desired = {"kind": "NetworkPolicy", "spec": {"policyTypes": ["Ingress", "Egress"], "egress": []}}
    observed = {"kind": "NetworkPolicy", "spec": {"policyTypes": ["Ingress", "Egress"]}}
    assert m.contains_subset(m.canonical_observed_resource(observed), desired)
    assert "egress" not in observed["spec"]
    observed["spec"]["egress"] = [{}]
    assert not m.contains_subset(m.canonical_observed_resource(observed), desired)
    observed["spec"].pop("egress")
    observed["spec"]["policyTypes"] = ["Ingress"]
    assert not m.contains_subset(m.canonical_observed_resource(observed), desired)


def concurrent_fixture(tmp_path, change_intent=None, change_submission=None):
    policy = {"tenant_id": m.TENANT, "principal_id": "pool-recovery-test", "max_concurrency": 3}
    expected = {"endpoint": "https://example.test/mcp", "tenant_policy": policy, "key_id": "same-key",
                "release_sha256": "same-release-hash", "release": {"source_revision": "same-revision"},
                "injection_mode": "task-only-create-webhook", "injector": {"plan_sha256": "same-injector"}}
    intent = {"schema": m.SCHEMA, "operation_id": None, **deepcopy(expected)}
    submission = {"operation": {"id": OP, "tenant_id": m.TENANT, "principal_id": policy["principal_id"], "model_id": "gromacs",
                               "model_revision": "same-model", "reused": False},
                  "batch": {"model_id": "gromacs", "workload_id": "logical-workload", "stages": [
                      {"stage_id": "workflow", "attempts": [{"workload_uid": "job-uid"}]}]}}
    if change_intent:
        change_intent(intent)
    if change_submission:
        change_submission(submission)
    directory = tmp_path / "existing"
    (directory / "customer").mkdir(parents=True)
    m.save(directory / "intent.json", intent)
    m.save(directory / "operation.json", {"operation_id": OP})
    m.save(directory / "customer/submission.json", submission)
    return directory, expected, submission


def concurrent_resources():
    labels = {m.PREFIX + key: value for key, value in {"tenant-id": m.TENANT, "model-id": "gromacs", "operation-id": OP,
                                                     "workload-id": "logical-workload"}.items()}
    owner = {"apiVersion": "batch/v1", "kind": "Job", "uid": "job-uid"}
    job = {"metadata": {"uid": "job-uid", "namespace": "fs2-models", "labels": deepcopy(labels)},
           "spec": {"template": {"metadata": {"labels": deepcopy(labels)}}}}
    pod = {"metadata": {"uid": "pod-uid", "namespace": "fs2-models", "labels": deepcopy(labels), "ownerReferences": [owner]}}
    workload = {"metadata": {"uid": "workload-uid", "namespace": "fs2-models", "ownerReferences": [owner],
                             "labels": {"kueue.x-k8s.io/job-uid": "job-uid"}},
                "spec": {"podSets": [{"template": {"metadata": {"labels": deepcopy(labels)}}}]}}
    return [job], [pod], [workload]


def test_explicit_existing_receipts_public_owner_and_unlabelled_workload_are_verified(tmp_path):
    directory, expected, status = concurrent_fixture(tmp_path)
    reads = []

    def read_status(operation):
        reads.append(operation)
        return status

    records, jobs = m.concurrent_cohorts([directory], expected, read_status)
    assert reads == [OP] and set(records) == {OP} and jobs == {"job-uid": OP}
    resources = concurrent_resources()
    assert m.validate_concurrent_resources(m.TENANT, records, jobs, *resources) == {"jobs": 1, "pods": 1, "workloads": 1}
    # A deleting Job may be gone while its attributed Pod/Workload remain.
    assert m.validate_concurrent_resources(m.TENANT, records, jobs, [], *resources[1:])["workloads"] == 1


@pytest.mark.parametrize("change", [
    lambda value: value.update(key_id="other-key"),
    lambda value: value.update(release_sha256="changed-release"),
    lambda value: value.update(release={"source_revision": "changed"}),
    lambda value: value.update(injector={"plan_sha256": "changed-injector"}),
    lambda value: value.update(endpoint="https://other.test/mcp"),
    lambda value: value["tenant_policy"].update(tenant_id="other-tenant"),
    lambda value: value["tenant_policy"].update(principal_id="other-principal"),
])
def test_concurrent_cohort_rejects_owner_key_release_endpoint_or_injector_change(tmp_path, change):
    directory, expected, status = concurrent_fixture(tmp_path, change_intent=change)
    with pytest.raises(m.GateError, match="identity_changed"):
        m.concurrent_cohorts([directory], expected, lambda _: status)


@pytest.mark.parametrize("change", [
    lambda value: value["operation"].update(tenant_id="customer"),
    lambda value: value["operation"].update(principal_id="customer-principal"),
    lambda value: value["operation"].update(model_id="namd"),
    lambda value: value["operation"].update(id="not-a-uuid"),
    lambda value: value["operation"].update(id="92f3f692-0d68-45cf-a138-376100000002"),
])
def test_concurrent_cohort_rejects_public_operation_owner_model_or_identity(tmp_path, change):
    directory, expected, status = concurrent_fixture(tmp_path)
    change(status)
    with pytest.raises(m.GateError):
        m.concurrent_cohorts([directory], expected, lambda _: status)


def test_concurrent_cohort_requires_all_receipts_and_distinct_operations(tmp_path):
    directory, expected, status = concurrent_fixture(tmp_path)
    with pytest.raises(m.GateError, match="duplicate_operation"):
        m.concurrent_cohorts([directory, directory], expected, lambda _: status)
    missing = tmp_path / "unrecorded"
    missing.mkdir()
    with pytest.raises(m.GateError, match="receipts_missing"):
        m.concurrent_cohorts([missing], expected, lambda _: status)


@pytest.mark.parametrize("change", [
    lambda jobs, pods, workloads: jobs[0]["metadata"]["labels"].update({m.PREFIX + "operation-id": "unrecorded"}),
    lambda jobs, pods, workloads: jobs[0]["spec"]["template"]["metadata"]["labels"].update({m.PREFIX + "tenant-id": "customer"}),
    lambda jobs, pods, workloads: pods[0]["metadata"]["labels"].update({m.PREFIX + "operation-id": "unrecorded"}),
    lambda jobs, pods, workloads: pods[0]["metadata"]["ownerReferences"][0].update(uid="unrecorded-job"),
    lambda jobs, pods, workloads: workloads[0]["metadata"]["ownerReferences"][0].update(uid="unrecorded-job"),
    lambda jobs, pods, workloads: workloads[0]["spec"]["podSets"][0]["template"]["metadata"]["labels"].update({m.PREFIX + "operation-id": "unrecorded"}),
    lambda jobs, pods, workloads: workloads[0]["metadata"]["labels"].update({m.PREFIX + "operation-id": "unrecorded"}),
])
def test_concurrent_inventory_rejects_unknown_or_conflicting_resource_owners(tmp_path, change):
    directory, expected, status = concurrent_fixture(tmp_path)
    records, known = m.concurrent_cohorts([directory], expected, lambda _: status)
    resources = concurrent_resources()
    change(*resources)
    with pytest.raises(m.GateError):
        m.validate_concurrent_resources(m.TENANT, records, known, *resources)


def test_before_acceptance_no_other_cohort_resources_are_queried():
    class Kube:
        def __init__(self):
            self.calls = []

        def owned(self, tenant, operation, known):
            self.calls.append((tenant, operation, known))
            return ["new-job"], [], []

    kube = Kube()
    assert m.current_owned(kube, m.TENANT, None, {"unrelated-job"}) == ([], [], []) and kube.calls == []
    assert m.current_owned(kube, m.TENANT, OP, {"new-job"}) == (["new-job"], [], [])
    assert kube.calls == [(m.TENANT, OP, {"new-job"})]


def test_new_acceptance_and_cleanup_cannot_adopt_or_cancel_existing_operation(tmp_path):
    _, expected, submission = concurrent_fixture(tmp_path)
    policy = expected["tenant_policy"]
    with pytest.raises(m.GateError, match="concurrent_cohort"):
        m.accepted_operation(submission, policy, {OP}, new=True)
    submission["operation"]["reused"] = True
    with pytest.raises(m.GateError, match="reused_existing"):
        m.accepted_operation(submission, policy, new=True)
    submission["operation"].update(reused=False, id="92f3f692-0d68-45cf-a138-376100000002")
    assert m.accepted_operation(submission, policy, {OP}, new=True) != OP

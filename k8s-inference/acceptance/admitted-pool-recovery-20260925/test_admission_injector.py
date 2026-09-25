"""Pure mutations, selector/sandbox boundaries, short-lived TLS, and evidence."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import admission_injector as m  # noqa: E402
import prepare_injector as render  # noqa: E402

SPEC = importlib.util.spec_from_file_location("acceptance_injector_tests", Path(__file__).with_name("run_acceptance.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
NOW = datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)
IMAGE = "registry/gromacs@sha256:" + "1" * 64
SERVER_IMAGE = "registry/control-plane@sha256:" + "2" * 64
OPERATION = "92f3f692-0d68-45cf-a138-376100000001"


def config(pause=0, synthetic=False):
    return {"tenant": m.TENANT, "namespace": m.NAMESPACE, "dead_pool": "h100-1x", "shard": "window-01",
            "runtime_image": IMAGE, "pause_seconds": pause, "synthetic_no_capacity": synthetic, "expires_at": "2026-09-25T11:00:00+00:00"}


def review(number=1):
    workload, attempt, name = m.expected_identity(OPERATION, "window-01", number)
    labels = {m.PREFIX + key: value for key, value in {
        "tenant-id": m.TENANT, "model-id": "gromacs", "stage-id": "workflow", "shard-id": "window-01",
        "operation-id": OPERATION, "workload-id": workload, "attempt-id": attempt}.items()}
    pools = ["h100-ondemand-1x", "h100-1x"] if number == 1 else ["h100-ondemand-1x"]
    annotations = {m.PREFIX + "pool-preference": ",".join(pools), m.PREFIX + "accelerator-resource": "nvidia.com/gpu",
                   m.PREFIX + "accelerator-count": "1", m.PREFIX + "scientific-manifest-sha256": "a" * 64,
                   m.PREFIX + "podset-resource-envelope-sha256": "b" * 64, m.PREFIX + "podset-resource-envelope": '{"frozen":true}'}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": m.NAMESPACE,
        "labels": labels, "annotations": annotations}, "spec": {"suspend": True, "backoffLimit": 0, "template": {
        "metadata": {"labels": deepcopy(labels)}, "spec": {"restartPolicy": "Never",
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": m.POOL, "operator": "In", "values": pools}]},
            {"matchExpressions": [{"key": m.POOL, "operator": "In", "values": pools}, {"key": "zone", "operator": "In", "values": ["a"]}]}]}}},
        "containers": [{"name": "scientific-stage", "image": IMAGE, "command": ["gmx", "mdrun"],
            "resources": {"requests": {"nvidia.com/gpu": "1", "cpu": "8", "memory": "64Gi"},
                          "limits": {"nvidia.com/gpu": "1", "cpu": "8", "memory": "64Gi"}}}],
        "initContainers": [{"name": "prepare-workspace", "image": SERVER_IMAGE, "resources": {
            "requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}}}]}}}}
    return {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "request": {
        "uid": "request-uid", "operation": "CREATE", "namespace": m.NAMESPACE,
        "resource": {"group": "batch", "version": "v1", "resource": "jobs"}, "userInfo": {"username": m.CONTROLLERS[0]}, "object": job}}


def patched(original, operations):
    result = deepcopy(original)
    for operation in operations:
        keys = [key.replace("~1", "/").replace("~0", "~") for key in operation["path"].split("/")[1:]]
        current = result
        for key in keys[:-1]:
            current = current[int(key)] if isinstance(current, list) else current[key]
        key = int(keys[-1]) if isinstance(current, list) else keys[-1]
        if operation["op"] == "test":
            assert current[key] == operation["value"]
        elif operation["op"] == "remove":
            del current[key]
        elif isinstance(current, list):
            current.insert(key, deepcopy(operation["value"]))
        else:
            current[key] = deepcopy(operation["value"])
    return result


def test_first_attempt_changes_only_affinity_and_is_idempotent():
    value = review()
    before = deepcopy(value)
    operations, reason = m.mutation(value, config(), NOW)
    assert reason == "dead_affinity_injected" and value == before
    assert len(operations) == 1 and operations[0]["path"] == "/spec/template/spec/affinity"
    value["request"]["object"] = patched(value["request"]["object"], operations)
    assert value["request"]["object"]["metadata"] == before["request"]["object"]["metadata"]
    terms = value["request"]["object"]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert all(term["matchExpressions"][-1] == {"key": m.POOL, "operator": "In", "values": ["h100-1x"]} for term in terms)
    assert m.mutation(value, config(), NOW) == ([], "dead_affinity_already_present")


@pytest.mark.parametrize("change", [
    lambda r: r["request"].update(operation="UPDATE"),
    lambda r: r["request"].update(operation="DELETE"),
    lambda r: r["request"].update(subResource="status"),
    lambda r: r["request"].update(namespace="default"),
    lambda r: r["request"]["resource"].update(resource="pods"),
    lambda r: r["request"]["userInfo"].update(username="system:serviceaccount:other:controller"),
    lambda r: r["request"]["object"]["metadata"].update(namespace="other"),
    lambda r: r["request"]["object"]["metadata"].update(name="fs2-workflow-window-01-a1-000000000000"),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "tenant-id": "customer"}),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "tenant-id": m.TENANT + "-sibling"}),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "model-id": "namd"}),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "stage-id": "aggregate"}),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "attempt-id": OPERATION}),
    lambda r: r["request"]["object"]["metadata"]["labels"].update({m.PREFIX + "workload-id": OPERATION}),
    lambda r: r["request"]["object"]["spec"].update(suspend=False),
    lambda r: r["request"]["object"].update(status={"startTime": "2026-09-25T06:00:00Z"}),
    lambda r: r["request"]["object"]["spec"]["template"]["metadata"].update(labels={}),
    lambda r: r["request"]["object"]["spec"]["template"]["spec"].update(nodeName="already-started"),
    lambda r: r["request"]["object"]["spec"]["template"]["spec"]["containers"][0].update(image="changed"),
    lambda r: r["request"]["object"]["metadata"]["annotations"].update({m.PREFIX + "pool-preference": "h100-ondemand-1x"}),
])
def test_mismatches_are_allowed_but_never_mutated(change):
    value = review()
    change(value)
    before = deepcopy(value)
    response = m.admit(value, config(), NOW)["response"]
    assert response["allowed"] and "patch" not in response and value == before


@pytest.mark.parametrize("value", [None, [], {}, {"request": None}, {"request": {}}, {"request": []}])
def test_malformed_input_is_fail_open(value):
    response = m.admit(value, config(), NOW)["response"]
    assert response["allowed"] and "patch" not in response


def test_expiry_and_disabled_retry_never_mutate():
    cfg = {**config(), "expires_at": NOW.isoformat()}
    assert "patch" not in m.admit(review(), cfg, NOW)["response"]
    assert "patch" not in m.admit(review(2), config(), NOW)["response"]
    with pytest.raises(m.NotEligible, match="conflict"):
        m.validate_config(config(150, True))


def test_second_attempt_init_uses_same_image_no_gpu_and_preserves_resources():
    value = review(2)
    before = deepcopy(value)
    operations, reason = m.mutation(value, config(150), NOW)
    assert reason == "init_pause_injected"
    value["request"]["object"] = patched(value["request"]["object"], operations)
    old, new = before["request"]["object"], value["request"]["object"]
    assert old["metadata"] == new["metadata"]
    assert old["spec"]["template"]["spec"]["containers"] == new["spec"]["template"]["spec"]["containers"]
    pause = new["spec"]["template"]["spec"]["initContainers"][0]
    assert pause["image"] == IMAGE and pause["command"] == ["/bin/sleep", "150"]
    assert "nvidia.com/gpu" not in json.dumps(pause)
    assert m.mutation(value, config(150), NOW) == ([], "init_pause_already_present")
    # Built-in defaults must not cause duplicate init insertion on reinvocation.
    pause["terminationMessagePolicy"] = "File"
    assert m.mutation(value, config(150), NOW)[0] == []


def barrier_job():
    value = review(2)
    operations, _ = m.mutation(value, config(synthetic=True), NOW)
    job = patched(value["request"]["object"], operations)
    job["metadata"].update(uid="job-uid", resourceVersion="12")
    workload = {"metadata": {"uid": "workload-uid", "ownerReferences": [{"uid": "job-uid"}]}, "status": {"conditions": [
        {"type": "QuotaReserved", "status": "False", "reason": "Pending", "message": "flavor h100-ondemand-1x does not match node affinity"}]}}
    return job, workload


def test_synthetic_barrier_is_idempotent_and_removal_restores_exact_original_affinity():
    job, workload = barrier_job()
    value = review(2)
    value["request"]["object"] = job
    assert m.mutation(value, config(synthetic=True), NOW) == ([], "synthetic_barrier_already_present")
    operations = runner.synthetic_return_patch(job, [], [workload], m.TENANT, OPERATION, "window-01")
    assert {x["op"] for x in operations} == {"test", "remove"}
    restored = patched(job, operations)
    assert restored["spec"]["template"]["spec"]["affinity"] == review(2)["request"]["object"]["spec"]["template"]["spec"]["affinity"]
    assert restored["metadata"] == job["metadata"]
    assert all("matchExpressions" in x["path"] for x in operations if x["op"] == "remove")


@pytest.mark.parametrize("change", [
    lambda j, w: j["metadata"]["labels"].update({m.PREFIX + "tenant-id": "customer"}),
    lambda j, w: j["spec"].update(suspend=False),
    lambda j, w: j.update(status={"startTime": "started"}),
    lambda j, w: w["status"].update(admission={"clusterQueue": "cq"}),
    lambda j, w: w["status"]["conditions"].append({"type": "QuotaReserved", "status": "True"}),
])
def test_synthetic_return_cannot_touch_admitted_started_or_other_owned_jobs(change):
    job, workload = barrier_job()
    change(job, workload)
    with pytest.raises(runner.GateError):
        runner.synthetic_return_patch(job, [], [workload], m.TENANT, OPERATION, "window-01")


def test_rendered_objects_have_exact_create_scope_fail_open_no_rbac_or_tokens():
    resources = render.bundle(config(150), SERVER_IMAGE, "# code", b"ca", b"cert", b"key")
    hook = resources["webhook.json"]["webhooks"][0]
    assert hook["rules"] == [{"apiGroups": ["batch"], "apiVersions": ["v1"], "resources": ["jobs"], "operations": ["CREATE"], "scope": "Namespaced"}]
    assert hook["objectSelector"]["matchLabels"][m.PREFIX + "tenant-id"] == m.TENANT
    assert hook["namespaceSelector"] == {"matchLabels": {"kubernetes.io/metadata.name": "fs2-models"}}
    assert hook["failurePolicy"] == "Ignore" and hook["timeoutSeconds"] == 2 and hook["sideEffects"] == "None"
    assert hook["reinvocationPolicy"] == "IfNeeded" and hook["matchPolicy"] == "Exact"
    items = resources["server.json"]["items"]
    assert not any("Role" in item["kind"] for item in items)
    deployment = next(item for item in items if item["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] and pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    assert "nvidia.com/gpu" not in json.dumps(pod)
    assert next(item for item in items if item["kind"] == "NetworkPolicy")["spec"]["egress"] == []
    assert next(item for item in items if item["kind"] == "Service")["spec"]["type"] == "ClusterIP"
    assert resources["tls-secret-private.json"]["immutable"] is True


@pytest.mark.parametrize("admission_path", ["/mutate", "/mutate?timeout=2s"])
def test_tls_chain_hostname_and_actual_https_handler(tmp_path, admission_path):
    mask = os.umask(0o077)
    try:
        ca, cert, key = render.certificates(tmp_path)
    finally:
        os.umask(mask)
    assert ca and cert and key and (tmp_path / "tls-private.key").stat().st_mode & 0o777 == 0o600
    server = m.AdmissionServer(("127.0.0.1", 0), m.Handler)
    server.injector_config = {**config(), "expires_at": "2099-01-01T00:00:00+00:00"}
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(tmp_path / "tls.crt"), str(tmp_path / "tls-private.key"))
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client_tls = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    # Hostname validation is verified independently by openssl verify in the
    # generator; localhost is intentionally absent from the service certificate.
    client_tls.check_hostname = False
    client = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=client_tls, timeout=3)
    try:
        client.request("GET", "/healthz")
        response = client.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"ok": True}
        client.request("POST", admission_path, body=json.dumps(review()), headers={"Content-Type": "application/json"})
        response = client.getresponse()
        payload = json.loads(response.read())["response"]
        assert response.status == 200 and payload["allowed"] and payload["uid"] == "request-uid"
        assert json.loads(base64.b64decode(payload["patch"]))[0]["path"] == "/spec/template/spec/affinity"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_init_pause_evidence_does_not_claim_real_scale_from_zero():
    attempt = {"attempt_id": "two", "attempt_number": 2, "shard_id": "window-01", "outcome": "succeeded"}
    status = {"batch": {"stages": [{"stage_id": "workflow", "attempts": [attempt]}]}}
    observed = [{"pods": [{"attempt_id": "two", "uid": "pod-uid", "node": "healthy", "init_pause": [
        {"name": m.PAUSE, "image": IMAGE, "command": ["/bin/sleep", "150"]}], "containers": [
        {"name": m.PAUSE, "image_id": "containerd://digest", "state": {"terminated": {"startedAt": "2026-09-25T07:00:00Z",
        "finishedAt": "2026-09-25T07:02:30Z", "exitCode": 0}}}]}]}]
    proof = runner.verify_init_pause(status, observed, config(150))
    assert proof["state"] == "passed" and proof["actual_scale_from_zero_proven"] is False
    observed[0]["pods"][0]["containers"][0]["state"]["terminated"]["finishedAt"] = "2026-09-25T07:02:29Z"
    with pytest.raises(runner.GateError, match="not_observed"):
        runner.verify_init_pause(status, observed, config(150))


def test_synthetic_wait_needs_affinity_no_fit_not_general_insufficient_quota():
    row = {"stage_id": "workflow", "shard_id": "window-01", "attempt_number": 2, "outcome": "active", "attempt_id": "two", "workload_uid": "job-uid"}
    snapshot = {"workloads": [{"uid": "workload-uid", "owners": [{"uid": "job-uid"}], "admission": None,
        "capacity_wait_conditions": [{"type": "QuotaReserved", "status": "False", "message": "no flavor matches node affinity"}]}]}
    assert runner.synthetic_wait_candidate([row], snapshot, config(synthetic=True))["attempt_id"] == "two"
    snapshot["workloads"][0]["capacity_wait_conditions"][0]["message"] = "insufficient quota"
    assert runner.synthetic_wait_candidate([row], snapshot, config(synthetic=True)) is None


@pytest.mark.parametrize("number,cfg", [(1, config()), (2, config(150)), (2, config(synthetic=True))])
def test_real_controller_podset_envelope_is_unchanged(number, cfg):
    envelope = pytest.importorskip("fs2_serve.scientific_batch.podset_envelope")
    models = pytest.importorskip("fs2_serve.scientific_batch.models")
    value = review(number)
    job = value["request"]["object"]
    before = envelope.envelope_from_manifest(job, models.WorkloadKind.JOB)
    operations, _ = m.mutation(value, cfg, NOW)
    after = envelope.envelope_from_manifest(patched(job, operations), models.WorkloadKind.JOB)
    assert after.to_json() == before.to_json() and after.digest == before.digest

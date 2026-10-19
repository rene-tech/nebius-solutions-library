"""Monitoring authorization, bounded queries, the other regions' API server proxy, and real response normalization."""

import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import app as appmod
import auth
import catalog
import jobs
import kube
import monitoring
from test_api import KEY_INFO, H, client, cluster  # noqa: F401 - pytest fixture registration


def transport(monkeypatch, responder):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(responder), **kwargs)
    )


def test_endpoint_metrics_are_authenticated_and_model_scoped(client, monkeypatch):
    c, _ = client
    calls = []
    transport(
        monkeypatch,
        lambda req: calls.append(req) or httpx.Response(200, json={"status": "success", "data": {"result": []}}),
    )
    assert c.get("/v1/endpoints/llm-example/metrics").status_code == 401
    assert c.get("/v1/endpoints/missing/metrics", headers=H).status_code == 404

    async def restricted(_):
        return {**KEY_INFO, "models": ["another-model"]}

    monkeypatch.setattr(auth, "key_info", restricted)
    assert c.get("/v1/endpoints/llm-example/metrics", headers=H).status_code == 403
    assert not calls


def test_metrics_missing_is_not_zero_and_nan_is_serializable(client, monkeypatch):
    c, _ = client
    queries = []

    def respond(req):
        q = req.url.params["query"]
        queries.append(q)
        values = [[time.time() - 15, "NaN"], [time.time(), "0.5"]] if "cpu_usage" in q else []
        return httpx.Response(
            200, json={"status": "success", "data": {"result": [{"values": values}] if values else []}}
        )

    transport(monkeypatch, respond)
    r = c.get("/v1/endpoints/llm-example/metrics?range=15m", headers=H)
    assert r.status_code == 200
    panels = {p["id"]: p for p in r.json()["panels"]}
    assert panels["requests"]["series"] == []
    assert panels["cpu"]["series"][0]["points"][0][1] is None
    assert panels["cpu"]["series"][0]["points"][1][1] == 0.5
    assert all('namespace="models"' in q or 'k8s_namespace_name="models"' in q for q in queries)


def test_partial_metric_failure_preserves_other_panels(client, monkeypatch):
    c, _ = client
    transport(
        monkeypatch,
        lambda req: httpx.Response(500)
        if "GPU_UTIL" in req.url.params["query"]
        else httpx.Response(200, json={"status": "success", "data": {"result": []}}),
    )
    r = c.get("/v1/endpoints/llm-example/metrics", headers=H)
    assert r.status_code == 200
    assert {p["id"] for p in r.json()["panels"] if p["unavailable"]} == {"gpu"}


def test_upstream_failure_is_clear_and_does_not_expose_body(client, monkeypatch):
    c, _ = client
    transport(monkeypatch, lambda req: httpx.Response(503, text="private credentials and internal URL"))
    r = c.get("/v1/endpoints/llm-example/logs", headers=H)
    assert r.status_code == 503 and "private credentials" not in r.text


@pytest.mark.parametrize(
    "query", ["range=30d", "end=nan", "end=inf", "end=99999999999", "limit=1001", "limit=0", "search=" + "a" * 201]
)
def test_log_query_bounds(client, query):
    c, _ = client
    assert c.get("/v1/endpoints/llm-example/logs?" + query, headers=H).status_code == 422


def test_log_search_is_literal_and_streams_are_sorted(client, monkeypatch):
    c, _ = client
    requests = []

    def respond(req):
        requests.append(req)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"pod": "worker", "container": "main"},
                            "values": [["1000000000", "first"], ["2000000000", "second"]],
                        }
                    ]
                },
            },
        )

    transport(monkeypatch, respond)
    r = c.get("/v1/endpoints/llm-example/logs", params={"search": '"} | json'}, headers=H)
    assert r.status_code == 200
    q = requests[0].url.params["query"]
    assert q.startswith('{namespace="models",inferenceservice="llm-example"}')
    assert q.endswith(' |= "\\"} | json"')
    assert [line["line"] for line in r.json()["lines"]] == ["second", "first"]


def test_operation_ownership_checked_before_monitoring(client, monkeypatch):
    c, _ = client
    calls = []
    monkeypatch.setattr(
        jobs, "find", lambda ns, id: ({"metadata": {"labels": {"serverless2.nebius/tenant": "other"}}}, "eu-north1")
    )
    transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(500))
    assert c.get("/v1/operations/op-other/logs", headers=H).status_code == 404
    assert not calls


class FakeAgentClient:
    """kube._client(region) of another region: the api-agent kubeconfig's host, CA and bearer token."""

    class configuration:
        host = "https://10.0.0.1:6443"
        ssl_ca_cert = None
        verify_ssl = True

    def update_params_for_auth(self, headers, querys, auth_settings):  # the client library's signature (36.x)
        assert auth_settings == ["BearerToken"]
        headers["Authorization"] = "Bearer agent-token"


def test_another_regions_monitoring_goes_through_its_api_server_proxy(client, monkeypatch):
    """Loki of another region is reached through that cluster's API server proxy with the api-agent credentials
    (the caller's key never leaves this API); the query stays scoped to the resource."""
    c, _ = client
    requests = []
    import keys

    monkeypatch.setattr(
        jobs,
        "find",
        lambda ns, id: (
            {
                "metadata": {
                    "name": id,
                    "labels": {"serverless2.nebius/tenant": "demo"},
                    "annotations": {"serverless2.nebius/key": keys.key_hash("sk-good")},
                }
            },
            "eu-south1",
        ),
    )
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "_client", lambda region: FakeAgentClient())
    transport(
        monkeypatch,
        lambda req: requests.append(req) or httpx.Response(200, json={"status": "success", "data": {"result": []}}),
    )
    r = c.get("/v1/operations/op-owned/logs?range=6h", headers=H)
    assert r.status_code == 200 and r.json()["region"] == "eu-south1"
    assert requests[0].url.host == "10.0.0.1"
    assert requests[0].url.path == "/api/v1/namespaces/monitoring/services/loki:3100/proxy/loki/api/v1/query_range"
    assert requests[0].headers["Authorization"] == "Bearer agent-token" and "sk-good" not in str(requests[0].headers)
    assert 'namespace="tenant-demo"' in requests[0].url.params["query"] and "region" not in requests[0].url.params


def test_fleet_endpoint_list_keeps_unreachable_region(client, monkeypatch):
    c, _ = client
    m = {**catalog.get("llm-example"), "regions": ["eu-north1", "eu-south1"]}
    monkeypatch.setattr(catalog, "all_models", lambda: {"llm-example": m})
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])

    def isvc(name, ns, region="eu-north1"):
        if region == "eu-south1":
            raise HTTPException(503, "unavailable")
        return {"metadata": {}, "spec": {"predictor": {}}}

    monkeypatch.setattr(kube, "isvc", isvc)
    rows = c.get("/v1/endpoints", headers=H).json()
    assert len(rows) == 2
    assert rows[0]["region"] == "eu-north1"
    assert rows[1]["status"] == "unavailable" and rows[1]["replicas_ready"] is None


def test_scaling_persists_and_rejects_invalid_range(client, monkeypatch):
    c, _ = client
    writes = []

    async def admin_info(_):
        return {**KEY_INFO, "metadata": {**KEY_INFO["metadata"], "role": "admin"}}

    monkeypatch.setattr(auth, "key_info", admin_info)
    m = {
        **catalog.get("llm-example"),
        "managed_by": "api",
        "spec": {"id": "llm-example", "scaling": {"min": 0, "max": 2}},
    }
    monkeypatch.setattr(appmod, "_endpoint_model", lambda id: m)
    monkeypatch.setattr(appmod, "_write_model", lambda spec, p, replace: writes.append(spec))
    monkeypatch.setattr(appmod, "get_endpoint", lambda *args: {"id": "llm-example"})
    assert c.patch("/v1/endpoints/llm-example?region=invalid", json={"min_replicas": 1}, headers=H).status_code == 400
    assert not writes
    assert c.patch("/v1/endpoints/llm-example", json={"min_replicas": 3}, headers=H).status_code == 422
    assert not writes
    assert (
        c.patch(
            "/v1/endpoints/llm-example", json={"min_replicas": 1, "scale_to_zero_after_s": 180}, headers=H
        ).status_code
        == 200
    )
    assert writes[0]["scaling"] == {"min": 1, "max": 2, "idle_s": 180}
    m["managed_by"] = "git"
    assert c.patch("/v1/endpoints/llm-example", json={"min_replicas": 1}, headers=H).status_code == 403
    assert len(writes) == 1


def test_container_arguments_and_environment_are_typed(client):
    c, fake = client
    r = c.post(
        "/v1/models/container-run:invoke",
        json={
            "mode": "run",
            "input": {
                "image": "example/image",
                "command": "python train.py",
                "gpus": 0,
                "args": ["value with spaces"],
                "env": {"TEST_VALUE": "a=b=c"},
            },
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    container = fake.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["spec"]["containers"][0]
    assert container["args"] == ["value with spaces"]
    assert container["command"][-2:] == ['python train.py "$@"', "--"]
    assert {v["name"]: v["value"] for v in container["env"]}["TEST_VALUE"] == "a=b=c"
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"mode": "run", "input": {"image": "example/image", "command": "true", "env": {"BAD-NAME": "v"}}},
        headers=H,
    )
    assert r.status_code == 400


def test_role_is_public_and_defaults_to_user(client):
    c, _ = client
    assert c.get("/v1/keys/me", headers=H).json()["role"] == "user"


@pytest.mark.parametrize(
    "params",
    [
        {"args": 0},
        {"args": None},
        {"args": ["valid", 1]},
        {"env": 0},
        {"env": None},
        {"env": {"VALID": 1}},
    ],
)
def test_container_rejects_invalid_argument_and_environment_shapes(client, params):
    c, _ = client
    response = c.post(
        "/v1/models/container-run:invoke",
        headers=H,
        json={"mode": "run", "input": {"image": "example/image", "command": "true", **params}},
    )
    assert response.status_code == 400


def test_unreachable_endpoint_state_does_not_invent_zero_replicas(monkeypatch):
    class Unreachable:
        def get_namespaced_custom_object(self, *args, **kwargs):
            raise ApiException(status=403, reason="forbidden")

    monkeypatch.setattr(kube, "api", lambda region: Unreachable())
    monkeypatch.setattr(kube, "_isvc_cache", {})
    assert kube.endpoint_status("example", "example-ns") == {"status": "unavailable", "replicas_ready": None}


def test_failed_revision_is_an_error_not_deploying(monkeypatch):
    """Knative marked the newest revision failed (ProgressDeadlineExceeded) and no pod exists: the endpoint reports
    `error` with Knative's message; while a pod exists it is still deploying; a Ready InferenceService without pods
    is scaled-to-zero."""
    from types import SimpleNamespace as NS

    state = {"conds": [], "pods": []}

    class Api:
        def get_namespaced_custom_object(self, *a, **k):
            return {"status": {"conditions": state["conds"]}}

    class Core:
        def list_namespaced_pod(self, ns, label_selector=None, **k):
            return NS(items=state["pods"])

    monkeypatch.setattr(kube, "api", lambda region: Api())
    monkeypatch.setattr(kube, "core", lambda region: Core())
    monkeypatch.setattr(kube, "_isvc_cache", {})
    failed = [
        {
            "type": "Ready",
            "status": "False",
            "reason": "RevisionFailed",
            "message": 'Revision "x-00002" failed with message: Initial scale was never achieved.',
        }
    ]
    state["conds"] = failed
    st = kube.endpoint_status("x", "models")
    assert (
        st["status"] == "error"
        and st["replicas_ready"] == 0
        and st["reason"].startswith('RevisionFailed: Revision "x-00002" failed')
    )
    kube._isvc_cache.clear()
    state["pods"] = [NS(status=NS(phase="Pending", container_statuses=[]), metadata=NS(creation_timestamp=None))]
    assert kube.endpoint_status("x", "models")["status"] == "deploying"  # a pod is starting: not given up yet
    kube._isvc_cache.clear()
    state["pods"], state["conds"] = [], [{"type": "Ready", "status": "True"}]
    assert kube.endpoint_status("x", "models")["status"] == "scaled-to-zero"
    kube._isvc_cache.clear()
    state["conds"] = [{"type": "Ready", "status": "False", "reason": "PredictorConfigurationReady not ready"}]
    assert kube.endpoint_status("x", "models")["status"] == "deploying"  # not a terminal failure


def test_monitoring_of_a_retired_region_says_so(monkeypatch):
    """An operation that ran in a region that left the fleet: 410 with the explanation, not a connection error."""

    import kube

    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-north2"])
    monkeypatch.setattr(kube, "_client", lambda region: FakeAgentClient())
    with pytest.raises(HTTPException) as e:
        monitoring.target(monitoring.PROMETHEUS_URL, "eu-west1")
    assert e.value.status_code == 410 and "no longer part of this fleet" in e.value.detail
    assert monitoring.target(monitoring.PROMETHEUS_URL, "eu-north1") == (
        monitoring.PROMETHEUS_URL,
        {},
        True,
    )  # this cluster: direct
    base, headers, verify = monitoring.target(monitoring.PROMETHEUS_URL, "eu-north2")
    assert (
        base
        == "https://10.0.0.1:6443/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy"
    )
    assert headers == {"Authorization": "Bearer agent-token"} and verify is True


def test_endpoint_metrics_carry_the_startup_breakdown(client, monkeypatch):
    """Per pod created inside the range: node wait, init, pull+start, load -> ready, from kube-state-metrics."""
    c, _ = client
    now = time.time()
    t0 = now - 600

    def respond(req):
        q = req.url.params["query"]
        if "query_range" in str(req.url):
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        metric = {"pod": "llm-example-predictor-00001-deployment-abc-xyz"}

        def one(v, **labels):
            return {"status": "success", "data": {"result": [{"metric": {**metric, **labels}, "value": [now, str(v)]}]}}

        if q.startswith("last_over_time(kube_pod_created"):
            body = one(t0)
        elif "kube_pod_status_scheduled_time" in q:
            body = one(t0 + 90)
        elif "kube_pod_status_initialized_time" in q:
            body = one(t0 + 100)
        elif "kube_pod_container_state_started" in q:
            body = one(t0 + 400)
        elif "kube_pod_status_ready_time" in q:
            body = one(t0 + 520)
        elif "kube_node_created" in q:
            body = one(t0 - 30)
        elif "kube_pod_labels" in q:
            assert 'label_serverless2_nebius_gpu_snapshot="true"' in q
            body = {"status": "success", "data": {"result": []}}  # no snapshotting on this fleet yet
        elif "kube_pod_info" in q:
            body = one(1, node="computeinstance-new")
        else:
            body = {"status": "success", "data": {"result": []}}
        assert 'namespace="models"' in q
        return httpx.Response(200, json=body)

    transport(monkeypatch, respond)
    r = c.get("/v1/endpoints/llm-example/metrics?range=1h", headers=H)
    assert r.status_code == 200, r.text
    rows = r.json()["startups"]
    assert len(rows) == 1
    row = rows[0]
    assert row["node"] == "computeinstance-new" and row["new_node"] is True and row["ready"] is True
    assert row["gpu_snapshot"] is False
    assert (row["node_wait_s"], row["init_s"], row["pull_start_s"], row["load_s"], row["total_s"]) == (
        90,
        10,
        300,
        120,
        520,
    )


def test_startup_rows_of_a_run_read_the_queue_wait_from_the_manager_admission():
    """Root's reproduction (2026-10-19): manager Job at t=0, Workload Admitted at t=598, the worker's first pod at
    t=605, a replacement pod at t=4200. The queue wait is submission → admission (598), the dispatch admission → the
    first pod (7); the retry carries neither (an earlier attempt's run time is never read as waiting)."""
    import monitoring

    t0 = 1_000_000.0

    def vec(values):
        return {"result": [{"metric": {"pod": pod, "node": "n1"}, "value": [t0, str(v)]} for pod, v in values.items()]}

    pods = {"op-1-first": t0 + 605, "op-1-retry": t0 + 4200}
    results = {
        "created": vec(pods),
        "scheduled": vec({p: v + 5 for p, v in pods.items()}),
        "initialized": vec({p: v + 50 for p, v in pods.items()}),
        "started": vec({p: v + 200 for p, v in pods.items()}),
        "ready": vec({p: v + 201 for p, v in pods.items()}),
        "node": vec({p: 1 for p in pods}),
        "node_created": vec({p: t0 - 86400 for p in pods}),
        "gpu_snapshot": {"result": []},
    }
    rows = {r["pod"]: r for r in monitoring._startups(results, t0, submitted=t0, admitted=t0 + 598)}
    first, retry = rows["op-1-first"], rows["op-1-retry"]
    assert first["attempt"] == 1 and first["queue_wait_s"] == 598 and first["dispatch_s"] == 7
    assert retry["attempt"] == 2 and retry["queue_wait_s"] is None and retry["dispatch_s"] is None
    assert (first["node_wait_s"], first["init_s"], first["pull_start_s"], first["load_s"]) == (5, 45, 150, 1)
    # an unreadable Workload leaves the queue wait unknown, never a guess from the worker Job
    rows = {r["pod"]: r for r in monitoring._startups(results, t0, submitted=t0, admitted=None)}
    assert rows["op-1-first"]["queue_wait_s"] is None and rows["op-1-first"]["dispatch_s"] is None
    q = monitoring.startup_queries("tenant-demo", "op-1", 3600, "job")
    assert 'container="main"' in q["started"] and "kube_job_created" not in str(q)


def test_a_fleet_placed_runs_startups_combine_the_managers_admission_with_the_workers_pods(client, monkeypatch):
    """The admission is read on the manager cluster (this API's), the pod timestamps in the worker's region."""
    c, _ = client
    import keys

    now = time.time()
    t0 = now - 3000
    manager = {
        "metadata": {
            "name": "op-owned",
            "uid": "uid-1",
            "creationTimestamp": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "labels": {"serverless2.nebius/tenant": "demo"},
            "annotations": {"serverless2.nebius/key": keys.key_hash("sk-good")},
        }
    }
    monkeypatch.setattr(jobs, "find", lambda ns, id: (manager, "eu-south1"))
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "eu-south1"])
    monkeypatch.setattr(kube, "_client", lambda region: FakeAgentClient())
    seen = {}

    def admission(ns, uid, region):
        seen["admission"] = (ns, uid, region)
        return {"admitted": True, "admitted_at": datetime.fromtimestamp(t0 + 598, tz=timezone.utc).isoformat()}

    monkeypatch.setattr(kube, "workload_admission", admission)
    hosts = []

    def respond(req):
        hosts.append(req.url.host)
        q = req.url.params["query"]
        if "query_range" in str(req.url):
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})

        def one(v, **labels):
            return {
                "status": "success",
                "data": {"result": [{"metric": {"pod": "op-owned-abcde", **labels}, "value": [now, str(v)]}]},
            }

        if q.startswith("last_over_time(kube_pod_created"):
            body = one(t0 + 605)
        elif "kube_pod_status_scheduled_time" in q:
            body = one(t0 + 610)
        elif "kube_pod_status_initialized_time" in q:
            body = one(t0 + 655)
        elif "kube_pod_container_state_started" in q:
            assert 'container="main"' in q
            body = one(t0 + 805)
        elif "kube_pod_status_ready_time" in q:
            body = one(t0 + 806)
        elif "kube_pod_info" in q:
            body = one(1, node="south-node")
        else:
            body = {"status": "success", "data": {"result": []}}
        return httpx.Response(200, json=body)

    transport(monkeypatch, respond)
    r = c.get("/v1/operations/op-owned/metrics?range=1h", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["region"] == "eu-south1" and set(hosts) == {"10.0.0.1"}  # the worker's Prometheus, by proxy
    assert seen["admission"] == ("tenant-demo", "uid-1", kube.REGION)  # the manager's Workload, here
    [row] = r.json()["startups"]
    assert row["attempt"] == 1 and row["queue_wait_s"] == 598 and row["dispatch_s"] == 7
    assert (row["node_wait_s"], row["init_s"], row["pull_start_s"], row["load_s"]) == (5, 45, 150, 1)

import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_model_deployment_controller import fence

from fs2_serve.model_deployment import FIELD_MANAGER, RenderedResource, canonical_digest
from fs2_serve.model_deployment_controller import (
    REPLICA_HANDOFF_FIELD_MANAGER,
    ControllerError,
    FenceLostError,
    HttpKubernetesModelClient,
)


def resource():
    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "voice",
            "namespace": "fs2-models",
            "ownerReferences": [
                {
                    "apiVersion": "inference.fs2.nebius.ai/v1alpha1",
                    "kind": "ModelDeployment",
                    "name": "voice",
                    "uid": "cr-uid-1",
                    "controller": True,
                }
            ],
        },
        "spec": {"replicas": 2, "template": {"metadata": {"labels": {"keep": "yes"}}}},
    }
    return RenderedResource(
        api_version="apps/v1",
        kind="Deployment",
        namespace="fs2-models",
        name="voice",
        manifest=manifest,
        digest=canonical_digest(manifest),
    )


class ReplicaAPI:
    def __init__(self, desired, refusal=None):
        self.actual = copy.deepcopy(desired.manifest)
        self.actual["spec"]["replicas"] = 1
        self.actual["metadata"].update(uid="deployment-uid", resourceVersion="11")
        self.replica_managers = {"keda"}
        self.refusal = refusal
        self.patches = []
        if refusal == "foreign_owner":
            self.actual["metadata"]["ownerReferences"][0]["uid"] = "foreign"
        if refusal == "unknown_manager":
            self.replica_managers.add("unrelated-operator")

    def body(self):
        value = copy.deepcopy(self.actual)
        value["metadata"]["managedFields"] = [
            {
                "manager": manager,
                "operation": "Apply" if manager.startswith("fs2-") else "Update",
                "fieldsV1": {"f:spec": {"f:replicas": {}}},
            }
            for manager in sorted(self.replica_managers)
        ]
        return value

    def __call__(self, request):
        if request.method == "GET":
            if request.url.path.endswith(("/scaledobjects", "/horizontalpodautoscalers")):
                items = []
                if self.refusal in {"live_hpa", "deleting_hpa"} and request.url.path.endswith(
                    "/horizontalpodautoscalers"
                ):
                    items = [
                        {
                            "metadata": {"deletionTimestamp": "pending"} if self.refusal == "deleting_hpa" else {},
                            "spec": {"scaleTargetRef": {"name": "voice", "kind": "Deployment"}},
                        }
                    ]
                if self.refusal == "live_scaler" and request.url.path.endswith("/scaledobjects"):
                    items = [{"spec": {"scaleTargetRef": {"name": "voice"}}}]
                return httpx.Response(
                    200,
                    json={
                        "items": items,
                        "metadata": {"continue": "more"} if self.refusal == "incomplete_inventory" else {},
                    },
                )
            return httpx.Response(200, json=self.body())
        assert request.method == "PATCH"
        assert request.headers["content-type"] == "application/apply-patch+yaml"
        body = json.loads(request.content)
        manager = request.url.params["fieldManager"]
        self.patches.append((manager, request.url.params["force"], body))
        assert body["metadata"]["resourceVersion"] == self.actual["metadata"]["resourceVersion"]
        if self.refusal == "stale_version":
            return httpx.Response(409, json={"message": "conflict"})
        if manager == REPLICA_HANDOFF_FIELD_MANAGER:
            assert set(body) <= {"apiVersion", "kind", "metadata", "spec"}
            assert set(body["metadata"]) == {"name", "namespace", "uid", "resourceVersion"}
            assert body["metadata"]["uid"] == "deployment-uid"
            if "spec" in body:
                assert body["spec"] == {"replicas": 2}
                assert request.url.params["force"] == "true"
                self.actual["spec"]["replicas"] = 2
                self.replica_managers = {manager}
            else:
                assert request.url.params["force"] == "false"
                assert FIELD_MANAGER in self.replica_managers
                self.replica_managers.remove(manager)
        else:
            assert manager == FIELD_MANAGER and request.url.params["force"] == "false"
            if "replicas" in body["spec"]:
                assert self.actual["spec"]["replicas"] == body["spec"]["replicas"]
                self.replica_managers.add(FIELD_MANAGER)
            else:
                self.replica_managers.discard(FIELD_MANAGER)
            assert body["spec"]["template"] == self.actual["spec"]["template"]
        self.actual["metadata"]["resourceVersion"] = str(int(self.actual["metadata"]["resourceVersion"]) + 1)
        return httpx.Response(200, json=self.body())


def client_for(tmp_path, api):
    token = tmp_path / "token"
    token.write_text("projected-test-service-account-token")
    http = httpx.AsyncClient(transport=httpx.MockTransport(api), base_url="https://kubernetes.invalid")
    client = HttpKubernetesModelClient(
        base_url="https://kubernetes.invalid",
        token_file=token,
        ca_file=tmp_path / "ca",
        writes_enabled=True,
        client=http,
    )
    client.assert_fence = AsyncMock()
    return client, http


@pytest.mark.asyncio
async def test_fixed_floor_reclaims_only_replicas_then_releases_temporary_manager(tmp_path):
    desired = resource()
    api = ReplicaAPI(desired)
    client, http = client_for(tmp_path, api)
    async with http:
        result = await client.apply_resource(desired, owner_uid="cr-uid-1", fence=fence())
    assert result.desired_replicas == 2
    assert result.replica_field_managers == [FIELD_MANAGER]
    assert [(m, force) for m, force, _ in api.patches] == [
        (REPLICA_HANDOFF_FIELD_MANAGER, "true"),
        (FIELD_MANAGER, "false"),
        (REPLICA_HANDOFF_FIELD_MANAGER, "false"),
    ]
    assert client.assert_fence.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [
        "foreign_owner",
        "unknown_manager",
        "live_hpa",
        "deleting_hpa",
        "live_scaler",
        "incomplete_inventory",
        "stale_version",
        "lost_fence",
    ],
)
async def test_replica_handoff_refuses_unsafe_or_unobserved_ownership(tmp_path, refusal):
    desired = resource()
    api = ReplicaAPI(desired, refusal)
    client, http = client_for(tmp_path, api)
    if refusal == "lost_fence":
        client.assert_fence.side_effect = FenceLostError("changed")
    async with http:
        with pytest.raises(ControllerError):
            await client.apply_resource(desired, owner_uid="cr-uid-1", fence=fence())
    assert api.actual["spec"]["replicas"] == 1
    assert len(api.patches) == (1 if refusal == "stale_version" else 0)


@pytest.mark.asyncio
async def test_reverse_fixed_to_autoscaled_omits_replicas_and_never_forces(tmp_path):
    desired = resource()
    api = ReplicaAPI(desired)
    api.replica_managers = {FIELD_MANAGER, "keda"}
    desired.manifest["spec"].pop("replicas")
    client, http = client_for(tmp_path, api)
    async with http:
        result = await client.apply_resource(desired, owner_uid="cr-uid-1", fence=fence())
    assert result.replica_field_managers == ["keda"]
    assert [(m, force) for m, force, _ in api.patches] == [(FIELD_MANAGER, "false")]

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fs2_serve.scientific_batch.writer_proxy import (
    Mutation,
    ScientificExecutionPolicy,
    ScientificWriter,
    ScientificWriterError,
    validate_scientific_manifest,
)

CALLER = "system:serviceaccount:fs2-system:fs2-serve-control-plane-runtime"
PROFILE_LABELS = {
    "app.kubernetes.io/part-of": "fs2-serve",
    "fs2-serve.nebius.ai/network-workload-class": "internal-job",
    "fs2-serve.nebius.ai/network-profile": "job-internal-v1",
}


class StubClient:
    def __init__(
        self, token_review: dict[str, Any], *, live: dict[str, Any] | None = None
    ) -> None:
        self.token_review = token_review
        self.live = live
        self.mutations: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, path: str, **_kwargs: object) -> httpx.Response:
        assert path == "/apis/authentication.k8s.io/v1/tokenreviews"
        return httpx.Response(201, json=self.token_review)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
        **_kwargs: object,
    ) -> httpx.Response:
        self.mutations.append((method, path, json))
        return httpx.Response(201, json={"kind": "Job"})

    async def get(self, path: str, **_kwargs: object) -> httpx.Response:
        if path == "/version":
            return httpx.Response(200, json={"gitVersion": "v1.test"})
        if self.live is not None:
            return httpx.Response(200, json=self.live)
        return httpx.Response(404, json={"kind": "Status"})


def job() -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "scientific",
            "namespace": "fs2-models",
            "labels": PROFILE_LABELS,
        },
        "spec": {"template": {"metadata": {"labels": PROFILE_LABELS}, "spec": {}}},
    }


def writer(tmp_path: Path, client: StubClient) -> ScientificWriter:
    token = tmp_path / "token"
    token.write_text("kubernetes-writer-token", encoding="utf-8")
    return ScientificWriter(
        api_url="https://kubernetes.default.svc",
        token_file=token,
        ca_file=tmp_path / "ca.crt",
        caller_username=CALLER,
        caller_audience="fs2-scientific-writer",
        allowed_namespaces=frozenset({"fs2-models"}),
        client=client,  # type: ignore[arg-type]
    )


def execution_policy() -> ScientificExecutionPolicy:
    return ScientificExecutionPolicy(
        {
            "schema": "fs2-serve.nebius.ai/scientific-execution-map/v3",
            "models": [
                {
                    "model_id": "esmfold2",
                    "variant_id": "default",
                    "workload_namespace": "fs2-models",
                    "stages": [
                        {
                            "stage_id": "prepare-input",
                            "image": "registry.example/scientific@sha256:" + "a" * 64,
                            "service_account_name": "scientific-runtime",
                            "workspace_uid": 1000,
                            "workspace_gid": 1000,
                            "mounts": [
                                {
                                    "name": "artifact-workspace",
                                    "kind": "artifact-workspace",
                                    "claim_name": None,
                                    "host_path": None,
                                    "mount_path": "/mnt/fs2-scientific",
                                    "sub_path": None,
                                    "read_only": False,
                                }
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": "1",
                                    "memory": "1Gi",
                                    "ephemeral_storage": "1Gi",
                                },
                                "limits": {
                                    "cpu": "2",
                                    "memory": "2Gi",
                                    "ephemeral_storage": "2Gi",
                                },
                            },
                            "termination_grace_seconds": 30,
                            "environment": {},
                            "required_node_labels": {},
                        }
                    ],
                }
            ],
        },
        tools_image="registry.example/tools@sha256:" + "b" * 64,
    )


def live_scientific_job() -> dict[str, Any]:
    labels = {
        **PROFILE_LABELS,
        "fs2-serve.nebius.ai/job-kind": "batch",
        "fs2.nebius.ai/operation-id": "10000000-0000-4000-8000-000000000001",
        "fs2.nebius.ai/workload-id": "10000000-0000-4000-8000-000000000002",
        "fs2.nebius.ai/attempt-id": "10000000-0000-4000-8000-000000000003",
        "fs2.nebius.ai/model-id": "esmfold2",
        "fs2.nebius.ai/variant-id": "default",
        "fs2.nebius.ai/stage-id": "prepare-input",
    }
    container_security = {
        "allowPrivilegeEscalation": False,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "capabilities": {"drop": ["ALL"]},
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "scientific",
            "namespace": "fs2-models",
            "uid": "uid-scientific",
            "resourceVersion": "123",
            "labels": labels,
            "annotations": {
                "fs2.nebius.ai/scientific-manifest-sha256": "c" * 64,
                "fs2.nebius.ai/scientific-controller-fence": "controller.example:7",
            },
        },
        "spec": {
            "activeDeadlineSeconds": 3600,
            "backoffLimit": 0,
            "suspend": True,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "scientific-runtime",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "hostNetwork": False,
                    "hostPID": False,
                    "hostIPC": False,
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "scientific-stage",
                            "image": "registry.example/scientific@sha256:" + "a" * 64,
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "/usr/local/bin/fs2-run-esmfold2",
                                "prepare-input",
                            ],
                            "workingDir": "/mnt/fs2-scientific/work/prepare-input/main",
                            "env": [],
                            "volumeMounts": [
                                {
                                    "name": "artifact-workspace",
                                    "mountPath": "/mnt/fs2-scientific",
                                    "readOnly": False,
                                }
                            ],
                            "resources": {
                                "requests": {
                                    "cpu": "1",
                                    "memory": "1Gi",
                                    "ephemeral-storage": "1Gi",
                                },
                                "limits": {
                                    "cpu": "2",
                                    "memory": "2Gi",
                                    "ephemeral-storage": "2Gi",
                                },
                            },
                            "securityContext": container_security,
                        },
                        {
                            "name": "artifact-collector",
                            "image": "registry.example/tools@sha256:" + "b" * 64,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["fs2-serve", "scientific-collect"],
                            "env": [],
                            "volumeMounts": [
                                {
                                    "name": "artifact-workspace",
                                    "mountPath": "/mnt/fs2-scientific",
                                    "readOnly": False,
                                }
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                            "securityContext": {
                                **container_security,
                                "readOnlyRootFilesystem": True,
                            },
                        },
                    ],
                    "initContainers": [
                        {
                            "name": "prepare-workspace",
                            "image": "registry.example/tools@sha256:" + "b" * 64,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["fs2-serve", "scientific-prepare-workspace"],
                            "env": [],
                            "volumeMounts": [
                                {
                                    "name": "artifact-workspace",
                                    "mountPath": "/mnt/fs2-scientific",
                                    "readOnly": False,
                                }
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                            "securityContext": {
                                **container_security,
                                "readOnlyRootFilesystem": True,
                            },
                        }
                    ],
                    "volumes": [{"name": "artifact-workspace", "emptyDir": {}}],
                    "nodeSelector": {},
                },
            }
        },
        "status": {"active": 1},
    }


def seal_submission(value: dict[str, Any]) -> dict[str, Any]:
    metadata = value["metadata"]
    canonical = copy.deepcopy(value)
    canonical["metadata"]["annotations"].pop(
        "fs2.nebius.ai/scientific-manifest-sha256"
    )
    canonical["metadata"]["annotations"].pop(
        "fs2.nebius.ai/scientific-controller-fence"
    )
    metadata["annotations"]["fs2.nebius.ai/scientific-manifest-sha256"] = (
        hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    return value


def submission_scientific_job() -> dict[str, Any]:
    value = live_scientific_job()
    value.pop("status", None)
    metadata = value["metadata"]
    metadata.pop("uid", None)
    metadata.pop("resourceVersion", None)
    return seal_submission(value)


def delete_ownership(value: dict[str, Any]) -> dict[str, str]:
    metadata = value["metadata"]
    labels = metadata["labels"]
    annotations = metadata["annotations"]
    return {
        "fs2.nebius.ai/operation-id": labels["fs2.nebius.ai/operation-id"],
        "fs2.nebius.ai/workload-id": labels["fs2.nebius.ai/workload-id"],
        "fs2.nebius.ai/attempt-id": labels["fs2.nebius.ai/attempt-id"],
        "fs2.nebius.ai/scientific-controller-fence": annotations[
            "fs2.nebius.ai/scientific-controller-fence"
        ],
        "fs2.nebius.ai/scientific-manifest-sha256": annotations[
            "fs2.nebius.ai/scientific-manifest-sha256"
        ],
    }


def token_review(
    *,
    groups: list[str] | None = None,
    extra: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": {
            "authenticated": True,
            "audiences": ["fs2-scientific-writer"],
            "user": {
                "username": CALLER,
                "groups": groups
                or [
                    "system:authenticated",
                    "system:serviceaccounts",
                    "system:serviceaccounts:fs2-system",
                ],
                "extra": extra or {},
            },
        }
    }


def test_manifest_accepts_only_internal_scientific_profile() -> None:
    validate_scientific_manifest(job(), resource="jobs")
    public = job()
    public["metadata"]["labels"] = {
        **PROFILE_LABELS,
        "fs2-serve.nebius.ai/network-workload-class": "public-acquisition",
        "fs2-serve.nebius.ai/network-profile": "job-public-acquisition-v1",
    }
    with pytest.raises(ScientificWriterError, match="internal scientific profile"):
        validate_scientific_manifest(public, resource="jobs")


@pytest.mark.asyncio
async def test_exact_tokenreview_identity_can_create_only_bounded_job(tmp_path: Path) -> None:
    client = StubClient(token_review())
    boundary = writer(tmp_path, client)
    boundary.execution_policy = execution_policy()
    await boundary.authorize("Bearer audience-scoped-caller-token")
    value = submission_scientific_job()
    response = await boundary.mutate(
        Mutation(
            method="POST",
            path="/apis/batch/v1/namespaces/fs2-models/jobs",
            body=value,
        )
    )
    assert response.status_code == 201
    assert client.mutations == [
        ("POST", "/apis/batch/v1/namespaces/fs2-models/jobs", value)
    ]


@pytest.mark.asyncio
async def test_create_rejects_resealed_arbitrary_command_and_secret_volume(
    tmp_path: Path,
) -> None:
    boundary = writer(tmp_path, StubClient(token_review()))
    boundary.execution_policy = execution_policy()

    arbitrary_command = submission_scientific_job()
    arbitrary_command["spec"]["template"]["spec"]["containers"][0][
        "command"
    ] = ["/bin/sh", "-c", "cat /input/*"]
    seal_submission(arbitrary_command)
    with pytest.raises(ScientificWriterError, match="reviewed adapter entrypoint"):
        await boundary.mutate(
            Mutation(
                method="POST",
                path="/apis/batch/v1/namespaces/fs2-models/jobs",
                body=arbitrary_command,
            )
        )

    secret_volume = submission_scientific_job()
    secret_volume["spec"]["template"]["spec"]["volumes"].append(
        {"name": "credentials", "secret": {"secretName": "unreviewed"}}
    )
    seal_submission(secret_volume)
    with pytest.raises(ScientificWriterError, match="execution map"):
        await boundary.mutate(
            Mutation(
                method="POST",
                path="/apis/batch/v1/namespaces/fs2-models/jobs",
                body=secret_volume,
            )
        )


@pytest.mark.asyncio
async def test_spoofed_group_or_extra_fails_closed(tmp_path: Path) -> None:
    for claim in (
        token_review(groups=["system:authenticated"]),
        token_review(extra={"unreviewed.example/claim": ["true"]}),
    ):
        with pytest.raises(ScientificWriterError, match="identity is not exact"):
            await writer(tmp_path, StubClient(claim)).authorize(
                "Bearer audience-scoped-caller-token"
            )


@pytest.mark.asyncio
async def test_writer_rejects_cross_namespace_and_unfenced_delete(tmp_path: Path) -> None:
    boundary = writer(tmp_path, StubClient(token_review()))
    with pytest.raises(ScientificWriterError, match="namespace is not authorized"):
        await boundary.mutate(
            Mutation(
                method="POST",
                path="/apis/batch/v1/namespaces/other/jobs",
                body=job(),
            )
        )
    with pytest.raises(ScientificWriterError, match="controller ownership"):
        await boundary.mutate(
            Mutation(
                method="DELETE",
                path="/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
                body={"apiVersion": "v1", "kind": "DeleteOptions"},
            )
        )


@pytest.mark.asyncio
async def test_delete_reads_and_revalidates_exact_live_internal_target(tmp_path: Path) -> None:
    live = live_scientific_job()
    client = StubClient(token_review(), live=live)
    boundary = writer(tmp_path, client)
    boundary.execution_policy = execution_policy()
    delete = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": "uid-scientific", "resourceVersion": "123"},
    }
    response = await boundary.mutate(
        Mutation(
            method="DELETE",
            path="/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
            body=delete,
            ownership=delete_ownership(live),
        )
    )
    assert response.status_code == 201
    assert client.mutations[-1] == (
        "DELETE",
        "/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
        delete,
    )

    live["metadata"]["labels"].update(
        {
            "fs2-serve.nebius.ai/network-workload-class": "public-acquisition",
            "fs2-serve.nebius.ai/network-profile": "job-public-acquisition-v1",
        }
    )
    with pytest.raises(ScientificWriterError, match="internal scientific profile"):
        await boundary.mutate(
            Mutation(
                method="DELETE",
                path="/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
                body=delete,
                ownership=delete_ownership(live),
            )
        )

    live["metadata"]["labels"].update(PROFILE_LABELS)
    wrong_owner = delete_ownership(live)
    wrong_owner["fs2.nebius.ai/workload-id"] = (
        "20000000-0000-4000-8000-000000000002"
    )
    with pytest.raises(ScientificWriterError, match="ownership fence"):
        await boundary.mutate(
            Mutation(
                method="DELETE",
                path="/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
                body=delete,
                ownership=wrong_owner,
            )
        )

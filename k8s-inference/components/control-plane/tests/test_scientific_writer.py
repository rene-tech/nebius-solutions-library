from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from fs2_serve.scientific_batch.writer_proxy import (
    Mutation,
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
    def __init__(self, token_review: dict[str, Any]) -> None:
        self.token_review = token_review
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
        assert path == "/version"
        return httpx.Response(200, json={"gitVersion": "v1.test"})


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
    await boundary.authorize("Bearer audience-scoped-caller-token")
    response = await boundary.mutate(
        Mutation(
            method="POST",
            path="/apis/batch/v1/namespaces/fs2-models/jobs",
            body=job(),
        )
    )
    assert response.status_code == 201
    assert client.mutations == [
        ("POST", "/apis/batch/v1/namespaces/fs2-models/jobs", job())
    ]


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
    with pytest.raises(ScientificWriterError, match="UID/resourceVersion"):
        await boundary.mutate(
            Mutation(
                method="DELETE",
                path="/apis/batch/v1/namespaces/fs2-models/jobs/scientific",
                body={"apiVersion": "v1", "kind": "DeleteOptions"},
            )
        )

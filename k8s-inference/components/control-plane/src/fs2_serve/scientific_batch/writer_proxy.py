"""Least-privilege Kubernetes writer for internal scientific Jobs and JobSets.

The public control-plane runtime authenticates with an audience-scoped projected
token. This process validates that token with TokenReview, accepts only the
finite internal scientific network profile, and performs the mutation with its
own dedicated ServiceAccount. Read/observe traffic never traverses this proxy.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

PROFILE_LABEL = "fs2-serve.nebius.ai/network-profile"
CLASS_LABEL = "fs2-serve.nebius.ai/network-workload-class"
PART_OF_LABEL = "app.kubernetes.io/part-of"
INTERNAL_PROFILE = "job-internal-v1"
INTERNAL_CLASS = "internal-job"
_PATH = re.compile(
    r"^/apis/(?P<group>batch|jobset\.x-k8s\.io)/(?P<version>v1|v1alpha2)/"
    r"namespaces/(?P<namespace>[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)/"
    r"(?P<resource>jobs|jobsets)(?:/(?P<name>[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?))?$"
)


class ScientificWriterError(RuntimeError):
    """A mutation cannot be proven to be a bounded scientific write."""


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(pattern=r"^(POST|DELETE)$")
    path: str = Field(min_length=1, max_length=1024)
    body: dict[str, Any]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScientificWriterError(f"{label} is missing or malformed")
    return value


def _profile(metadata: Mapping[str, Any], label: str) -> None:
    labels = _mapping(metadata.get("labels"), f"{label}.labels")
    if (
        labels.get(PART_OF_LABEL) != "fs2-serve"
        or labels.get(CLASS_LABEL) != INTERNAL_CLASS
        or labels.get(PROFILE_LABEL) != INTERNAL_PROFILE
    ):
        raise ScientificWriterError(f"{label} does not select the internal scientific profile")


def validate_scientific_manifest(value: Mapping[str, Any], *, resource: str) -> None:
    expected = ("batch/v1", "Job") if resource == "jobs" else ("jobset.x-k8s.io/v1alpha2", "JobSet")
    if (value.get("apiVersion"), value.get("kind")) != expected:
        raise ScientificWriterError("scientific mutation has the wrong API kind")
    _profile(_mapping(value.get("metadata"), "workload.metadata"), "workload.metadata")
    spec = _mapping(value.get("spec"), "workload.spec")
    if resource == "jobs":
        template = _mapping(spec.get("template"), "Job.spec.template")
        _profile(_mapping(template.get("metadata"), "Job Pod template metadata"), "Job Pod template")
        return
    replicated = spec.get("replicatedJobs")
    if not isinstance(replicated, list) or not replicated:
        raise ScientificWriterError("JobSet has no replicated Jobs")
    for index, raw in enumerate(replicated):
        job = _mapping(raw, f"JobSet replicatedJobs[{index}]")
        template = _mapping(job.get("template"), f"JobSet replicatedJobs[{index}].template")
        _profile(
            _mapping(template.get("metadata"), f"JobSet replicatedJobs[{index}] metadata"),
            f"JobSet replicatedJobs[{index}]",
        )
        pod = _mapping(_mapping(template.get("spec"), "Job template spec").get("template"), "Job Pod template")
        _profile(_mapping(pod.get("metadata"), "JobSet Pod template metadata"), "JobSet Pod template")


class ScientificWriter:
    def __init__(
        self,
        *,
        api_url: str,
        token_file: Path,
        ca_file: Path,
        caller_username: str,
        caller_audience: str,
        allowed_namespaces: frozenset[str],
        timeout_seconds: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self.caller_username = caller_username
        self.caller_audience = caller_audience
        self.allowed_namespaces = allowed_namespaces
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _api_headers(self) -> dict[str, str]:
        token = self.token_file.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise ScientificWriterError("scientific writer Kubernetes token is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def authorize(self, authorization: str | None) -> None:
        if authorization is None or not authorization.startswith("Bearer "):
            raise ScientificWriterError("scientific writer caller token is absent")
        caller_token = authorization.removeprefix("Bearer ").strip()
        if len(caller_token) < 16:
            raise ScientificWriterError("scientific writer caller token is malformed")
        response = await self.client.post(
            "/apis/authentication.k8s.io/v1/tokenreviews",
            headers=self._api_headers(),
            json={
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": caller_token, "audiences": [self.caller_audience]},
            },
        )
        if response.status_code != 201:
            raise ScientificWriterError("scientific writer TokenReview failed closed")
        try:
            token_review = response.json()
        except ValueError as exc:
            raise ScientificWriterError("scientific writer TokenReview returned malformed JSON") from exc
        status = _mapping(_mapping(token_review, "TokenReview").get("status"), "TokenReview.status")
        user = _mapping(status.get("user"), "TokenReview.status.user")
        groups = user.get("groups", [])
        extra = user.get("extra", {})
        allowed_extra_keys = {
            "authentication.kubernetes.io/credential-id",
            "authentication.kubernetes.io/node-name",
            "authentication.kubernetes.io/node-uid",
            "authentication.kubernetes.io/pod-name",
            "authentication.kubernetes.io/pod-uid",
        }
        expected_namespace = self.caller_username.split(":", 3)[2]
        if (
            status.get("authenticated") is not True
            or status.get("audiences") != [self.caller_audience]
            or user.get("username") != self.caller_username
            or not isinstance(groups, list)
            or frozenset(groups)
            != frozenset(
                {
                    "system:authenticated",
                    "system:serviceaccounts",
                    f"system:serviceaccounts:{expected_namespace}",
                }
            )
            or not isinstance(extra, Mapping)
            or not set(extra).issubset(allowed_extra_keys)
        ):
            raise ScientificWriterError("scientific writer caller identity is not exact")

    async def mutate(self, mutation: Mutation) -> httpx.Response:
        decoded = unquote(mutation.path)
        match = _PATH.fullmatch(decoded)
        if match is None or decoded != mutation.path:
            raise ScientificWriterError("scientific writer path is not canonical")
        values = match.groupdict()
        namespace = values["namespace"]
        resource = values["resource"]
        if namespace not in self.allowed_namespaces:
            raise ScientificWriterError("scientific writer namespace is not authorized")
        if resource == "jobs" and (values["group"], values["version"]) != ("batch", "v1"):
            raise ScientificWriterError("scientific Job API is not exact")
        if resource == "jobsets" and (values["group"], values["version"]) != (
            "jobset.x-k8s.io",
            "v1alpha2",
        ):
            raise ScientificWriterError("scientific JobSet API is not exact")
        if mutation.method == "POST":
            if values["name"] is not None:
                raise ScientificWriterError("scientific create must target a collection")
            validate_scientific_manifest(mutation.body, resource=resource)
            metadata = _mapping(mutation.body.get("metadata"), "workload.metadata")
            if metadata.get("namespace") != namespace:
                raise ScientificWriterError("scientific manifest namespace differs from its path")
        else:
            if values["name"] is None:
                raise ScientificWriterError("scientific delete must target one exact object")
            preconditions = _mapping(mutation.body.get("preconditions"), "DeleteOptions.preconditions")
            if (
                mutation.body.get("apiVersion") != "v1"
                or mutation.body.get("kind") != "DeleteOptions"
                or mutation.body.get("propagationPolicy") != "Foreground"
                or not isinstance(preconditions.get("uid"), str)
                or not isinstance(preconditions.get("resourceVersion"), str)
            ):
                raise ScientificWriterError("scientific delete lacks exact UID/resourceVersion fencing")
        return await self.client.request(
            mutation.method,
            mutation.path,
            headers=self._api_headers(),
            json=mutation.body,
        )

    async def ready(self) -> None:
        response = await self.client.get("/version", headers=self._api_headers())
        if response.status_code != 200:
            raise ScientificWriterError("scientific writer cannot reach Kubernetes")


def create_scientific_writer_app(writer: ScientificWriter) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await writer.ready()
        except (OSError, httpx.HTTPError, ScientificWriterError) as exc:
            return JSONResponse({"status": "not-ready", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post("/v1/mutate")
    async def mutate(
        mutation: Mutation,
        authorization: str | None = Header(default=None),
    ) -> Response:
        try:
            await writer.authorize(authorization)
            response = await writer.mutate(mutation)
        except (OSError, httpx.HTTPError, ScientificWriterError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type", "application/json"),
        )

    return app


__all__ = [
    "Mutation",
    "ScientificWriter",
    "ScientificWriterError",
    "create_scientific_writer_app",
    "validate_scientific_manifest",
]

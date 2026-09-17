"""Fail-closed Kubernetes admission server for NIM CRs and descendants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fs2_serve_catalog.loader import Catalog, CatalogError, load_catalog
from fs2_serve_catalog.workloads import validate_nim_operator_admission_review


CONFIG_SCHEMA = "fs2-serve.nebius.ai/nim-operator-admission-config/v1"
ENVELOPE_ANNOTATION = "fs2-serve.nebius.ai/operator-security-envelope-sha256"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("NIM admission configuration contains a duplicate key")
        value[key] = item
    return value


class NimAdmissionConfig:
    def __init__(self, value: object, *, catalog: Catalog) -> None:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "namespace",
            "security_session_id",
            "trusted_attestors",
            "entries",
        }:
            raise ValueError("NIM admission configuration fields differ")
        if value["schema"] != CONFIG_SCHEMA or value["namespace"] != "fs2-models":
            raise ValueError("NIM admission configuration identity differs")
        if not isinstance(value["security_session_id"], str):
            raise ValueError("NIM admission security session is absent")
        trusted = value["trusted_attestors"]
        entries = value["entries"]
        if not isinstance(trusted, Mapping) or not trusted or not isinstance(entries, list):
            raise ValueError("NIM admission trust or entries are absent")
        self.namespace = str(value["namespace"])
        self.security_session_id = str(value["security_session_id"])
        self.trusted_attestors = {str(key): str(item) for key, item in trusted.items()}
        self.entries: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self.entries_by_digest: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
        self.descendant_actors: set[str] = set()
        self.descendant_images: set[str] = set()
        self.descendant_service_accounts: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != {
                "resource_kind",
                "model_id",
                "security_envelope",
            }:
                raise ValueError("NIM admission entry fields differ")
            kind = entry["resource_kind"]
            model_id = entry["model_id"]
            envelope = entry["security_envelope"]
            if kind not in {"NIMCache", "NIMService"} or not isinstance(model_id, str):
                raise ValueError("NIM admission entry identity differs")
            if not isinstance(envelope, Mapping) or not isinstance(envelope.get("subject_sha256"), str):
                raise ValueError("NIM admission entry envelope is absent")
            record = catalog.model(model_id)
            subject = envelope.get("subject")
            if not isinstance(subject, Mapping):
                raise ValueError("NIM admission subject is absent")
            actor = subject.get("admission_actors")
            if not isinstance(actor, Mapping) or not isinstance(actor.get("descendant_pod"), str):
                raise ValueError("NIM admission descendant actor is absent")
            key = (str(kind), model_id, str(envelope["subject_sha256"]))
            if key in self.entries:
                raise ValueError("NIM admission entry is duplicated")
            # The exact cryptographic envelope is verified by every real
            # request below. Loading the catalog here also rejects unknown IDs.
            self.entries[key] = envelope
            digest = str(envelope["subject_sha256"])
            if digest in self.entries_by_digest:
                raise ValueError("NIM admission subject selector is ambiguous")
            self.entries_by_digest[digest] = (str(kind), model_id, envelope)
            self.descendant_actors.add(str(actor["descendant_pod"]))
            image = subject.get("descendant_image")
            pod_spec = subject.get("pod_spec")
            if not isinstance(image, str) or not isinstance(pod_spec, Mapping):
                raise ValueError("NIM admission descendant identity is absent")
            service_account = pod_spec.get("serviceAccountName")
            if not isinstance(service_account, str) or not service_account:
                raise ValueError("NIM admission descendant service account is absent")
            self.descendant_images.add(image)
            self.descendant_service_accounts.add(service_account)
            if record.to_dict()["runtime"]["kind"] != "nim":
                raise ValueError("NIM admission entry names a non-NIM model")

    @classmethod
    def load(cls, path: Path, *, catalog_dir: Path) -> "NimAdmissionConfig":
        raw = path.read_bytes()
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise ValueError("NIM admission configuration is empty or too large")
        return cls(
            json.loads(raw, object_pairs_hook=_unique_object),
            catalog=load_catalog(catalog_dir),
        )

    def select(
        self,
        review: Mapping[str, Any],
        *,
        catalog: Catalog,
    ) -> tuple[str, str, Mapping[str, Any]] | None:
        request = review.get("request")
        if not isinstance(request, Mapping):
            raise CatalogError("NIM admission request is absent")
        admitted = request.get("object")
        user = request.get("userInfo")
        if not isinstance(admitted, Mapping) or not isinstance(user, Mapping):
            raise CatalogError("NIM admission object or actor is absent")
        metadata = admitted.get("metadata")
        if not isinstance(metadata, Mapping):
            raise CatalogError("NIM admission metadata is absent")
        annotations = metadata.get("annotations", {})
        digest = annotations.get(ENVELOPE_ANNOTATION) if isinstance(annotations, Mapping) else None
        resource = request.get("resource")
        if not isinstance(resource, Mapping):
            raise CatalogError("NIM admission resource is absent")
        if resource.get("group") == "apps.nvidia.com" and resource.get("resource") in {
            "nimcaches",
            "nimservices",
        }:
            kind = "NIMCache" if resource["resource"] == "nimcaches" else "NIMService"
            model_id = metadata.get("name")
            if not isinstance(model_id, str) or not isinstance(digest, str):
                raise CatalogError("NIM custom resource lacks its signed envelope selector")
        elif resource.get("group") == "" and resource.get("resource") == "pods":
            owners = metadata.get("ownerReferences", [])
            nim_owners = [
                owner
                for owner in owners
                if isinstance(owner, Mapping) and owner.get("kind") in {"NIMCache", "NIMService"}
            ] if isinstance(owners, list) else []
            username = user.get("username")
            spec = admitted.get("spec")
            if not isinstance(spec, Mapping):
                raise CatalogError("NIM descendant Pod spec is absent")
            images = {
                container.get("image")
                for container_class in ("initContainers", "containers", "ephemeralContainers")
                for container in spec.get(container_class, [])
                if isinstance(container, Mapping) and isinstance(container.get("image"), str)
            }
            attributed = (
                bool(nim_owners)
                or username in self.descendant_actors
                or isinstance(digest, str)
                or bool(images.intersection(self.descendant_images))
                or spec.get("serviceAccountName") in self.descendant_service_accounts
            )
            if not attributed:
                return None
            if not isinstance(digest, str):
                raise CatalogError("NIM descendant lacks its signed envelope selector")
            selected = self.entries_by_digest.get(digest)
            if selected is None:
                raise CatalogError("NIM descendant envelope selector is not configured")
            kind, model_id, envelope = selected
            catalog.model(model_id)
            return kind, model_id, envelope
        else:
            raise CatalogError("NIM admission request targets an unsupported resource")
        key = (kind, model_id, digest)
        envelope = self.entries.get(key)
        if envelope is None:
            raise CatalogError("NIM admission envelope selector is not configured")
        catalog.model(model_id)
        return kind, model_id, envelope


def create_nim_admission_app(*, config: NimAdmissionConfig, catalog: Catalog) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/livez", include_in_schema=False)
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.post("/admit", include_in_schema=False)
    async def admit(request: Request) -> JSONResponse:
        review: object = await request.json()
        uid = ""
        if isinstance(review, Mapping) and isinstance(review.get("request"), Mapping):
            candidate = review["request"].get("uid")
            uid = candidate if isinstance(candidate, str) else ""
        try:
            if not isinstance(review, Mapping):
                raise CatalogError("NIM admission review is not an object")
            selected = config.select(review, catalog=catalog)
            if selected is None:
                return JSONResponse(
                    {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}}
                )
            kind, model_id, envelope = selected
            response = validate_nim_operator_admission_review(
                review,
                security_envelope=envelope,
                trusted_attestors=config.trusted_attestors,
                security_session_id=config.security_session_id,
                resource_kind=kind,
                record=catalog.model(model_id),
            )
            return JSONResponse(response)
        except (CatalogError, ValueError, KeyError, TypeError):
            return JSONResponse(
                {
                    "apiVersion": "admission.k8s.io/v1",
                    "kind": "AdmissionReview",
                    "response": {
                        "uid": uid,
                        "allowed": False,
                        "status": {"code": 403, "reason": "Forbidden", "message": "NIM workload differs from its signed admission contract"},
                    },
                }
            )

    return app


async def serve_nim_admission(
    *,
    config_file: Path,
    catalog_dir: Path,
    tls_certificate_file: Path,
    tls_private_key_file: Path,
    port: int,
    log_level: str,
) -> None:
    catalog = load_catalog(catalog_dir)
    config = NimAdmissionConfig.load(config_file, catalog_dir=catalog_dir)
    server = uvicorn.Server(
        uvicorn.Config(
            create_nim_admission_app(config=config, catalog=catalog),
            host="0.0.0.0",  # noqa: S104 - cluster-internal TLS Service only
            port=port,
            ssl_certfile=str(tls_certificate_file),
            ssl_keyfile=str(tls_private_key_file),
            log_level=log_level.lower(),
        )
    )
    await server.serve()

"""Fail-closed Kubernetes admission server for NIM CRs and descendants."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.loader import Catalog, CatalogError, load_catalog
from fs2_serve_catalog.workloads import (
    validate_nim_operator_admission_review,
    validate_persisted_nim_operator_root,
)


CONFIG_SCHEMA = "fs2-serve.nebius.ai/nim-operator-admission-config/v2"
ENVELOPE_ANNOTATION = "fs2-serve.nebius.ai/operator-security-envelope-sha256"
ROOT_ENROLLMENT_LABEL = "fs2-serve.nebius.ai/nim-root-enrollment"
_RESOURCE_PATHS = {
    ("apps.nvidia.com/v1alpha1", "NIMCache"): "/apis/apps.nvidia.com/v1alpha1/namespaces/{namespace}/nimcaches/{name}",
    ("apps.nvidia.com/v1alpha1", "NIMService"): "/apis/apps.nvidia.com/v1alpha1/namespaces/{namespace}/nimservices/{name}",
    ("apps/v1", "Deployment"): "/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
    ("apps/v1", "ReplicaSet"): "/apis/apps/v1/namespaces/{namespace}/replicasets/{name}",
    ("apps/v1", "StatefulSet"): "/apis/apps/v1/namespaces/{namespace}/statefulsets/{name}",
    ("batch/v1", "Job"): "/apis/batch/v1/namespaces/{namespace}/jobs/{name}",
    ("v1", "Pod"): "/api/v1/namespaces/{namespace}/pods/{name}",
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("NIM admission configuration contains a duplicate key")
        value[key] = item
    return value


def _root_enrollment_name(*, subject_sha256: str, root_uid: str) -> str:
    """Return a generation-addressed name while retaining old immutable records."""

    if (
        re.fullmatch(r"[0-9a-f]{64}", subject_sha256) is None
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            root_uid,
        )
        is None
    ):
        raise CatalogError("NIM root enrollment generation identity is invalid")
    return f"fs2-nim-root-{subject_sha256[:12]}-{root_uid}"


class AmbiguousNimDescendant(CatalogError):
    """A selector-less descendant needs its persisted NIM root to disambiguate."""


class NimAdmissionConfig:
    def __init__(self, value: object, *, catalog: Catalog) -> None:
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "namespace",
            "security_session_id",
            "trusted_attestors",
            "admission_policy",
            "admission_policy_sha256",
            "entries",
        }:
            raise ValueError("NIM admission configuration fields differ")
        if value["schema"] != CONFIG_SCHEMA or value["namespace"] != "fs2-models":
            raise ValueError("NIM admission configuration identity differs")
        if not isinstance(value["security_session_id"], str):
            raise ValueError("NIM admission security session is absent")
        trusted = value["trusted_attestors"]
        entries = value["entries"]
        policy_sha256 = value["admission_policy_sha256"]
        policy = value["admission_policy"]
        if (
            not isinstance(trusted, Mapping)
            or not trusted
            or not isinstance(policy_sha256, str)
            or len(policy_sha256) != 64
            or any(character not in "0123456789abcdef" for character in policy_sha256)
            or not isinstance(policy, Mapping)
            or hashlib.sha256(canonical_bytes(policy)).hexdigest() != policy_sha256
            or not isinstance(entries, list)
        ):
            raise ValueError("NIM admission trust or entries are absent")
        if policy.get("schema") != "fs2-serve.nebius.ai/nim-admission-policy/v5" or set(policy) != {
            "schema",
            "name",
            "namespace",
            "failure_policy",
            "match_policy",
            "side_effects",
            "timeout_seconds",
            "admission_review_versions",
            "operations",
            "resources",
            "service",
            "ca_bundle_sha256",
            "owner_resolution",
            "root_enrollment",
            "security_boundary",
        } or policy.get("root_enrollment") != {
            "namespace": "fs2-system",
            "name_prefix": "fs2-nim-root-",
            "storage_kind": "immutable-configmap-create-once",
            "reconciler": "persisted-root-readback",
            "reconcile_interval_seconds": 2,
            "admission_behavior": "verify-existing-deny-until-enrolled",
        }:
            raise ValueError("NIM admission root enrollment policy differs")
        self.namespace = str(value["namespace"])
        self.security_session_id = str(value["security_session_id"])
        self.trusted_attestors = {str(key): str(item) for key, item in trusted.items()}
        self.admission_policy_sha256 = policy_sha256
        self.admission_policy = dict(policy)
        self.entries: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        self.entries_by_digest: dict[str, tuple[str, str, Mapping[str, Any]]] = {}
        self.entries_by_root: dict[tuple[str, str], Mapping[str, Any]] = {}
        self.descendant_candidates: list[
            tuple[str, str, Mapping[str, Any], frozenset[str], frozenset[str], str]
        ] = []
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
            actors = subject.get("actor_identities")
            descendants = subject.get("descendant_resources")
            if (
                not isinstance(actors, Mapping)
                or not isinstance(descendants, Mapping)
                or subject.get("admission_policy_sha256") != self.admission_policy_sha256
            ):
                raise ValueError("NIM admission actor/resource or policy binding is absent")
            key = (str(kind), model_id, str(envelope["subject_sha256"]))
            if key in self.entries:
                raise ValueError("NIM admission entry is duplicated")
            # The exact cryptographic envelope is verified by every real
            # request below. Loading the catalog here also rejects unknown IDs.
            self.entries[key] = envelope
            root_key = (str(kind), model_id)
            if root_key in self.entries_by_root:
                raise ValueError("NIM admission root identity is duplicated")
            self.entries_by_root[root_key] = envelope
            digest = str(envelope["subject_sha256"])
            if digest in self.entries_by_digest:
                raise ValueError("NIM admission subject selector is ambiguous")
            self.entries_by_digest[digest] = (str(kind), model_id, envelope)
            descendant_actor_keys = {
                str(contract["actor_identity"])
                for contract in descendants.values()
                if isinstance(contract, Mapping)
                and isinstance(contract.get("actor_identity"), str)
            }
            for actor_key in descendant_actor_keys:
                identity = actors.get(actor_key)
                if (
                    isinstance(identity, Mapping)
                    and identity.get("kind") == "pod-bound-service-account"
                    and isinstance(identity.get("username"), str)
                ):
                    self.descendant_actors.add(str(identity["username"]))
            image = subject.get("descendant_image")
            pod_spec = subject.get("pod_spec")
            if not isinstance(image, str) or not isinstance(pod_spec, Mapping):
                raise ValueError("NIM admission descendant identity is absent")
            service_account = pod_spec.get("serviceAccountName")
            if not isinstance(service_account, str) or not service_account:
                raise ValueError("NIM admission descendant service account is absent")
            self.descendant_images.add(image)
            self.descendant_service_accounts.add(service_account)
            signed_images = frozenset(
                str(contract["image"])
                for contract in subject.get("containers", {}).values()
                if isinstance(contract, Mapping)
                and isinstance(contract.get("image"), str)
            )
            actor_usernames = frozenset(
                str(identity["username"])
                for actor_key, identity in actors.items()
                if actor_key in descendant_actor_keys
                if isinstance(identity, Mapping)
                and identity.get("kind") == "pod-bound-service-account"
                and isinstance(identity.get("username"), str)
            )
            self.descendant_candidates.append(
                (str(kind), model_id, envelope, signed_images, actor_usernames, service_account)
            )
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
        root_kind_hint: str | None = None,
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
        elif (resource.get("group"), resource.get("version"), resource.get("resource")) in {
            ("", "v1", "pods"),
            ("apps", "v1", "deployments"),
            ("apps", "v1", "replicasets"),
            ("apps", "v1", "statefulsets"),
            ("batch", "v1", "jobs"),
        }:
            username = user.get("username")
            spec = admitted.get("spec")
            if not isinstance(spec, Mapping):
                raise CatalogError("NIM descendant spec is absent")
            template = spec.get("template")
            pod_spec = (
                template.get("spec")
                if isinstance(template, Mapping) and isinstance(template.get("spec"), Mapping)
                else spec
            )
            template_metadata = (
                template.get("metadata") if isinstance(template, Mapping) else None
            )
            template_annotations = (
                template_metadata.get("annotations", {})
                if isinstance(template_metadata, Mapping)
                else {}
            )
            template_digest = (
                template_annotations.get(ENVELOPE_ANNOTATION)
                if isinstance(template_annotations, Mapping)
                else None
            )
            if digest is not None and template_digest is not None and digest != template_digest:
                raise CatalogError("NIM descendant object and Pod-template selectors differ")
            digest = digest or template_digest
            images = {
                container.get("image")
                for container_class in ("initContainers", "containers", "ephemeralContainers")
                for container in pod_spec.get(container_class, [])
                if isinstance(container, Mapping) and isinstance(container.get("image"), str)
            }
            attributed = (
                isinstance(digest, str)
                or bool(images.intersection(self.descendant_images))
                or pod_spec.get("serviceAccountName") in self.descendant_service_accounts
                or username in self.descendant_actors
            )
            if not attributed:
                return None
            if isinstance(digest, str):
                selected = self.entries_by_digest.get(digest)
                if selected is None:
                    raise CatalogError("NIM descendant envelope selector is not configured")
            else:
                candidates = [
                    (kind, candidate_model_id, candidate_envelope)
                    for (
                        kind,
                        candidate_model_id,
                        candidate_envelope,
                        signed_images,
                        actor_usernames,
                        service_account,
                    ) in self.descendant_candidates
                    if bool(images.intersection(signed_images))
                    or username in actor_usernames
                    or pod_spec.get("serviceAccountName") == service_account
                    if root_kind_hint is None or kind == root_kind_hint
                ]
                if len(candidates) != 1:
                    if len(candidates) > 1 and root_kind_hint is None:
                        raise AmbiguousNimDescendant(
                            "NIM descendant needs its persisted root for attribution"
                        )
                    raise CatalogError(
                        "NIM descendant without a propagated selector is not uniquely attributable"
                    )
                selected = candidates[0]
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

    def select_persisted_root(
        self, root: Mapping[str, Any], *, catalog: Catalog
    ) -> tuple[str, str, Mapping[str, Any]]:
        """Select authority only from a live, UID-resolved NIM root."""

        kind = root.get("kind")
        metadata = root.get("metadata")
        model_id = metadata.get("name") if isinstance(metadata, Mapping) else None
        if (
            kind not in {"NIMCache", "NIMService"}
            or not isinstance(model_id, str)
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace") != self.namespace
        ):
            raise CatalogError("persisted NIM root identity is incomplete")
        envelope = self.entries_by_root.get((str(kind), model_id))
        if envelope is None:
            raise CatalogError("persisted NIM root has no signed admission envelope")
        catalog.model(model_id)
        return str(kind), model_id, envelope


class KubernetesNimOwnerResolver:
    """Resolve every dynamic UID edge against persisted Kubernetes objects."""

    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        ca_file: Path,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_file = token_file
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            verify=str(ca_file),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CatalogError("NIM owner resolver credential is unavailable") from error
        if len(token) < 16:
            raise CatalogError("NIM owner resolver credential is unavailable")
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    async def get(
        self,
        *,
        api_version: str,
        kind: str,
        namespace: str,
        name: str,
        expected_uid: str,
    ) -> Mapping[str, Any]:
        pattern = _RESOURCE_PATHS.get((api_version, kind))
        if pattern is None:
            raise CatalogError("NIM owner resolver received an unsupported GVK")
        path = pattern.format(
            namespace=quote(namespace, safe=""),
            name=quote(name, safe=""),
        )
        try:
            response = await self.client.get(path, headers=self._headers())
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM owner resolver Kubernetes request failed") from error
        if response.status_code != 200 or len(response.content) > 4 * 1024 * 1024:
            raise CatalogError("NIM owner resolver could not bind the persisted object")
        try:
            value = response.json()
        except ValueError as error:
            raise CatalogError("NIM owner resolver returned invalid JSON") from error
        metadata = value.get("metadata") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or value.get("apiVersion") != api_version
            or value.get("kind") != kind
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace") != namespace
            or metadata.get("name") != name
            or metadata.get("uid") != expected_uid
        ):
            raise CatalogError("NIM owner resolver object identity differs")
        return value

    async def verify_admission_policy(self, policy: Mapping[str, Any]) -> None:
        name = policy.get("name")
        if not isinstance(name, str) or not name:
            raise CatalogError("NIM admission policy name is absent")
        try:
            response = await self.client.get(
                "/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/"
                + quote(name, safe=""),
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM admission policy lookup failed") from error
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM admission policy is not installed")
        try:
            value = response.json()
        except ValueError as error:
            raise CatalogError("NIM admission policy lookup returned invalid JSON") from error
        webhooks = value.get("webhooks") if isinstance(value, Mapping) else None
        if not isinstance(webhooks, list) or len(webhooks) != 1 or not isinstance(webhooks[0], Mapping):
            raise CatalogError("NIM admission policy webhook inventory differs")
        webhook = webhooks[0]
        client_config = webhook.get("clientConfig")
        service = client_config.get("service") if isinstance(client_config, Mapping) else None
        namespace_selector = webhook.get("namespaceSelector")
        labels = (
            namespace_selector.get("matchLabels")
            if isinstance(namespace_selector, Mapping)
            else None
        )
        rules = webhook.get("rules")
        if not isinstance(rules, list):
            raise CatalogError("NIM admission policy rules are absent")
        resources: list[str] = []
        operations: set[str] = set()
        for rule in rules:
            if not isinstance(rule, Mapping) or rule.get("scope") != "Namespaced":
                raise CatalogError("NIM admission policy rule scope differs")
            groups = rule.get("apiGroups")
            versions = rule.get("apiVersions")
            names = rule.get("resources")
            rule_operations = rule.get("operations")
            if (
                not isinstance(groups, list)
                or len(groups) != 1
                or not isinstance(versions, list)
                or len(versions) != 1
                or not isinstance(names, list)
                or not isinstance(rule_operations, list)
            ):
                raise CatalogError("NIM admission policy rule shape differs")
            operations.update(str(operation) for operation in rule_operations)
            prefix = f"{groups[0]}/{versions[0]}" if groups[0] else str(versions[0])
            resources.extend(f"{prefix}/{name}" for name in names)
        ca_bundle = client_config.get("caBundle") if isinstance(client_config, Mapping) else None
        try:
            ca_sha256 = hashlib.sha256(base64.b64decode(ca_bundle, validate=True)).hexdigest()
        except (TypeError, ValueError, binascii.Error) as error:
            raise CatalogError("NIM admission policy CA bundle is invalid") from error
        security_boundary = policy.get("security_boundary")
        if not isinstance(security_boundary, Mapping) or set(security_boundary) != {
            "name", "policy_uid", "policy_resource_version", "binding_uid",
            "binding_resource_version", "subject_sha256"
        }:
            raise CatalogError("NIM external security boundary identity is absent")
        for resource, uid_field, version_field in (
            ("validatingadmissionpolicies", "policy_uid", "policy_resource_version"),
            ("validatingadmissionpolicybindings", "binding_uid", "binding_resource_version"),
        ):
            try:
                boundary_response = await self.client.get(
                    "/apis/admissionregistration.k8s.io/v1/"
                    + resource
                    + "/"
                    + quote(str(security_boundary["name"]), safe=""),
                    headers=self._headers(),
                )
            except (OSError, httpx.HTTPError) as error:
                raise CatalogError("NIM external security boundary lookup failed") from error
            if boundary_response.status_code != 200 or len(boundary_response.content) > 1024 * 1024:
                raise CatalogError("NIM external security boundary is not installed")
            try:
                boundary = boundary_response.json()
            except ValueError as error:
                raise CatalogError("NIM external security boundary response is invalid") from error
            boundary_metadata = boundary.get("metadata") if isinstance(boundary, Mapping) else None
            boundary_labels = boundary_metadata.get("labels") if isinstance(boundary_metadata, Mapping) else None
            if (
                not isinstance(boundary_metadata, Mapping)
                or boundary_metadata.get("name") != security_boundary["name"]
                or boundary_metadata.get("uid") != security_boundary[uid_field]
                or boundary_metadata.get("resourceVersion") != security_boundary[version_field]
                or not isinstance(boundary_labels, Mapping)
                or boundary_labels.get("fs2-serve.nebius.ai/immutable-security-boundary") != "true"
            ):
                raise CatalogError("NIM external security boundary live identity differs")
        observed = {
            "schema": "fs2-serve.nebius.ai/nim-admission-policy/v5",
            "name": value.get("metadata", {}).get("name") if isinstance(value.get("metadata"), Mapping) else None,
            "namespace": labels.get("kubernetes.io/metadata.name") if isinstance(labels, Mapping) else None,
            "failure_policy": webhook.get("failurePolicy"),
            "match_policy": webhook.get("matchPolicy"),
            "side_effects": webhook.get("sideEffects"),
            "timeout_seconds": webhook.get("timeoutSeconds"),
            "admission_review_versions": webhook.get("admissionReviewVersions"),
            "operations": sorted(operations),
            "resources": sorted(resources),
            "service": {
                "namespace": service.get("namespace") if isinstance(service, Mapping) else None,
                "name": service.get("name") if isinstance(service, Mapping) else None,
                "path": service.get("path") if isinstance(service, Mapping) else None,
                "port": service.get("port") if isinstance(service, Mapping) else None,
            },
            "ca_bundle_sha256": ca_sha256,
            "owner_resolution": "live-read-through-exact-uid-chain",
            "root_enrollment": {
                "namespace": "fs2-system",
                "name_prefix": "fs2-nim-root-",
                "storage_kind": "immutable-configmap-create-once",
                "reconciler": "persisted-root-readback",
                "reconcile_interval_seconds": 2,
                "admission_behavior": "verify-existing-deny-until-enrolled",
            },
            "security_boundary": dict(security_boundary),
        }
        expected = dict(policy)
        expected["operations"] = sorted(expected.get("operations", []))
        expected["resources"] = sorted(expected.get("resources", []))
        if observed != expected:
            raise CatalogError("installed NIM admission policy differs from its signed digest")

    async def enroll_root(
        self,
        root: Mapping[str, Any],
        *,
        model_id: str,
        subject_sha256: str,
        policy: Mapping[str, Any],
    ) -> None:
        """Atomically bind one signed envelope to the first persisted root UID."""

        metadata = root.get("metadata")
        enrollment = policy.get("root_enrollment")
        if (
            root.get("apiVersion") != "apps.nvidia.com/v1alpha1"
            or root.get("kind") not in {"NIMCache", "NIMService"}
            or not isinstance(metadata, Mapping)
            or metadata.get("namespace", "fs2-models") != "fs2-models"
            or metadata.get("name") != model_id
            or not isinstance(metadata.get("uid"), str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                metadata["uid"],
            )
            is None
            or not isinstance(enrollment, Mapping)
            or enrollment != {
                "namespace": "fs2-system",
                "name_prefix": "fs2-nim-root-",
                "storage_kind": "immutable-configmap-create-once",
                "reconciler": "persisted-root-readback",
                "reconcile_interval_seconds": 2,
                "admission_behavior": "verify-existing-deny-until-enrolled",
            }
            or len(subject_sha256) != 64
            or any(character not in "0123456789abcdef" for character in subject_sha256)
        ):
            raise CatalogError("NIM root enrollment identity is incomplete")
        name = _root_enrollment_name(
            subject_sha256=subject_sha256,
            root_uid=str(metadata["uid"]),
        )
        namespace = str(enrollment["namespace"])
        expected_data = {
            "schema": "fs2-serve.nebius.ai/nim-root-enrollment/v1",
            "resource_kind": str(root["kind"]),
            "model_id": model_id,
            "root_uid": str(metadata["uid"]),
            "subject_sha256": subject_sha256,
        }
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {ROOT_ENROLLMENT_LABEL: "true"},
            },
            "immutable": True,
            "data": expected_data,
        }
        try:
            response = await self.client.post(
                f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=canonical_bytes(body),
            )
            if response.status_code == 409:
                response = await self.client.get(
                    f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}",
                    headers=self._headers(),
                )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM root enrollment request failed") from error
        if response.status_code not in {200, 201} or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM root enrollment could not be committed")
        try:
            persisted = response.json()
        except ValueError as error:
            raise CatalogError("NIM root enrollment response is invalid") from error
        persisted_metadata = persisted.get("metadata") if isinstance(persisted, Mapping) else None
        if (
            not isinstance(persisted, Mapping)
            or persisted.get("apiVersion") != "v1"
            or persisted.get("kind") != "ConfigMap"
            or persisted.get("immutable") is not True
            or persisted.get("data") != expected_data
            or not isinstance(persisted_metadata, Mapping)
            or persisted_metadata.get("name") != name
            or persisted_metadata.get("namespace") != namespace
            or persisted_metadata.get("labels") != {ROOT_ENROLLMENT_LABEL: "true"}
        ):
            raise CatalogError("persisted NIM root enrollment differs")

    async def verify_root_enrollment(
        self,
        root: Mapping[str, Any],
        *,
        model_id: str,
        subject_sha256: str,
        policy: Mapping[str, Any],
    ) -> None:
        """Require the reconciler's immutable UID binding without mutating admission state."""

        enrollment = policy.get("root_enrollment")
        metadata = root.get("metadata")
        if not isinstance(enrollment, Mapping) or not isinstance(metadata, Mapping):
            raise CatalogError("NIM root enrollment policy or identity is absent")
        name = _root_enrollment_name(
            subject_sha256=subject_sha256,
            root_uid=str(metadata.get("uid", "")),
        )
        namespace = str(enrollment.get("namespace", ""))
        expected_data = {
            "schema": "fs2-serve.nebius.ai/nim-root-enrollment/v1",
            "resource_kind": str(root.get("kind", "")),
            "model_id": model_id,
            "root_uid": str(metadata.get("uid", "")),
            "subject_sha256": subject_sha256,
        }
        try:
            response = await self.client.get(
                f"/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(name, safe='')}",
                headers=self._headers(),
            )
        except (OSError, httpx.HTTPError) as error:
            raise CatalogError("NIM root enrollment lookup failed") from error
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise CatalogError("NIM root is not atomically enrolled yet")
        try:
            persisted = response.json()
        except ValueError as error:
            raise CatalogError("NIM root enrollment response is invalid") from error
        persisted_metadata = persisted.get("metadata") if isinstance(persisted, Mapping) else None
        if (
            not isinstance(persisted, Mapping)
            or persisted.get("apiVersion") != "v1"
            or persisted.get("kind") != "ConfigMap"
            or persisted.get("immutable") is not True
            or persisted.get("data") != expected_data
            or not isinstance(persisted_metadata, Mapping)
            or persisted_metadata.get("name") != name
            or persisted_metadata.get("namespace") != namespace
            or persisted_metadata.get("labels") != {ROOT_ENROLLMENT_LABEL: "true"}
        ):
            raise CatalogError("persisted NIM root enrollment differs")

    async def _list_roots(self, *, kind: str, namespace: str) -> list[Mapping[str, Any]]:
        resource = {"NIMCache": "nimcaches", "NIMService": "nimservices"}.get(kind)
        if resource is None:
            raise CatalogError("NIM root reconciler received an unsupported kind")
        path = (
            "/apis/apps.nvidia.com/v1alpha1/namespaces/"
            f"{quote(namespace, safe='')}/{resource}"
        )
        roots: list[Mapping[str, Any]] = []
        continuation: str | None = None
        for _ in range(16):
            params = {"limit": "500"}
            if continuation is not None:
                params["continue"] = continuation
            try:
                response = await self.client.get(path, params=params, headers=self._headers())
            except (OSError, httpx.HTTPError) as error:
                raise CatalogError("NIM root inventory lookup failed") from error
            if response.status_code != 200 or len(response.content) > 16 * 1024 * 1024:
                raise CatalogError("NIM root inventory is unavailable")
            try:
                listing = response.json()
            except ValueError as error:
                raise CatalogError("NIM root inventory returned invalid JSON") from error
            items = listing.get("items") if isinstance(listing, Mapping) else None
            metadata = listing.get("metadata") if isinstance(listing, Mapping) else None
            if not isinstance(items, list) or not isinstance(metadata, Mapping):
                raise CatalogError("NIM root inventory shape differs")
            if any(not isinstance(item, Mapping) for item in items):
                raise CatalogError("NIM root inventory contains a non-object")
            roots.extend(items)
            next_token = metadata.get("continue", "")
            if not isinstance(next_token, str):
                raise CatalogError("NIM root inventory continuation token differs")
            if not next_token:
                return roots
            continuation = next_token
        raise CatalogError("NIM root inventory exceeds its bounded pagination")

    async def reconcile_root_enrollments(
        self,
        *,
        config: "NimAdmissionConfig",
        catalog: Catalog,
    ) -> None:
        """Read persisted roots, validate signed bytes, then create UID bindings once."""

        await self.verify_admission_policy(config.admission_policy)
        expected = {
            (kind, model_id): envelope
            for kind, model_id, envelope in config.entries_by_digest.values()
        }
        for kind in ("NIMCache", "NIMService"):
            for root in await self._list_roots(kind=kind, namespace=config.namespace):
                metadata = root.get("metadata")
                model_id = metadata.get("name") if isinstance(metadata, Mapping) else None
                envelope = expected.get((kind, str(model_id)))
                if envelope is None:
                    raise CatalogError("persisted NIM root is outside the signed inventory")
                subject_sha256 = validate_persisted_nim_operator_root(
                    root,
                    security_envelope=envelope,
                    trusted_attestors=config.trusted_attestors,
                    security_session_id=config.security_session_id,
                    resource_kind=kind,
                    record=catalog.model(str(model_id)),
                )
                await self.enroll_root(
                    root,
                    model_id=str(model_id),
                    subject_sha256=subject_sha256,
                    policy=config.admission_policy,
                )

    async def owner_chain(
        self,
        value: Mapping[str, Any],
        *,
        root_kind: str,
        namespace: str,
    ) -> list[Mapping[str, Any]]:
        chain: list[Mapping[str, Any]] = []
        child = value
        for _ in range(4):
            metadata = child.get("metadata")
            references = metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
            if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], Mapping):
                raise CatalogError("NIM descendant lacks one persisted owner edge")
            reference = references[0]
            owner = await self.get(
                api_version=str(reference.get("apiVersion", "")),
                kind=str(reference.get("kind", "")),
                namespace=namespace,
                name=str(reference.get("name", "")),
                expected_uid=str(reference.get("uid", "")),
            )
            chain.append(owner)
            if owner.get("kind") == root_kind:
                return chain
            child = owner
        raise CatalogError("NIM owner graph exceeds its signed depth")

    async def owner_chain_to_nim(
        self,
        value: Mapping[str, Any],
        *,
        namespace: str,
    ) -> list[Mapping[str, Any]] | None:
        """Classify an owner chain from persisted UID edges, never Pod hints.

        ``None`` means the exact live chain terminated at an ownerless non-NIM
        object. Missing, multiple, malformed, or overlong edges are not a
        negative classification and fail closed.
        """

        chain: list[Mapping[str, Any]] = []
        child = value
        for _ in range(4):
            metadata = child.get("metadata")
            references = (
                metadata.get("ownerReferences") if isinstance(metadata, Mapping) else None
            )
            if references is None or references == []:
                return None
            if not isinstance(references, list) or len(references) != 1 or not isinstance(references[0], Mapping):
                raise CatalogError("NIM descendant ownership is not conclusively attributable")
            reference = references[0]
            owner = await self.get(
                api_version=str(reference.get("apiVersion", "")),
                kind=str(reference.get("kind", "")),
                namespace=namespace,
                name=str(reference.get("name", "")),
                expected_uid=str(reference.get("uid", "")),
            )
            chain.append(owner)
            if owner.get("kind") in {"NIMCache", "NIMService"}:
                return chain
            child = owner
        raise CatalogError("NIM descendant owner graph exceeds its bounded classification depth")

    async def actor_chain(
        self,
        user_info: Mapping[str, Any],
        identity: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        if identity.get("kind") == "kubernetes-control-plane":
            return []
        extra = user_info.get("extra")
        names = extra.get("authentication.kubernetes.io/pod-name") if isinstance(extra, Mapping) else None
        uids = extra.get("authentication.kubernetes.io/pod-uid") if isinstance(extra, Mapping) else None
        if (
            not isinstance(names, list)
            or len(names) != 1
            or not isinstance(names[0], str)
            or not isinstance(uids, list)
            or len(uids) != 1
            or not isinstance(uids[0], str)
        ):
            raise CatalogError("NIM actor token lacks Pod-bound authentication extras")
        pod = await self.get(
            api_version="v1",
            kind="Pod",
            namespace=str(identity["namespace"]),
            name=names[0],
            expected_uid=uids[0],
        )
        owners = await self.owner_chain(
            pod,
            root_kind="Deployment",
            namespace=str(identity["namespace"]),
        )
        if len(owners) != 2:
            raise CatalogError("NIM actor Pod does not resolve through ReplicaSet to Deployment")
        return [pod, *owners]


def create_nim_admission_app(
    *,
    config: NimAdmissionConfig,
    catalog: Catalog,
    resolver: KubernetesNimOwnerResolver,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root_enrollment_ready = False
    reconcile_stop = asyncio.Event()
    reconcile_task: asyncio.Task[None] | None = None

    async def reconcile_once() -> None:
        try:
            await resolver.reconcile_root_enrollments(config=config, catalog=catalog)
        except (CatalogError, OSError, ValueError, KeyError, TypeError):
            app.state.root_enrollment_ready = False
        else:
            app.state.root_enrollment_ready = True

    async def reconcile_forever() -> None:
        interval = int(config.admission_policy["root_enrollment"]["reconcile_interval_seconds"])
        while not reconcile_stop.is_set():
            await reconcile_once()
            try:
                await asyncio.wait_for(reconcile_stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    @app.on_event("startup")
    async def start_root_enrollment_reconciler() -> None:
        nonlocal reconcile_task
        await reconcile_once()
        reconcile_task = asyncio.create_task(
            reconcile_forever(), name="nim-root-enrollment-reconciler"
        )

    @app.on_event("shutdown")
    async def stop_root_enrollment_reconciler() -> None:
        reconcile_stop.set()
        if reconcile_task is not None:
            await reconcile_task

    @app.get("/livez", include_in_schema=False)
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    async def ready() -> JSONResponse:
        if app.state.root_enrollment_ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "root-enrollment-unavailable"}, status_code=503)

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
            if not app.state.root_enrollment_ready:
                raise CatalogError("NIM root enrollment reconciler is not ready")
            admission_request = review.get("request")
            if not isinstance(admission_request, Mapping):
                raise CatalogError("NIM admission request is absent")
            admitted = admission_request.get("object")
            resource = admission_request.get("resource")
            if not isinstance(admitted, Mapping) or not isinstance(resource, Mapping):
                raise CatalogError("NIM admission object or resource is absent")
            descendant_resource = (
                resource.get("group"),
                resource.get("version"),
                resource.get("resource"),
            ) in {
                ("", "v1", "pods"),
                ("apps", "v1", "deployments"),
                ("apps", "v1", "replicasets"),
                ("apps", "v1", "statefulsets"),
                ("batch", "v1", "jobs"),
            }
            pre_resolved_owner_chain: list[Mapping[str, Any]] | None = None
            if descendant_resource:
                metadata = admitted.get("metadata")
                owner_references = (
                    metadata.get("ownerReferences")
                    if isinstance(metadata, Mapping)
                    else None
                )
                if owner_references is not None and owner_references != []:
                    pre_resolved_owner_chain = await resolver.owner_chain_to_nim(
                        admitted,
                        namespace=config.namespace,
                    )
            if pre_resolved_owner_chain is not None:
                selected = config.select_persisted_root(
                    pre_resolved_owner_chain[-1], catalog=catalog
                )
            else:
                selected = config.select(review, catalog=catalog)
            if selected is None:
                return JSONResponse(
                    {"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview", "response": {"uid": uid, "allowed": True}}
                )
            kind, model_id, envelope = selected
            subject = envelope["subject"]
            descendant_contract = next(
                (
                    contract
                    for contract in subject["descendant_resources"].values()
                    if resource
                    == {
                        "group": contract["group"],
                        "version": contract["version"],
                        "resource": contract["resource"],
                    }
                ),
                None,
            )
            actor_identity = subject["actor_identities"][
                descendant_contract["actor_identity"]
                if descendant_contract is not None
                else "custom_resource"
            ]
            actor_chain = await resolver.actor_chain(
                admission_request["userInfo"], actor_identity
            )
            owner_chain: Sequence[Mapping[str, Any]] = []
            if descendant_contract is not None:
                owner_chain = pre_resolved_owner_chain or await resolver.owner_chain(
                    admitted,
                    root_kind=kind,
                    namespace=config.namespace,
                )
            response = validate_nim_operator_admission_review(
                review,
                security_envelope=envelope,
                trusted_attestors=config.trusted_attestors,
                security_session_id=config.security_session_id,
                resource_kind=kind,
                record=catalog.model(model_id),
                resolved_owner_chain=owner_chain,
                resolved_actor_chain=actor_chain,
            )
            root: Mapping[str, Any] | None = None
            if descendant_contract is not None:
                root = owner_chain[-1]
            elif admission_request.get("operation") == "UPDATE":
                root = admitted
            if root is not None:
                await resolver.verify_root_enrollment(
                    root,
                    model_id=model_id,
                    subject_sha256=str(envelope["subject_sha256"]),
                    policy=config.admission_policy,
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
    kubernetes_api_url: str,
    kubernetes_token_file: Path,
    kubernetes_ca_file: Path,
    kubernetes_timeout_seconds: float,
    port: int,
    log_level: str,
) -> None:
    catalog = load_catalog(catalog_dir)
    config = NimAdmissionConfig.load(config_file, catalog_dir=catalog_dir)
    resolver = KubernetesNimOwnerResolver(
        base_url=kubernetes_api_url,
        token_file=kubernetes_token_file,
        ca_file=kubernetes_ca_file,
        timeout_seconds=kubernetes_timeout_seconds,
    )
    await resolver.verify_admission_policy(config.admission_policy)
    server = uvicorn.Server(
        uvicorn.Config(
            create_nim_admission_app(config=config, catalog=catalog, resolver=resolver),
            host="0.0.0.0",  # noqa: S104 - cluster-internal TLS Service only
            port=port,
            ssl_certfile=str(tls_certificate_file),
            ssl_keyfile=str(tls_private_key_file),
            log_level=log_level.lower(),
        )
    )
    try:
        await server.serve()
    finally:
        await resolver.close()

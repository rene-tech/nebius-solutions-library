#!/usr/bin/env python3
"""CAS executor for an externally signed storage-reconciler cutover.

This program is deliberately separate from Helm and the workloads Terraform
identity.  It reads the same signed state as the admission service, mutates
only ``spec.replicas`` plus the content-bound transition annotation, and
prints a canonical non-secret observation for the external owner to sign as
the next ledger generation.  It never deletes or replaces an object.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from daemonset_fence_policy import canonical
from daemonset_fence_runtime import _safe_read
from storage_reconciler_cutover_runtime import load_verified_state

TOKEN_PATH = Path("/var/run/fs2-cutover-kubernetes/token")
CA_PATH = Path("/var/run/fs2-cutover-kubernetes/ca.crt")
TRANSITION_ANNOTATION = "fs2.nebius.ai/storage-cutover-transition-sha256"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: object, *args: object, **kwargs: object) -> None:
        raise ValueError("Kubernetes API redirects are forbidden")


class KubernetesAPI:
    def __init__(self, endpoint: str, token_path: Path = TOKEN_PATH, ca_path: Path = CA_PATH) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValueError("exact Kubernetes HTTPS authority required")
        self.endpoint = endpoint.rstrip("/")
        self.token_path = token_path
        context = ssl.create_default_context(cadata=_safe_read(ca_path).decode("ascii"))
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _NoRedirect()
        )

    def _token(self, identity: dict[str, Any]) -> str:
        """Descriptor-read and bind one fresh, expiring epoch credential."""

        token = _safe_read(self.token_path).decode("ascii").strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError("bounded Kubernetes bearer token is malformed")
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("cutover credential is not a bounded JWT")
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("cutover credential claims are malformed") from exc
        now = int(time.time())
        audience = payload.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience
        jti = str(payload.get("jti", ""))
        kubernetes_claims = payload.get("kubernetes.io") or {}
        service_account_claims = (
            kubernetes_claims.get("serviceaccount")
            if isinstance(kubernetes_claims, dict)
            else {}
        ) or {}
        uid = str(
            service_account_claims.get(
                "uid",
                payload.get("kubernetes.io/serviceaccount/uid", payload.get("uid", "")),
            )
        )
        groups = sorted(payload.get("groups") or [])
        try:
            signed_from = int(
                datetime.fromisoformat(
                    str(identity["valid_from"]).replace("Z", "+00:00")
                ).timestamp()
            )
            signed_until = int(
                datetime.fromisoformat(
                    str(identity["valid_until"]).replace("Z", "+00:00")
                ).timestamp()
            )
        except (KeyError, ValueError) as exc:
            raise ValueError("signed cutover credential epoch is malformed") from exc
        if (
            not isinstance(payload.get("exp"), int)
            or not isinstance(payload.get("iat"), int)
            or payload["exp"] <= now + 10
            or payload["iat"] > now + 5
            or payload["exp"] - payload["iat"] > 900
            or payload["iat"] < signed_from
            or payload["exp"] > signed_until
            or "fs2-storage-cutover" not in (audiences or [])
            or payload.get("sub") != identity.get("username")
            or uid != identity.get("uid")
            or groups != sorted(identity.get("groups") or [])
            or hashlib.sha256(jti.encode()).hexdigest()
            != identity.get("credential_id")
        ):
            raise ValueError("cutover credential does not match the current signed epoch")
        return token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        content_type: str = "application/json",
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        url = f"{self.endpoint}{path}"
        payload = canonical(body) if body is not None else None
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token(identity)}",
                "Content-Type": content_type,
                "User-Agent": "fs2-storage-cutover-executor/1",
            },
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                if response.geturl() != url or response.status not in {200, 201}:
                    raise ValueError("Kubernetes API identity or status differs")
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Kubernetes CAS request failed with status {exc.code}") from None
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise ValueError("Kubernetes response is absent or oversized")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Kubernetes response is not an object")
        return value

    def deployment(
        self, contract: dict[str, Any], identity: dict[str, Any]
    ) -> dict[str, Any]:
        namespace = urllib.parse.quote(contract["namespace"], safe="")
        name = urllib.parse.quote(contract["name"], safe="")
        return self.request(
            "GET",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            identity=identity,
        )

    def pods(self, contract: dict[str, Any], identity: dict[str, Any]) -> list[dict[str, Any]]:
        namespace = urllib.parse.quote(contract["namespace"], safe="")
        generation = urllib.parse.quote(
            f"fs2.nebius.ai/storage-rollout-generation={contract['generation']}", safe=""
        )
        response = self.request(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods?labelSelector={generation}",
            identity=identity,
        )
        items = response.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError("Pod inventory differs")
        return items

    def scale(
        self,
        contract: dict[str, Any],
        observed: dict[str, Any],
        replicas: int,
        transition_id: str,
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = observed.get("metadata") or {}
        namespace = urllib.parse.quote(contract["namespace"], safe="")
        name = urllib.parse.quote(contract["name"], safe="")
        body = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": contract["name"],
                "namespace": contract["namespace"],
                "resourceVersion": metadata.get("resourceVersion"),
                "annotations": {TRANSITION_ANNOTATION: transition_id},
            },
            "spec": {"replicas": replicas},
        }
        return self.request(
            "PATCH",
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            body=body,
            content_type="application/merge-patch+json",
            identity=identity,
        )


def _observed(
    api: KubernetesAPI,
    generation: str,
    contract: dict[str, Any],
    identity: dict[str, Any],
) -> dict[str, Any]:
    deployment = api.deployment(contract, identity)
    metadata = deployment.get("metadata") or {}
    spec = deployment.get("spec") or {}
    normalized = json.loads(canonical(spec))
    normalized["replicas"] = 0
    if (
        metadata.get("namespace") != contract["namespace"]
        or metadata.get("name") != contract["name"]
        or metadata.get("uid") != contract["uid"]
        or normalized != contract["passive_spec"]
        or ((metadata.get("labels") or {}).get("fs2.nebius.ai/storage-rollout-generation"))
        != generation
    ):
        raise ValueError("live Deployment differs from the signed immutable contract")
    return deployment


def _quiesced(
    api: KubernetesAPI,
    contract: dict[str, Any],
    deployment: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    status = deployment.get("status") or {}
    if any(int(status.get(field) or 0) != 0 for field in ("replicas", "readyReplicas", "availableReplicas")):
        return False
    for pod in api.pods(contract, identity):
        phase = (pod.get("status") or {}).get("phase")
        if phase not in {"Succeeded", "Failed"}:
            return False
    return True


def _ready(deployment: dict[str, Any]) -> bool:
    spec_replicas = int((deployment.get("spec") or {}).get("replicas") or 0)
    status = deployment.get("status") or {}
    return bool(
        spec_replicas == 1
        and int(status.get("observedGeneration") or 0)
        >= int((deployment.get("metadata") or {}).get("generation") or 0)
        and int(status.get("updatedReplicas") or 0) == 1
        and int(status.get("readyReplicas") or 0) == 1
        and int(status.get("availableReplicas") or 0) == 1
        and int(status.get("unavailableReplicas") or 0) == 0
    )


def _projection(generation: str, contract: dict[str, Any], deployment: dict[str, Any]) -> dict[str, Any]:
    metadata = deployment.get("metadata") or {}
    status = deployment.get("status") or {}
    return {
        "generation": generation,
        "uid": contract["uid"],
        "replicas": (deployment.get("spec") or {}).get("replicas"),
        "ready_replicas": int(status.get("readyReplicas") or 0),
        "available_replicas": int(status.get("availableReplicas") or 0),
        "unavailable_replicas": int(status.get("unavailableReplicas") or 0),
        "resource_version": metadata.get("resourceVersion"),
    }


def execute(api: KubernetesAPI, state: dict[str, Any]) -> dict[str, Any]:
    transition = state["transition"]
    phase = transition["phase"]
    predecessor_generation = transition["predecessor_generation"]
    successor_generation = transition["successor_generation"]
    predecessor = {**state["deployments"][predecessor_generation], "generation": predecessor_generation}
    successor = {**state["deployments"][successor_generation], "generation": successor_generation}
    identity = state["security_owner_identity"]
    observed_predecessor = _observed(
        api, predecessor_generation, predecessor, identity
    )
    observed_successor = _observed(api, successor_generation, successor, identity)

    if phase == "QUIESCE_PREDECESSOR":
        if (observed_successor.get("spec") or {}).get("replicas") != 0:
            raise ValueError("successor must remain passive before predecessor quiescence")
        if (observed_predecessor.get("spec") or {}).get("replicas") == 1:
            if (
                not isinstance(transition.get("provider_drain_receipt"), dict)
                or transition["provider_drain_receipt"].get(
                    "nonterminal_provider_operations"
                )
                != 0
            ):
                raise ValueError("signed zero-terminal provider drain is required")
            observed_predecessor = api.scale(
                predecessor,
                observed_predecessor,
                0,
                transition["transition_id"],
                identity,
            )
        if not _quiesced(api, predecessor, observed_predecessor, identity):
            raise RuntimeError("predecessor has not reached bounded quiescence")
    elif phase == "ACTIVATE_SUCCESSOR":
        if not _quiesced(api, predecessor, observed_predecessor, identity):
            raise ValueError("signed predecessor quiescence no longer matches live state")
        if (observed_successor.get("spec") or {}).get("replicas") == 0:
            observed_successor = api.scale(
                successor,
                observed_successor,
                1,
                transition["transition_id"],
                identity,
            )
        if not _ready(observed_successor):
            raise RuntimeError("successor has not reached exact single-replica readiness")
    elif phase == "ROLLBACK_QUIESCE":
        if (observed_successor.get("spec") or {}).get("replicas") == 1:
            observed_successor = api.scale(
                successor,
                observed_successor,
                0,
                transition["transition_id"],
                identity,
            )
        if not _quiesced(api, successor, observed_successor, identity):
            raise RuntimeError("successor has not reached bounded rollback quiescence")
    elif phase == "ROLLBACK_ACTIVATE":
        if not _quiesced(api, successor, observed_successor, identity):
            raise ValueError("signed successor quiescence no longer matches live state")
        if (observed_predecessor.get("spec") or {}).get("replicas") == 0:
            observed_predecessor = api.scale(
                predecessor,
                observed_predecessor,
                1,
                transition["transition_id"],
                identity,
            )
        if not _ready(observed_predecessor):
            raise RuntimeError("rollback predecessor has not reached exact readiness")
    else:
        raise ValueError("signed cutover phase has no executable mutation")

    observation = {
        "schema": "fs2-serve.nebius.ai/storage-reconciler-cutover-observation/v1",
        "state_head_sha256": state["head_sha256"],
        "transition_id": transition["transition_id"],
        "phase": phase,
        "provider_drain_receipt": transition["provider_drain_receipt"],
        "provider_drain_receipt_sha256": transition[
            "provider_drain_receipt_sha256"
        ],
        "predecessor": _projection(
            predecessor_generation, predecessor, observed_predecessor
        ),
        "successor": _projection(successor_generation, successor, observed_successor),
    }
    return {**observation, "observation_sha256": hashlib.sha256(canonical(observation)).hexdigest()}


def main() -> None:
    state = load_verified_state()
    endpoint = os.environ.get("FS2_CUTOVER_KUBERNETES_API", "")
    result = execute(KubernetesAPI(endpoint), state)
    print(canonical(result).decode("utf-8"))


if __name__ == "__main__":
    main()

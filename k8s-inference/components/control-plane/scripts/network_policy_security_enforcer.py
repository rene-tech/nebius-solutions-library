#!/usr/bin/env python3
"""Security-owned enforcer for permanent public-edge NetworkPolicy transitions."""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

BOUNDARY_LABEL = "fs2.nebius.ai/network-policy-boundary"
BOUNDARY_VALUE = "permanent"
ROLE_LABEL = "fs2.nebius.ai/network-policy-role"
CANDIDATE_ANNOTATION = "fs2.nebius.ai/network-policy-candidate-sha256"
NORMAL_ANNOTATION = "fs2.nebius.ai/normal-policy-name"
DENY_ANNOTATION = "fs2.nebius.ai/deny-policy-name"
FENCE_ANNOTATION = "fs2.nebius.ai/network-policy-fence"
TOPOLOGY_NAME = "fs2-network-policy-boundary-topology"
STATE_NAME = "fs2-network-policy-transition"
PARAMETER_NAME = "fs2-network-policy-boundary-parameters"
ADMISSION_NAME = "fs2-network-policy-boundary"
RELEASE_NAME = "fs2-serve-control-plane"
RELEASE_NAMESPACE = "fs2-system"
RECEIPT_SCHEMA = "fs2-serve.nebius.ai/network-policy-transition-receipt/v3"
REQUEST_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-handoff-request/v2"
RESPONSE_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-handoff-response/v2"
ATTESTATION_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-automation-attestation/v2"
RECOVERY_APPROVAL_SCHEMA = "fs2-serve.nebius.ai/network-policy-recovery-approval/v2"
HANDOFF_SECONDS = 30
MAX_REQUEST_BYTES = 1024 * 1024
SOCKET_READ_SECONDS = 5.0
RELAXED_SELECTOR = {"fs2.nebius.ai/network-policy-deny-relaxed": "true"}
ACTIVE_DENY_SPEC = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
RELAXED_DENY_SPEC = {
    "podSelector": {"matchLabels": RELAXED_SELECTOR},
    "policyTypes": ["Ingress"],
    "ingress": [],
}


class EnforcerError(RuntimeError):
    """A signed request failed a security-side invariant."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def decode_base64url(value: Any, *, expected_bytes: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise EnforcerError("signed handoff contains invalid base64url data")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise EnforcerError("signed handoff contains invalid base64url data") from error
    if len(decoded) != expected_bytes:
        raise EnforcerError("signed handoff has an invalid cryptographic length")
    return decoded


def instant(value: Any, *, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise EnforcerError(f"{field} is not an RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EnforcerError(f"{field} is not an RFC3339 instant") from error
    if parsed.tzinfo is None:
        raise EnforcerError(f"{field} has no timezone")
    return parsed.astimezone(dt.UTC)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        key_stat = path.lstat()
    except OSError as error:
        raise EnforcerError("signing key is unavailable") from error
    if (
        not stat.S_ISREG(key_stat.st_mode)
        or stat.S_ISLNK(key_stat.st_mode)
        or stat.S_IMODE(key_stat.st_mode) not in {0o400, 0o600}
        or key_stat.st_uid != os.geteuid()
    ):
        raise EnforcerError("signing key ownership or mode is unsafe")
    try:
        encoded = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise EnforcerError("signing key cannot be read safely") from error
    return Ed25519PrivateKey.from_private_bytes(decode_base64url(encoded, expected_bytes=32))


def assert_security_owned_file(path: Path, *, label: str) -> None:
    try:
        file_stat = path.lstat()
    except OSError as error:
        raise EnforcerError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) not in {0o400, 0o600}
        or file_stat.st_uid != os.geteuid()
    ):
        raise EnforcerError(f"{label} ownership or mode is unsafe")


def assert_security_owned_socket_parent(path: Path, *, peer_gid: int) -> None:
    try:
        parent_stat = path.parent.lstat()
    except OSError as error:
        raise EnforcerError("security handoff socket parent is unavailable") from error
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or parent_stat.st_gid != peer_gid
        or stat.S_IMODE(parent_stat.st_mode) != 0o2710
    ):
        raise EnforcerError("security handoff socket parent must be an enforcer-owned peer-GID setgid 02710 directory")


def assert_absent_socket_path(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EnforcerError("security handoff socket path cannot be inspected safely") from error
    raise EnforcerError("security handoff socket path already exists")


class KubernetesAPI(Protocol):
    def get(self, resource: str, name: str, namespace: str = "") -> dict[str, Any]: ...

    def patch(
        self,
        resource: str,
        name: str,
        namespace: str,
        patch_type: str,
        patch: Any,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    def cluster_identity(self) -> dict[str, str]: ...

    def user_info(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class KubectlAPI:
    """Exact-name Kubernetes access using the security-owned kubeconfig."""

    def __init__(self, kubeconfig: Path, context: str) -> None:
        assert_security_owned_file(kubeconfig, label="security kubeconfig")
        prefix = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
        if context:
            prefix.extend(["--context", context])
        self.prefix = prefix

    def _run(self, *arguments: str) -> CommandResult:
        result = subprocess.run(  # noqa: S603 -- fixed executable and validated exact arguments
            [*self.prefix, *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        outcome = CommandResult(result.returncode, result.stdout, result.stderr)
        if outcome.returncode != 0:
            raise EnforcerError("security-owned Kubernetes request failed closed")
        return outcome

    def get(self, resource: str, name: str, namespace: str = "") -> dict[str, Any]:
        arguments = ["get", resource, name]
        if namespace:
            arguments.extend(["--namespace", namespace])
        arguments.extend(["-o", "json"])
        try:
            resource_object = json.loads(self._run(*arguments).stdout)
        except json.JSONDecodeError as error:
            raise EnforcerError("Kubernetes response is not valid JSON") from error
        if not isinstance(resource_object, dict):
            raise EnforcerError("Kubernetes response is not an object")
        return cast(dict[str, Any], resource_object)

    def patch(
        self,
        resource: str,
        name: str,
        namespace: str,
        patch_type: str,
        patch: Any,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        arguments = ["patch", resource, name]
        if namespace:
            arguments.extend(["--namespace", namespace])
        arguments.extend([f"--type={patch_type}", "--patch", canonical(patch)])
        if dry_run:
            arguments.append("--dry-run=server")
        arguments.extend(["-o", "json"])
        try:
            resource_object = json.loads(self._run(*arguments).stdout)
        except json.JSONDecodeError as error:
            raise EnforcerError("Kubernetes patch response is not valid JSON") from error
        if not isinstance(resource_object, dict):
            raise EnforcerError("Kubernetes patch response is not an object")
        return cast(dict[str, Any], resource_object)

    def cluster_identity(self) -> dict[str, str]:
        config = json.loads(self._run("config", "view", "--minify", "--raw", "-o", "json").stdout)
        clusters = config.get("clusters", [])
        if len(clusters) != 1 or not isinstance(clusters[0].get("cluster", {}).get("server"), str):
            raise EnforcerError("security kubeconfig does not select one API server")
        uid = self._run("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").stdout.strip()
        if not uid:
            raise EnforcerError("security kubeconfig cannot bind kube-system UID")
        return {
            "api_server_sha256": sha256_text(clusters[0]["cluster"]["server"]),
            "kube_system_uid": uid,
        }

    def user_info(self) -> dict[str, Any]:
        try:
            response = json.loads(self._run("auth", "whoami", "-o", "json").stdout)
            info = response["status"]["userInfo"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise EnforcerError("security kubeconfig has no exact whoami identity") from error
        if (
            not isinstance(info.get("username"), str)
            or not info["username"]
            or not isinstance(info.get("uid"), str)
            or not info["uid"]
            or not isinstance(info.get("groups", []), list)
            or not isinstance(info.get("extra", {}), dict)
            or not all(isinstance(group, str) and group for group in info.get("groups", []))
            or not all(
                isinstance(key, str)
                and key
                and isinstance(values, list)
                and all(isinstance(value, str) for value in values)
                for key, values in info.get("extra", {}).items()
            )
        ):
            raise EnforcerError("security whoami identity is incomplete")
        return {
            "username": info["username"],
            "uid": info["uid"],
            "groups": sorted(info.get("groups", [])),
            "extra": {key: sorted(values) for key, values in sorted(info.get("extra", {}).items())},
        }


def object_evidence(resource_object: dict[str, Any]) -> dict[str, Any]:
    metadata = resource_object.get("metadata", {})
    namespace = metadata.get("namespace", "")
    if not all(
        isinstance(metadata.get(field), str) and metadata.get(field) for field in ("name", "uid", "resourceVersion")
    ):
        raise EnforcerError("protected object has incomplete Kubernetes identity")
    return {
        "api_version": resource_object.get("apiVersion"),
        "kind": resource_object.get("kind"),
        "namespace": namespace,
        "name": metadata["name"],
        "uid": metadata["uid"],
        "resource_version": metadata["resourceVersion"],
        "state_sha256": sha256_json(
            {
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
                "spec": resource_object.get("spec"),
                "data": resource_object.get("data"),
            }
        ),
    }


def receipt_from_object(resource_object: dict[str, Any]) -> dict[str, Any]:
    try:
        receipt = json.loads(resource_object.get("data", {}).get("receipt.json", ""))
    except json.JSONDecodeError as error:
        raise EnforcerError("transition receipt is not valid JSON") from error
    if not isinstance(receipt, dict):
        raise EnforcerError("transition receipt is not an object")
    return cast(dict[str, Any], receipt)


class SecurityEnforcer:
    """Authorize only receipt- and fence-bound semantic state transitions."""

    def __init__(
        self,
        api: KubernetesAPI,
        *,
        client_public_key_value: str,
        server_private_key: Ed25519PrivateKey,
        recovery_public_key_value: str,
        expected_cluster: dict[str, str],
        expected_peer_uid: int,
        expected_peer_gid: int,
        expected_socket_path: Path,
        security_kubeconfig_sha256: str,
    ) -> None:
        self.api = api
        self.client_public_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(client_public_key_value, expected_bytes=32)
        )
        self.client_key_id = sha256_text(client_public_key_value)
        self.server_private_key = server_private_key
        server_public_value = (
            base64.urlsafe_b64encode(server_private_key.public_key().public_bytes_raw()).decode().rstrip("=")
        )
        self.server_key_id = sha256_text(server_public_value)
        self.recovery_public_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(recovery_public_key_value, expected_bytes=32)
        )
        self.recovery_key_id = sha256_text(recovery_public_key_value)
        self.expected_cluster = expected_cluster
        self.expected_peer_uid = expected_peer_uid
        self.expected_peer_gid = expected_peer_gid
        self.expected_socket_path = expected_socket_path
        self.security_kubeconfig_sha256 = security_kubeconfig_sha256
        self.seen_operations: set[str] = set()

    def _topology(self) -> tuple[dict[str, Any], dict[str, Any]]:
        resource = self.api.get("configmap", TOPOLOGY_NAME, RELEASE_NAMESPACE)
        try:
            contract = json.loads(resource.get("data", {}).get("topology.json", ""))
        except json.JSONDecodeError as error:
            raise EnforcerError("protected topology is not valid JSON") from error
        if not isinstance(contract, dict):
            raise EnforcerError("protected topology is not an object")
        return resource, cast(dict[str, Any], contract)

    def _verify_request(self, envelope: dict[str, Any], peer_uid: int, peer_gid: int) -> dict[str, Any]:
        if peer_uid != self.expected_peer_uid or peer_gid != self.expected_peer_gid:
            raise EnforcerError("Unix peer UID/GID is not authorized")
        if set(envelope) != {"signed", "signature"} or not isinstance(envelope.get("signed"), dict):
            raise EnforcerError("request envelope is not exact")
        request = cast(dict[str, Any], envelope["signed"])
        expected_fields = {
            "schema",
            "operation_id",
            "client_key_id",
            "action",
            "cluster",
            "release",
            "issued_at",
            "expires_at",
            "body",
        }
        if set(request) != expected_fields:
            raise EnforcerError("signed request fields are not exact")
        try:
            self.client_public_key.verify(
                decode_base64url(envelope["signature"], expected_bytes=64),
                canonical(request).encode(),
            )
        except InvalidSignature as error:
            raise EnforcerError("request signature is invalid") from error
        issued = instant(request["issued_at"], field="request issued_at")
        expires = instant(request["expires_at"], field="request expires_at")
        now = dt.datetime.now(dt.UTC)
        operation_id = request.get("operation_id")
        if (
            request.get("schema") != REQUEST_SCHEMA
            or not isinstance(operation_id, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                operation_id,
            )
            or request.get("client_key_id") != self.client_key_id
            or request.get("cluster") != self.expected_cluster
            or request.get("release") != {"name": RELEASE_NAME, "namespace": RELEASE_NAMESPACE}
            or issued < now - dt.timedelta(seconds=5)
            or issued > now + dt.timedelta(seconds=5)
            or expires < now
            or expires > issued + dt.timedelta(seconds=HANDOFF_SECONDS)
            or request.get("action") not in {"attest", "transition-mutation", "set-admission-recovery"}
            or not isinstance(request.get("body"), dict)
        ):
            raise EnforcerError("signed request is stale or outside the exact contract")
        if operation_id in self.seen_operations:
            raise EnforcerError("signed request operation was already consumed")
        if self.api.cluster_identity() != self.expected_cluster:
            raise EnforcerError("security automation is connected to a different cluster")
        self.seen_operations.add(operation_id)
        return request

    def _response(self, request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        issued = dt.datetime.now(dt.UTC)
        signed = {
            "schema": RESPONSE_SCHEMA,
            "operation_id": request["operation_id"],
            "request_sha256": sha256_json(request),
            "cluster": self.expected_cluster,
            "issued_at": issued.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "expires_at": (issued + dt.timedelta(seconds=HANDOFF_SECONDS))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "status": "approved",
            "signer_key_id": self.server_key_id,
            "result": result,
        }
        signature = (
            base64.urlsafe_b64encode(self.server_private_key.sign(canonical(signed).encode())).decode().rstrip("=")
        )
        return {"signed": signed, "signature": signature}

    @staticmethod
    def _assert_evidence(live: dict[str, Any], evidence: Any, *, label: str) -> None:
        if not isinstance(evidence, dict) or object_evidence(live) != {
            key: evidence.get(key)
            for key in (
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "resource_version",
                "state_sha256",
            )
        }:
            raise EnforcerError(f"{label} live identity/state changed")

    @staticmethod
    def _assert_boundary(resource_object: dict[str, Any], role: str) -> None:
        labels = resource_object.get("metadata", {}).get("labels", {})
        if labels.get(BOUNDARY_LABEL) != BOUNDARY_VALUE or labels.get(ROLE_LABEL) != role:
            raise EnforcerError("mutation target is not the exact permanent boundary role")

    def _assert_topology(self, evidence: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        resource, contract = self._topology()
        metadata = resource.get("metadata", {})
        expected = {
            "namespace": RELEASE_NAMESPACE,
            "name": TOPOLOGY_NAME,
            "uid": metadata.get("uid"),
            "resource_version": metadata.get("resourceVersion"),
            "sha256": sha256_json(contract),
        }
        if evidence != expected:
            raise EnforcerError("protected topology evidence changed")
        handoff = contract.get("security_handoff", {})
        identity = handoff.get("identity_boundary", {})
        epoch = str(identity.get("identity_epoch", ""))
        prior_epoch = str(identity.get("prior_identity_epoch", ""))
        successor_epoch = str(identity.get("successor_identity_epoch", ""))

        def principal(role: str, value: str) -> str:
            return f"fs2-np-{role}-{hashlib.sha256(value.encode()).hexdigest()[:16]}"

        expected_principals = {
            "release": principal("release", epoch),
            "security_owner": principal("security-owner", epoch),
            "security_bootstrap": principal("security-bootstrap", epoch),
            "prior_owner": principal("security-owner", prior_epoch),
            "prior_bootstrap": principal("security-bootstrap", prior_epoch),
            "successor_owner": principal("security-owner", successor_epoch),
            "successor_bootstrap": principal("security-bootstrap", successor_epoch),
        }
        now = dt.datetime.now(dt.UTC)
        if (
            handoff.get("client_public_key_sha256") != self.client_key_id
            or handoff.get("server_public_key_sha256") != self.server_key_id
            or handoff.get("recovery_public_key_sha256") != self.recovery_key_id
            or handoff.get("peer_uid") != self.expected_peer_uid
            or handoff.get("peer_gid") != self.expected_peer_gid
            or handoff.get("peer_gid_contract") != "effective-dedicated"
            or handoff.get("socket_path") != str(self.expected_socket_path)
            or handoff.get("socket_directory_contract") != "precreated-setgid-02710"
            or handoff.get("cluster") != self.expected_cluster
            or handoff.get("allowed_actions") != ["transition-mutation", "set-admission-recovery"]
            or handoff.get("delete_allowed") is not False
            or identity.get("schema") != "fs2-serve.nebius.ai/network-policy-identity-boundary/v3"
            or any(
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", value)
                for value in (prior_epoch, epoch, successor_epoch)
            )
            or len({prior_epoch, epoch, successor_epoch}) != 3
            or identity.get("epoch_principals") != expected_principals
            or contract.get("security_owner_username") != expected_principals["security_owner"]
            or contract.get("security_bootstrap_username") != expected_principals["security_bootstrap"]
            or contract.get("successor_security_bootstrap_username") != expected_principals["successor_bootstrap"]
            or contract.get("successor_security_owner_username") != expected_principals["successor_owner"]
            or self.expected_socket_path.parent.name != epoch
            or identity.get("security_user_info_sha256") != sha256_json(self.api.user_info())
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("credential_set_sha256", "")))
            or identity.get("security_kubeconfig_sha256") != self.security_kubeconfig_sha256
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("prior_security_kubeconfig_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("prior_bootstrap_kubeconfig_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("security_subject_inventory_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("provider_subject_snapshot_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("kubernetes_subject_inventory_sha256", "")))
            or identity.get("plan_preflight_verified") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("plan_preflight_sha256", "")))
            or identity.get("bootstrap_must_be_expired") is not True
            or instant(identity.get("bootstrap_expires_at"), field="bootstrap identity expires_at") > now
            or instant(identity.get("security_expires_at"), field="security identity expires_at") <= now
            or instant(identity.get("rollback_valid_until"), field="rollback validity") <= now
            or int(identity.get("minimum_rollback_seconds", 0)) < 3600
            or identity.get("rotation_contract")
            != {
                "mechanism": "preauthorized-successor-epoch",
                "bootstrap_update_identity": expected_principals["security_bootstrap"],
                "successor_security_owner_identity": expected_principals["successor_owner"],
                "successor_bootstrap_identity": expected_principals["successor_bootstrap"],
                "prior_security_owner_identity": expected_principals["prior_owner"],
                "prior_bootstrap_identity": expected_principals["prior_bootstrap"],
                "new_paths_required": True,
                "prior_epoch_authorization_denied": True,
            }
        ):
            raise EnforcerError("live topology does not authorize this enforcer")
        return resource, contract

    @staticmethod
    def _lease_is_fenced(lease: dict[str, Any], evidence: dict[str, Any]) -> None:
        spec = lease.get("spec", {})
        if (
            not spec.get("holderIdentity")
            or spec.get("holderIdentity") != evidence.get("holder_identity")
            or spec.get("leaseTransitions") != evidence.get("lease_transitions")
        ):
            raise EnforcerError("transition Lease is not held by the signed request fence")
        renew = instant(spec.get("renewTime"), field="Lease renewTime")
        duration = int(spec.get("leaseDurationSeconds", 0))
        if duration <= 0 or dt.datetime.now(dt.UTC) >= renew + dt.timedelta(seconds=duration):
            raise EnforcerError("transition Lease fence is expired")

    def _live_transition_state(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        lease = self.api.get("lease", STATE_NAME, RELEASE_NAMESPACE)
        receipt_object = self.api.get("configmap", STATE_NAME, RELEASE_NAMESPACE)
        self._assert_evidence(lease, body.get("lease"), label="Lease")
        self._assert_evidence(receipt_object, body.get("receipt"), label="receipt")
        receipt = receipt_from_object(receipt_object)
        if (
            body.get("receipt", {}).get("receipt_sha256") != sha256_json(receipt)
            or body.get("receipt", {}).get("phase") != receipt.get("phase")
            or body.get("receipt", {}).get("intent") != receipt.get("intent")
        ):
            raise EnforcerError("signed request is not bound to the live durable receipt")
        self._lease_is_fenced(lease, cast(dict[str, Any], body["lease"]))
        return lease, receipt_object, receipt

    @staticmethod
    def _validate_lease_patch(operation: str, live: dict[str, Any], patch: Any) -> None:
        if not isinstance(patch, list) or not patch:
            raise EnforcerError("Lease transition requires a bounded JSON Patch")
        expected_rv = live.get("metadata", {}).get("resourceVersion")
        tests = {item.get("path"): item.get("value") for item in patch if item.get("op") == "test"}
        adds = {item.get("path"): item.get("value") for item in patch if item.get("op") == "add"}
        if tests.get("/metadata/resourceVersion") != expected_rv:
            raise EnforcerError("Lease patch lacks the exact live resourceVersion test")
        if operation == "lease-acquire":
            current_holder = str(live.get("spec", {}).get("holderIdentity", ""))
            requested_holder = adds.get("/spec/holderIdentity")
            current_renewal = live.get("spec", {}).get("renewTime")
            current_duration = int(live.get("spec", {}).get("leaseDurationSeconds", 0))
            current_unexpired = False
            if current_holder and current_renewal and current_duration > 0:
                current_unexpired = dt.datetime.now(dt.UTC) < instant(
                    current_renewal, field="current Lease renewTime"
                ) + dt.timedelta(seconds=current_duration)
            if (
                set(tests) != {"/metadata/resourceVersion"}
                or set(adds)
                != {
                    "/spec/holderIdentity",
                    "/spec/leaseDurationSeconds",
                    "/spec/leaseTransitions",
                    "/spec/renewTime",
                }
                or not isinstance(requested_holder, str)
                or not requested_holder
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}-[1-9][0-9]*-[0-9a-f-]{36}", requested_holder)
                or (current_unexpired and current_holder != requested_holder)
                or adds["/spec/leaseDurationSeconds"] != 60
                or adds["/spec/leaseTransitions"] != int(live.get("spec", {}).get("leaseTransitions", 0)) + 1
            ):
                raise EnforcerError("Lease acquire patch is not the exact fenced transition")
        elif operation == "lease-renew":
            if (
                set(tests)
                != {
                    "/metadata/resourceVersion",
                    "/spec/holderIdentity",
                    "/spec/leaseTransitions",
                }
                or set(adds) != {"/spec/renewTime"}
                or tests["/spec/holderIdentity"] != live.get("spec", {}).get("holderIdentity")
                or tests["/spec/leaseTransitions"] != live.get("spec", {}).get("leaseTransitions")
            ):
                raise EnforcerError("Lease renew patch is not bound to the live fence")
        elif operation == "lease-release":
            if (
                set(tests)
                != {
                    "/metadata/resourceVersion",
                    "/spec/holderIdentity",
                    "/spec/leaseTransitions",
                }
                or tests["/spec/holderIdentity"] != live.get("spec", {}).get("holderIdentity")
                or tests["/spec/leaseTransitions"] != live.get("spec", {}).get("leaseTransitions")
                or adds != {"/spec/holderIdentity": ""}
            ):
                raise EnforcerError("Lease release patch is not exact")
        else:
            raise EnforcerError("Lease semantic operation is not allowed")
        if "/spec/renewTime" in adds:
            renewal = instant(adds["/spec/renewTime"], field="requested Lease renewTime")
            if abs((dt.datetime.now(dt.UTC) - renewal).total_seconds()) > 5:
                raise EnforcerError("requested Lease renewal is stale")

    @staticmethod
    def _allowed_receipt_transition(old_phase: str, new_phase: str) -> bool:
        allowed = {
            "uninitialized": {"bootstrap-relaxing", "guards-staging"},
            "bootstrap-relaxing": {"bootstrap-ready"},
            "bootstrap-ready": {"bootstrap-ready", "bootstrap-guards-staging"},
            "bootstrap-guards-staging": {"guards-ready"},
            "guards-staging": {"guards-ready"},
            "guards-ready": {"staged"},
            "staged": {"staged", "active", "guards-staging", "rollback-relaxing", "destroy-relaxing"},
            "active": {"active", "guards-staging", "rollback-relaxing", "destroy-relaxing"},
            "rollback-relaxing": {"rollback-prepared"},
            "rollback-prepared": {"rollback-guards-staging"},
            "rollback-guards-staging": {"rollback-guards-ready"},
            "rollback-guards-ready": {"rolled-back"},
            "rolled-back": {"rolled-back", "guards-staging", "rollback-relaxing", "destroy-relaxing"},
            "destroy-relaxing": {"destroy-prepared"},
            "destroy-prepared": {"destroy-prepared"},
        }
        return new_phase in allowed.get(old_phase, set())

    def _validate_receipt_patch(
        self,
        live: dict[str, Any],
        old_receipt: dict[str, Any],
        lease: dict[str, Any],
        patch: Any,
        contract: dict[str, Any],
        topology_resource: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(patch, dict) or set(patch) != {"metadata", "data"}:
            raise EnforcerError("receipt patch may update only resourceVersion and receipt.json")
        if patch.get("metadata") != {"resourceVersion": live.get("metadata", {}).get("resourceVersion")}:
            raise EnforcerError("receipt patch lacks the exact live resourceVersion")
        data = patch.get("data")
        if not isinstance(data, dict) or set(data) != {"receipt.json"}:
            raise EnforcerError("receipt patch data is not exact")
        try:
            desired = json.loads(data["receipt.json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise EnforcerError("desired receipt is not valid JSON") from error
        if not isinstance(desired, dict) or desired.get("schema") != RECEIPT_SCHEMA:
            raise EnforcerError("desired receipt schema is not exact")
        old_phase = str(old_receipt.get("phase", "uninitialized"))
        new_phase = str(desired.get("phase", ""))
        if (
            not self._allowed_receipt_transition(old_phase, new_phase)
            or desired.get("previous_phase") != old_phase
            or desired.get("receipt_object", {}).get("prior_resource_version")
            != live.get("metadata", {}).get("resourceVersion")
            or desired.get("fence", {}).get("holder_identity") != lease.get("spec", {}).get("holderIdentity")
            or desired.get("fence", {}).get("lease_transitions") != lease.get("spec", {}).get("leaseTransitions")
            or desired.get("security_recovery") != old_receipt.get("security_recovery")
        ):
            raise EnforcerError("desired receipt is outside the allowed durable state machine")
        candidate = desired.get("candidate", {})
        topology_metadata = topology_resource.get("metadata", {})
        expected_topology = {
            "contract": contract,
            "uid": topology_metadata.get("uid"),
            "resource_version": topology_metadata.get("resourceVersion"),
            "sha256": sha256_json(contract),
        }
        if (
            not isinstance(candidate, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("candidate_sha256", "")))
            or candidate.get("topology") != expected_topology
        ):
            raise EnforcerError("desired receipt candidate identity is incomplete")
        if old_phase not in {"uninitialized", "active", "staged", "rolled-back"}:
            old_candidate = old_receipt.get("candidate", {}).get("candidate_sha256")
            if old_candidate != candidate.get("candidate_sha256"):
                raise EnforcerError("in-flight receipt cannot change candidate identity")
        return cast(dict[str, Any], desired)

    @staticmethod
    def _validate_policy_patch(
        operation: str,
        live: dict[str, Any],
        receipt: dict[str, Any],
        lease: dict[str, Any],
        patch: Any,
        contract: dict[str, Any],
    ) -> None:
        if not isinstance(patch, dict) or set(patch) != {"metadata", "spec"}:
            raise EnforcerError("boundary patch may update only annotations, resourceVersion and spec")
        metadata = patch.get("metadata", {})
        annotations = metadata.get("annotations", {})
        if metadata.get("resourceVersion") != live.get("metadata", {}).get("resourceVersion"):
            raise EnforcerError("boundary patch lacks the exact live resourceVersion")
        fence = f"{lease.get('spec', {}).get('holderIdentity')}:{lease.get('spec', {}).get('leaseTransitions')}"
        if annotations.get(FENCE_ANNOTATION) != fence:
            raise EnforcerError("boundary patch is not bound to the live Lease fence")
        candidate = receipt.get("candidate", {})
        if annotations.get(CANDIDATE_ANNOTATION) != candidate.get("candidate_sha256"):
            raise EnforcerError("boundary patch is not bound to the durable candidate")
        intent = receipt.get("intent", {})
        phase = receipt.get("phase")
        desired_spec = patch.get("spec")
        policy_names = contract.get("policy_names", {})
        live_name = live.get("metadata", {}).get("name")
        if operation == "guard-stage":
            if phase not in {"guards-staging", "bootstrap-guards-staging", "rollback-guards-staging"}:
                raise EnforcerError("guard mutation has no durable staging phase")
            role = live.get("metadata", {}).get("labels", {}).get(ROLE_LABEL)
            policy_key = "public-envoy" if role == "public-envoy" else "envoy-controller"
            if (
                role not in {"public-envoy", "envoy-controller"}
                or desired_spec != candidate.get("policies", {}).get(policy_key, {}).get("spec")
                or intent.get("operation") not in {"stage-guards", "stage-bootstrap-guards", "stage-rollback-guards"}
                or annotations.get(NORMAL_ANNOTATION)
                != candidate.get("policies", {}).get(policy_key, {}).get("normal_name")
            ):
                raise EnforcerError("guard mutation does not match the receipt intent/spec")
            allowed_annotations = {CANDIDATE_ANNOTATION, NORMAL_ANNOTATION, FENCE_ANNOTATION}
            if role == "public-envoy":
                allowed_annotations.add(DENY_ANNOTATION)
                if annotations.get(DENY_ANNOTATION) != policy_names.get("default_deny"):
                    raise EnforcerError("proxy guard lost its deny binding")
            if set(annotations) != allowed_annotations:
                raise EnforcerError("guard mutation annotations are not exact")
        elif operation == "deny-relax":
            if (
                phase not in {"bootstrap-relaxing", "rollback-relaxing", "destroy-relaxing"}
                or desired_spec != RELAXED_DENY_SPEC
                or intent.get("operation") not in {"relax-deny", "relax-deny-for-rollback", "relax-deny-for-destroy"}
                or live_name != policy_names.get("default_deny")
                or set(annotations) != {CANDIDATE_ANNOTATION, FENCE_ANNOTATION}
            ):
                raise EnforcerError("deny relaxation does not match the durable intent")
        elif operation == "deny-activate":
            if (
                phase not in {"guards-ready", "rollback-guards-ready"}
                or desired_spec != ACTIVE_DENY_SPEC
                or intent.get("operation") not in {"activate-deny", "activate-deny-after-rollback"}
                or live_name != policy_names.get("default_deny")
                or set(annotations) != {CANDIDATE_ANNOTATION, FENCE_ANNOTATION}
            ):
                raise EnforcerError("deny activation does not match the durable intent")
        else:
            raise EnforcerError("boundary semantic operation is not allowed")

    def _transition_mutation(self, body: dict[str, Any]) -> dict[str, Any]:
        topology_resource, contract = self._assert_topology(body.get("topology"))
        target = body.get("target", {})
        if not isinstance(target, dict):
            raise EnforcerError("transition target evidence is missing")
        kind = target.get("kind")
        resource = {
            "ConfigMap": "configmap",
            "Lease": "lease",
            "NetworkPolicy": "networkpolicy",
        }.get(kind if isinstance(kind, str) else "")
        namespace = str(target.get("namespace", ""))
        name = str(target.get("name", ""))
        if resource is None or not namespace or not name:
            raise EnforcerError("transition target kind/name/namespace is invalid")
        allowed = {
            ("lease", STATE_NAME, RELEASE_NAMESPACE),
            ("configmap", STATE_NAME, RELEASE_NAMESPACE),
            ("networkpolicy", contract.get("policy_names", {}).get("proxy_guard"), contract.get("gateway_namespace")),
            (
                "networkpolicy",
                contract.get("policy_names", {}).get("controller_guard"),
                contract.get("controller_namespace"),
            ),
            ("networkpolicy", contract.get("policy_names", {}).get("default_deny"), contract.get("gateway_namespace")),
        }
        if (resource, name, namespace) not in allowed:
            raise EnforcerError("transition target is outside the protected allowlist")
        live = self.api.get(resource, name, namespace)
        self._assert_evidence(live, target, label="target")
        patch = body.get("patch")
        if (
            body.get("patch_sha256") != sha256_json(patch)
            or body.get("patch_type") not in {"json", "merge"}
            or not isinstance(body.get("dry_run"), bool)
        ):
            raise EnforcerError("transition patch encoding/hash is invalid")
        operation = str(body.get("operation", ""))
        if resource == "lease":
            if (
                set(body)
                != {
                    "operation",
                    "topology",
                    "target",
                    "patch_type",
                    "patch",
                    "patch_sha256",
                    "dry_run",
                }
                or body.get("patch_type") != "json"
            ):
                raise EnforcerError("Lease request fields are not exact")
            self._validate_lease_patch(operation, live, patch)
        else:
            if (
                set(body)
                != {
                    "operation",
                    "topology",
                    "target",
                    "lease",
                    "receipt",
                    "patch_type",
                    "patch",
                    "patch_sha256",
                    "dry_run",
                }
                or body.get("patch_type") != "merge"
            ):
                raise EnforcerError("protected mutation request fields are not exact")
            lease, receipt_object, receipt = self._live_transition_state(body)
            if resource == "configmap":
                self._validate_receipt_patch(live, receipt, lease, patch, contract, topology_resource)
            else:
                role = live.get("metadata", {}).get("labels", {}).get(ROLE_LABEL)
                self._assert_boundary(live, str(role))
                self._validate_policy_patch(operation, live, receipt, lease, patch, contract)
                if object_evidence(receipt_object) != {
                    key: body["receipt"].get(key)
                    for key in (
                        "api_version",
                        "kind",
                        "namespace",
                        "name",
                        "uid",
                        "resource_version",
                        "state_sha256",
                    )
                }:
                    raise EnforcerError("receipt changed during policy authorization")
        updated = self.api.patch(
            resource,
            name,
            namespace,
            str(body["patch_type"]),
            patch,
            dry_run=body.get("dry_run") is True,
        )
        before = object_evidence(live)
        after = object_evidence(updated)
        if (
            after["uid"] != before["uid"]
            or after["name"] != before["name"]
            or after["namespace"] != before["namespace"]
            or updated.get("metadata", {}).get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
        ):
            raise EnforcerError("protected mutation returned a different object")
        reread = self.api.get(resource, name, namespace)
        expected_reread = before if body.get("dry_run") is True else after
        if object_evidence(reread) != expected_reread:
            raise EnforcerError("protected mutation/dry-run was not confirmed by a live reread")
        return {"object": updated, "operation": operation, "dry_run": body.get("dry_run") is True}

    def _verify_recovery_approval(
        self,
        body: dict[str, Any],
        receipt: dict[str, Any],
        prior_recovery: dict[str, Any] | None,
        binding: dict[str, Any],
        parameter: dict[str, Any],
    ) -> dict[str, Any]:
        approval = body.get("approval")
        if not isinstance(approval, dict) or set(approval) != {"signed", "signature"}:
            raise EnforcerError("recovery requires a detached security approval")
        signed = approval.get("signed")
        if not isinstance(signed, dict):
            raise EnforcerError("recovery approval payload is missing")
        expected = {
            "schema",
            "mode",
            "recovery_reference",
            "cluster",
            "topology_uid",
            "topology_sha256",
            "receipt_uid",
            "receipt_resource_version",
            "receipt_sha256",
            "binding",
            "parameter",
            "issued_at",
            "expires_at",
            "signer_key_id",
        }
        try:
            self.recovery_public_key.verify(
                decode_base64url(approval.get("signature"), expected_bytes=64),
                canonical(signed).encode(),
            )
        except InvalidSignature as error:
            raise EnforcerError("recovery approval signature is invalid") from error
        now = dt.datetime.now(dt.UTC)
        issued = instant(signed.get("issued_at"), field="recovery approval issued_at")
        expires = instant(signed.get("expires_at"), field="recovery approval expires_at")
        receipt_evidence = body.get("receipt", {})
        common_invalid = (
            set(signed) != expected
            or signed.get("schema") != RECOVERY_APPROVAL_SCHEMA
            or signed.get("mode") != body.get("mode")
            or signed.get("recovery_reference") != body.get("recovery_reference")
            or signed.get("cluster") != self.expected_cluster
            or signed.get("topology_uid") != body.get("topology", {}).get("uid")
            or signed.get("topology_sha256") != body.get("topology", {}).get("sha256")
            or signed.get("receipt_uid") != receipt_evidence.get("uid")
            or signed.get("signer_key_id") != self.recovery_key_id
            or expires > issued + dt.timedelta(minutes=5)
        )
        approval_sha256 = sha256_json(approval)
        accepted = prior_recovery.get("authorization", {}) if prior_recovery else {}
        if accepted:
            # The exact approval accepted before the first mutation is the
            # durable crash-resume capability. Its original receipt binding
            # remains immutable while the receipt resourceVersion advances.
            binding_invalid = (
                prior_recovery.get("state") not in {"intent", "complete"}
                or accepted.get("approval_sha256") != approval_sha256
                or accepted.get("receipt_uid") != signed.get("receipt_uid")
                or accepted.get("receipt_resource_version") != signed.get("receipt_resource_version")
                or accepted.get("receipt_sha256") != signed.get("receipt_sha256")
                or accepted.get("binding") != signed.get("binding")
                or accepted.get("parameter") != signed.get("parameter")
                or accepted.get("issued_at") != signed.get("issued_at")
                or accepted.get("expires_at") != signed.get("expires_at")
                or accepted.get("signer_key_id") != signed.get("signer_key_id")
                or (prior_recovery.get("state") == "complete" and expires <= now)
            )
        else:
            binding_invalid = (
                signed.get("receipt_resource_version") != receipt_evidence.get("resource_version")
                or signed.get("receipt_sha256") != sha256_json(receipt)
                or signed.get("binding") != self._recovery_object_evidence(binding)
                or signed.get("parameter") != self._recovery_object_evidence(parameter)
                or issued < now - dt.timedelta(seconds=5)
                or issued > now + dt.timedelta(seconds=5)
                or expires < now
            )
        if common_invalid or binding_invalid:
            raise EnforcerError("recovery approval is stale or bound to different live state")
        return {
            "approval_sha256": approval_sha256,
            "receipt_uid": signed["receipt_uid"],
            "receipt_resource_version": signed["receipt_resource_version"],
            "receipt_sha256": signed["receipt_sha256"],
            "binding": signed["binding"],
            "parameter": signed["parameter"],
            "issued_at": signed["issued_at"],
            "expires_at": signed["expires_at"],
            "signer_key_id": signed["signer_key_id"],
        }

    def _patch_recovery_receipt(
        self,
        receipt_object: dict[str, Any],
        receipt: dict[str, Any],
        recovery: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        desired = {**receipt, "security_recovery": recovery}
        updated = self.api.patch(
            "configmap",
            STATE_NAME,
            RELEASE_NAMESPACE,
            "merge",
            {
                "metadata": {"resourceVersion": receipt_object["metadata"]["resourceVersion"]},
                "data": {"receipt.json": canonical(desired)},
            },
        )
        reread = self.api.get("configmap", STATE_NAME, RELEASE_NAMESPACE)
        if reread.get("metadata", {}).get("uid") != receipt_object.get("metadata", {}).get("uid") or object_evidence(
            reread
        ) != object_evidence(updated):
            raise EnforcerError("durable recovery receipt write was not confirmed by a live reread")
        return reread, receipt_from_object(reread)

    @staticmethod
    def _recovery_object_evidence(resource_object: dict[str, Any]) -> dict[str, Any]:
        evidence = object_evidence(resource_object)
        metadata = resource_object.get("metadata", {})
        evidence["labels"] = metadata.get("labels", {})
        evidence["annotations"] = metadata.get("annotations", {})
        evidence["spec"] = resource_object.get("spec")
        evidence["data"] = resource_object.get("data")
        return evidence

    @staticmethod
    def _recovery_content(evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            key: evidence.get(key)
            for key in (
                "api_version",
                "kind",
                "namespace",
                "name",
                "uid",
                "labels",
                "annotations",
                "spec",
                "data",
            )
        }

    @classmethod
    def _recovery_target(
        cls,
        before: dict[str, Any],
        *,
        desired_actions: list[str],
        mode: str,
    ) -> dict[str, dict[str, Any]]:
        binding = cls._recovery_content(cast(dict[str, Any], before["binding"]))
        parameter = cls._recovery_content(cast(dict[str, Any], before["parameter"]))
        binding_spec = dict(cast(dict[str, Any], binding["spec"]))
        binding_spec["validationActions"] = desired_actions
        parameter_data = dict(cast(dict[str, Any], parameter["data"]))
        parameter_data.update(mode=mode, delete_allowed="false")
        binding["spec"] = binding_spec
        parameter["data"] = parameter_data
        return {"binding": binding, "parameter": parameter}

    @classmethod
    def _recovery_state(
        cls,
        binding: dict[str, Any],
        parameter: dict[str, Any],
        recovery: dict[str, Any],
    ) -> str:
        before = cast(dict[str, Any], recovery.get("before", {}))
        target = cast(dict[str, Any], recovery.get("target", {}))
        live_binding = cls._recovery_content(cls._recovery_object_evidence(binding))
        live_parameter = cls._recovery_content(cls._recovery_object_evidence(parameter))
        before_binding = cls._recovery_content(cast(dict[str, Any], before.get("binding", {})))
        before_parameter = cls._recovery_content(cast(dict[str, Any], before.get("parameter", {})))
        target_binding = cast(dict[str, Any], target.get("binding", {}))
        target_parameter = cast(dict[str, Any], target.get("parameter", {}))
        pair = {"binding": live_binding, "parameter": live_parameter}
        if pair == {"binding": before_binding, "parameter": before_parameter}:
            return "before"
        if pair == {"binding": target_binding, "parameter": target_parameter}:
            return "target"
        intermediate = (
            {"binding": before_binding, "parameter": target_parameter}
            if recovery.get("mode") == "audit-warn"
            else {"binding": target_binding, "parameter": before_parameter}
        )
        if pair == intermediate:
            return "intermediate"
        raise EnforcerError("recovery objects drifted outside the approved before/intermediate/target states")

    def _recovery(self, request: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {
            "mode",
            "recovery_reference",
            "topology",
            "receipt",
            "lease",
            "binding",
            "parameter",
            "approval",
            "delete_allowed",
        }:
            raise EnforcerError("recovery request fields are not exact")
        if (
            body.get("mode") not in {"audit-warn", "deny"}
            or not re.fullmatch(r"(?:SEC|INC|CHG)-[1-9][0-9]{2,15}", str(body.get("recovery_reference", "")))
            or body.get("binding") != {"name": ADMISSION_NAME}
            or body.get("parameter") != {"namespace": RELEASE_NAMESPACE, "name": PARAMETER_NAME}
            or body.get("delete_allowed") is not False
        ):
            raise EnforcerError("recovery request is outside the reversible contract")
        _, contract = self._assert_topology(body.get("topology"))
        lease, receipt_object, receipt = self._live_transition_state(body)
        prior_recovery_value = receipt.get("security_recovery")
        prior_recovery = prior_recovery_value if isinstance(prior_recovery_value, dict) else None
        if prior_recovery and prior_recovery.get("state") == "intent" and (
            prior_recovery.get("mode") != body["mode"]
            or prior_recovery.get("recovery_reference") != body["recovery_reference"]
        ):
            raise EnforcerError("a different admission recovery is already in flight")
        same_recovery = (
            prior_recovery
            if prior_recovery
            and prior_recovery.get("state") in {"intent", "complete"}
            and prior_recovery.get("mode") == body["mode"]
            and prior_recovery.get("recovery_reference") == body["recovery_reference"]
            else None
        )
        binding = self.api.get("validatingadmissionpolicybinding", ADMISSION_NAME)
        parameter = self.api.get("configmap", PARAMETER_NAME, RELEASE_NAMESPACE)
        authorization = self._verify_recovery_approval(
            body,
            receipt,
            same_recovery,
            binding,
            parameter,
        )
        if (
            binding.get("metadata", {}).get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or binding.get("spec", {}).get("policyName") != ADMISSION_NAME
            or parameter.get("metadata", {}).get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or parameter.get("data", {}).get("delete_allowed") != "false"
            or parameter.get("data", {}).get("signer_key_id")
            != contract.get("security_handoff", {}).get("server_public_key_sha256")
            or parameter.get("data", {}).get("recovery_signer_key_id")
            != contract.get("security_handoff", {}).get("recovery_public_key_sha256")
        ):
            raise EnforcerError("recovery objects are not the exact permanent boundary")
        desired_actions = ["Audit", "Warn"] if body["mode"] == "audit-warn" else ["Deny"]
        if same_recovery and same_recovery.get("state") == "complete":
            after = same_recovery.get("after")
            if not isinstance(after, dict) or after != {
                "binding": self._recovery_object_evidence(binding),
                "parameter": self._recovery_object_evidence(parameter),
            }:
                raise EnforcerError("completed recovery is terminal and its exact live state has drifted")
            self._lease_is_fenced(lease, cast(dict[str, Any], body["lease"]))
            return {"binding": binding, "parameter": parameter, "receipt": receipt_object}
        if same_recovery and same_recovery.get("state") == "intent":
            pass
        else:
            before = {
                "binding": self._recovery_object_evidence(binding),
                "parameter": self._recovery_object_evidence(parameter),
            }
            recovery = {
                "schema": "fs2-serve.nebius.ai/network-policy-security-recovery/v1",
                "state": "intent",
                "operation_id": request["operation_id"],
                "mode": body["mode"],
                "recovery_reference": body["recovery_reference"],
                "topology": body["topology"],
                "authorization": authorization,
                "lease_fence": {
                    "holder_identity": lease.get("spec", {}).get("holderIdentity"),
                    "lease_transitions": lease.get("spec", {}).get("leaseTransitions"),
                },
                "before": before,
                "target": self._recovery_target(
                    before,
                    desired_actions=desired_actions,
                    mode=body["mode"],
                ),
            }
            if recovery["before"] != {
                "binding": authorization["binding"],
                "parameter": authorization["parameter"],
            }:
                raise EnforcerError("recovery intent differs from the security-approved old objects")
            receipt_object, receipt = self._patch_recovery_receipt(receipt_object, receipt, recovery)
        recovery = cast(dict[str, Any], receipt.get("security_recovery", {}))
        state = self._recovery_state(binding, parameter, recovery)
        if recovery.get("state") == "intent":
            recovery = {
                **recovery,
                "attempt": {
                    "operation_id": request["operation_id"],
                    "lease_fence": {
                        "holder_identity": lease.get("spec", {}).get("holderIdentity"),
                        "lease_transitions": lease.get("spec", {}).get("leaseTransitions"),
                    },
                    "receipt_resource_version": receipt_object.get("metadata", {}).get("resourceVersion"),
                },
            }
            receipt_object, receipt = self._patch_recovery_receipt(receipt_object, receipt, recovery)
        # Audit/Warn changes the parameter first; Deny changes the binding first.
        if body["mode"] == "audit-warn" and state == "before":
            parameter = self.api.patch(
                "configmap",
                PARAMETER_NAME,
                RELEASE_NAMESPACE,
                "merge",
                {
                    "metadata": {"resourceVersion": parameter["metadata"]["resourceVersion"]},
                    "data": {**parameter["data"], "mode": "audit-warn", "delete_allowed": "false"},
                },
            )
            binding = self.api.get("validatingadmissionpolicybinding", ADMISSION_NAME)
            parameter = self.api.get("configmap", PARAMETER_NAME, RELEASE_NAMESPACE)
            state = self._recovery_state(binding, parameter, recovery)
            if state != "intermediate":
                raise EnforcerError("Audit/Warn recovery did not reach its exact approved intermediate state")
        if state in {"before", "intermediate"} and binding.get("spec", {}).get("validationActions") != desired_actions:
            binding = self.api.patch(
                "validatingadmissionpolicybinding",
                ADMISSION_NAME,
                "",
                "merge",
                {
                    "metadata": {"resourceVersion": binding["metadata"]["resourceVersion"]},
                    "spec": {"validationActions": desired_actions},
                },
            )
            binding = self.api.get("validatingadmissionpolicybinding", ADMISSION_NAME)
            parameter = self.api.get("configmap", PARAMETER_NAME, RELEASE_NAMESPACE)
            state = self._recovery_state(binding, parameter, recovery)
            expected = "target" if body["mode"] == "audit-warn" else "intermediate"
            if state != expected:
                raise EnforcerError("recovery binding mutation did not reach its exact approved state")
        if body["mode"] == "deny" and state == "intermediate":
            parameter = self.api.patch(
                "configmap",
                PARAMETER_NAME,
                RELEASE_NAMESPACE,
                "merge",
                {
                    "metadata": {"resourceVersion": parameter["metadata"]["resourceVersion"]},
                    "data": {**parameter["data"], "mode": "deny", "delete_allowed": "false"},
                },
            )
        # Re-read all state after mutation; never trust patch return values alone.
        binding = self.api.get("validatingadmissionpolicybinding", ADMISSION_NAME)
        parameter = self.api.get("configmap", PARAMETER_NAME, RELEASE_NAMESPACE)
        receipt_object = self.api.get("configmap", STATE_NAME, RELEASE_NAMESPACE)
        receipt = receipt_from_object(receipt_object)
        live_lease = self.api.get("lease", STATE_NAME, RELEASE_NAMESPACE)
        recovery = cast(dict[str, Any], receipt.get("security_recovery", {}))
        if self._recovery_state(binding, parameter, recovery) != "target":
            raise EnforcerError("recovery did not reach the exact reversible target")
        self._lease_is_fenced(live_lease, cast(dict[str, Any], body["lease"]))
        if recovery.get("state") != "complete":
            recovery = {
                **recovery,
                "state": "complete",
                "after": {
                    "binding": self._recovery_object_evidence(binding),
                    "parameter": self._recovery_object_evidence(parameter),
                },
            }
            receipt_object, receipt = self._patch_recovery_receipt(receipt_object, receipt, recovery)
        final_receipt_object = self.api.get("configmap", STATE_NAME, RELEASE_NAMESPACE)
        final_receipt = receipt_from_object(final_receipt_object)
        final_recovery = final_receipt.get("security_recovery", {})
        if (
            final_receipt_object.get("metadata", {}).get("uid") != body.get("receipt", {}).get("uid")
            or final_recovery.get("state") != "complete"
            or final_recovery.get("mode") != body["mode"]
            or final_recovery.get("recovery_reference") != body["recovery_reference"]
            or final_recovery.get("after")
            != {
                "binding": self._recovery_object_evidence(binding),
                "parameter": self._recovery_object_evidence(parameter),
            }
        ):
            raise EnforcerError("durable recovery completion reread failed")
        return {"binding": binding, "parameter": parameter, "receipt": final_receipt_object}

    def handle_envelope(self, envelope: dict[str, Any], *, peer_uid: int, peer_gid: int) -> dict[str, Any]:
        request = self._verify_request(envelope, peer_uid, peer_gid)
        action = request["action"]
        body = cast(dict[str, Any], request["body"])
        if action == "attest":
            if body:
                raise EnforcerError("attestation body must be empty")
            topology, contract = self._topology()
            self._assert_topology(
                {
                    "namespace": RELEASE_NAMESPACE,
                    "name": TOPOLOGY_NAME,
                    "uid": topology.get("metadata", {}).get("uid"),
                    "resource_version": topology.get("metadata", {}).get("resourceVersion"),
                    "sha256": sha256_json(contract),
                }
            )
            result = {
                "schema": ATTESTATION_SCHEMA,
                "security_owner_username": contract.get("security_owner_username"),
                "allowed_actions": ["transition-mutation", "set-admission-recovery"],
                "recovery_modes": ["Audit", "Warn", "Deny"],
                "delete_allowed": False,
                "security_user_info_sha256": contract.get("security_handoff", {})
                .get("identity_boundary", {})
                .get("security_user_info_sha256"),
                "identity_epoch": contract.get("security_handoff", {})
                .get("identity_boundary", {})
                .get("identity_epoch"),
                "provider_subject_snapshot_sha256": contract.get("security_handoff", {})
                .get("identity_boundary", {})
                .get("provider_subject_snapshot_sha256"),
                "kubernetes_subject_inventory_sha256": contract.get("security_handoff", {})
                .get("identity_boundary", {})
                .get("kubernetes_subject_inventory_sha256"),
            }
        elif action == "transition-mutation":
            result = self._transition_mutation(body)
        else:
            result = self._recovery(request, body)
        return self._response(request, result)


def serve_connection(connection: socket.socket, enforcer: SecurityEnforcer) -> None:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, peer_uid, peer_gid = struct.unpack("3i", credentials)
    if peer_uid != enforcer.expected_peer_uid or peer_gid != enforcer.expected_peer_gid:
        raise EnforcerError("Unix peer UID/GID is not authorized")
    connection.settimeout(SOCKET_READ_SECONDS)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise EnforcerError("security handoff request exceeded its byte bound")
        chunks.append(chunk)
    try:
        envelope = json.loads(b"".join(chunks))
    except json.JSONDecodeError as error:
        raise EnforcerError("security handoff request is not valid JSON") from error
    if not isinstance(envelope, dict):
        raise EnforcerError("security handoff request is not an object")
    response = enforcer.handle_envelope(cast(dict[str, Any], envelope), peer_uid=peer_uid, peer_gid=peer_gid)
    connection.sendall((canonical(response) + "\n").encode())


def serve(socket_path: Path, enforcer: SecurityEnforcer, *, peer_gid: int) -> None:
    if not socket_path.is_absolute():
        raise EnforcerError("security handoff socket must be an absolute path")
    assert_absent_socket_path(socket_path)
    assert_security_owned_socket_parent(socket_path, peer_gid=peer_gid)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        socket_metadata = socket_path.lstat()
        if (
            not stat.S_ISSOCK(socket_metadata.st_mode)
            or socket_metadata.st_uid != os.geteuid()
            or socket_metadata.st_gid != peer_gid
        ):
            raise EnforcerError("security handoff socket did not inherit the exact setgid parent ownership")
        socket_path.chmod(0o660)
        socket_metadata = socket_path.lstat()
        if stat.S_IMODE(socket_metadata.st_mode) != 0o660:
            raise EnforcerError("security handoff socket mode is not exact")
        listener.listen(16)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    serve_connection(connection, enforcer)
                except (EnforcerError, OSError, UnicodeError, ValueError):
                    # Per-request fail-closed behavior must not stop the security service.
                    continue


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("serve", choices=("serve",))
    parser.add_argument("--socket", required=True)
    parser.add_argument("--security-kubeconfig", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--client-public-key", required=True)
    parser.add_argument("--server-private-key", required=True)
    parser.add_argument("--recovery-public-key", required=True)
    parser.add_argument("--peer-uid", required=True, type=int)
    parser.add_argument("--peer-gid", required=True, type=int)
    parser.add_argument("--api-server-sha256", required=True)
    parser.add_argument("--kube-system-uid", required=True)
    arguments = parser.parse_args(argv)
    for value in (arguments.client_public_key, arguments.recovery_public_key):
        decode_base64url(value, expected_bytes=32)
    if not re.fullmatch(r"[0-9a-f]{64}", arguments.api_server_sha256):
        parser.error("API server SHA-256 is invalid")
    if arguments.peer_uid < 1 or arguments.peer_gid < 1:
        parser.error("peer UID/GID must be positive")
    if arguments.peer_uid == os.geteuid():
        parser.error("security enforcer and rollout peer must use distinct Unix UIDs")
    if arguments.peer_gid in {os.getegid(), *os.getgroups()}:
        parser.error("security enforcer and rollout peer must use a dedicated disjoint Unix GID")
    for name in ("socket", "security_kubeconfig", "server_private_key"):
        if not Path(getattr(arguments, name)).is_absolute():
            parser.error(f"{name} path must be absolute")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv or sys.argv[1:])
    api = KubectlAPI(Path(arguments.security_kubeconfig), arguments.context)
    enforcer = SecurityEnforcer(
        api,
        client_public_key_value=arguments.client_public_key,
        server_private_key=load_private_key(Path(arguments.server_private_key)),
        recovery_public_key_value=arguments.recovery_public_key,
        expected_cluster={
            "api_server_sha256": arguments.api_server_sha256,
            "kube_system_uid": arguments.kube_system_uid,
        },
        expected_peer_uid=arguments.peer_uid,
        expected_peer_gid=arguments.peer_gid,
        expected_socket_path=Path(arguments.socket),
        security_kubeconfig_sha256=hashlib.sha256(Path(arguments.security_kubeconfig).read_bytes()).hexdigest(),
    )
    serve(Path(arguments.socket), enforcer, peer_gid=arguments.peer_gid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

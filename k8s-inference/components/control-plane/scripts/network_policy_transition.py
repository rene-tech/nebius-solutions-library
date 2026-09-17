#!/usr/bin/env python3
"""Crash-safe public-edge NetworkPolicy transition coordinator."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SCHEMA = "fs2-serve.nebius.ai/network-policy-transition-receipt/v3"
BOUNDARY_LABEL = "fs2.nebius.ai/network-policy-boundary"
BOUNDARY_VALUE = "permanent"
ROLE_LABEL = "fs2.nebius.ai/network-policy-role"
CANDIDATE_ANNOTATION = "fs2.nebius.ai/network-policy-candidate-sha256"
NORMAL_ANNOTATION = "fs2.nebius.ai/normal-policy-name"
DENY_ANNOTATION = "fs2.nebius.ai/deny-policy-name"
FENCE_ANNOTATION = "fs2.nebius.ai/network-policy-fence"
LEASE_NAME = "fs2-network-policy-transition"
RECEIPT_NAME = "fs2-network-policy-transition"
TOPOLOGY_NAME = "fs2-network-policy-boundary-topology"
LOCK_SECONDS = 60
LOCK_RENEW_SECONDS = 15
POD_PAGE_SIZE = 100
MAX_POD_PAGES = 10
MAX_HANDOFF_RESPONSE_BYTES = 1024 * 1024
HANDOFF_SECONDS = 30
REQUEST_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-handoff-request/v2"
RESPONSE_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-handoff-response/v2"
ATTESTATION_SCHEMA = "fs2-serve.nebius.ai/network-policy-security-automation-attestation/v2"
RELAXED_SELECTOR = {"fs2.nebius.ai/network-policy-deny-relaxed": "true"}
IN_FLIGHT_PHASES = {
    "bootstrap-relaxing",
    "bootstrap-ready",
    "bootstrap-guards-staging",
    "guards-staging",
    "guards-ready",
    "staged",
    "rollback-relaxing",
    "rollback-prepared",
    "rollback-guards-staging",
    "rollback-guards-ready",
    "destroy-relaxing",
    "destroy-prepared",
}


class TransitionError(RuntimeError):
    """A transition invariant failed closed."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def identity_instant(value: Any, *, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise TransitionError(f"{field} is not an RFC3339 instant")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TransitionError(f"{field} is not an RFC3339 instant") from error
    if parsed.tzinfo is None:
        raise TransitionError(f"{field} has no timezone")
    return parsed.astimezone(dt.UTC)


def normalized_user_info(response: dict[str, Any]) -> dict[str, Any]:
    try:
        info = response["status"]["userInfo"]
    except (KeyError, TypeError) as error:
        raise TransitionError("release kubeconfig has no exact whoami identity") from error
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
        raise TransitionError("release whoami identity is incomplete")
    return {
        "username": info["username"],
        "uid": info["uid"],
        "groups": sorted(info.get("groups", [])),
        "extra": {key: sorted(values) for key, values in sorted(info.get("extra", {}).items())},
    }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Command:
    def __init__(self, prefix: Sequence[str], *, name: str) -> None:
        self.prefix = list(prefix)
        self.name = name

    def run(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        result = subprocess.run(  # noqa: S603 - fixed tools and validated arguments
            [*self.prefix, *arguments],
            input=input_text,
            capture_output=True,
            check=False,
            text=True,
        )
        outcome = CommandResult(result.returncode, result.stdout, result.stderr)
        if check and result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise TransitionError(f"{self.name} command failed{suffix}")
        return outcome


def decode_base64url(value: Any, *, expected_bytes: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise TransitionError("signed handoff contains invalid base64url data")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as error:
        raise TransitionError("signed handoff contains invalid base64url data") from error
    if len(decoded) != expected_bytes:
        raise TransitionError("signed handoff has an invalid cryptographic length")
    return decoded


class SecurityHandoff:
    """Mutually authenticate a narrow security-automation request and response."""

    def __init__(
        self,
        socket_path: Path,
        server_public_key_value: str,
        client_private_key_path: Path,
        client_public_key_value: str,
        *,
        cluster: dict[str, str],
        release: dict[str, str],
    ) -> None:
        if not socket_path.is_absolute():
            raise TransitionError("security handoff socket path must be absolute")
        server_public_key_bytes = decode_base64url(server_public_key_value, expected_bytes=32)
        client_public_key_bytes = decode_base64url(client_public_key_value, expected_bytes=32)
        try:
            key_stat = client_private_key_path.lstat()
        except OSError as error:
            raise TransitionError("security handoff client signing key is unavailable") from error
        if (
            not stat.S_ISREG(key_stat.st_mode)
            or stat.S_ISLNK(key_stat.st_mode)
            or stat.S_IMODE(key_stat.st_mode) not in {0o400, 0o600}
            or key_stat.st_uid != os.geteuid()
        ):
            raise TransitionError("security handoff client signing key ownership or mode is unsafe")
        try:
            private_value = client_private_key_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as error:
            raise TransitionError("security handoff client signing key cannot be read safely") from error
        client_private_key = Ed25519PrivateKey.from_private_bytes(decode_base64url(private_value, expected_bytes=32))
        if client_private_key.public_key().public_bytes_raw() != client_public_key_bytes:
            raise TransitionError("security handoff client signing key does not match protected topology")
        self.socket_path = socket_path
        self.server_public_key = Ed25519PublicKey.from_public_bytes(server_public_key_bytes)
        self.server_key_id = sha256_text(server_public_key_value)
        self.client_private_key = client_private_key
        self.client_key_id = sha256_text(client_public_key_value)
        self.cluster = cluster
        self.release = release

    def _exchange(self, envelope: dict[str, Any]) -> dict[str, Any]:
        encoded = (canonical(envelope) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(HANDOFF_SECONDS)
                connection.connect(str(self.socket_path))
                connection.sendall(encoded)
                connection.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_HANDOFF_RESPONSE_BYTES:
                        raise TransitionError("security handoff response exceeded its byte bound")
                    chunks.append(chunk)
        except OSError as error:
            raise TransitionError("security handoff transport failed closed") from error
        try:
            response = json.loads(b"".join(chunks))
        except json.JSONDecodeError as error:
            raise TransitionError("security handoff response is not valid JSON") from error
        if not isinstance(response, dict):
            raise TransitionError("security handoff response is not an object")
        return cast(dict[str, Any], response)

    @staticmethod
    def _instant(value: Any, *, field: str) -> dt.datetime:
        if not isinstance(value, str):
            raise TransitionError(f"security handoff {field} is not an RFC3339 instant")
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise TransitionError(f"security handoff {field} is not an RFC3339 instant") from error
        if parsed.tzinfo is None:
            raise TransitionError(f"security handoff {field} has no timezone")
        return parsed.astimezone(dt.UTC)

    def request(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action not in {"attest", "transition-mutation", "set-admission-recovery"}:
            raise TransitionError("security handoff action is outside the narrow contract")
        issued = dt.datetime.now(dt.UTC)
        operation_id = str(uuid.uuid4())
        request = {
            "schema": REQUEST_SCHEMA,
            "operation_id": operation_id,
            "client_key_id": self.client_key_id,
            "action": action,
            "cluster": self.cluster,
            "release": self.release,
            "issued_at": issued.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "expires_at": (issued + dt.timedelta(seconds=HANDOFF_SECONDS))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "body": body,
        }
        request_signature = (
            base64.urlsafe_b64encode(self.client_private_key.sign(canonical(request).encode())).decode().rstrip("=")
        )
        response = self._exchange({"signed": request, "signature": request_signature})
        if set(response) != {"signed", "signature"} or not isinstance(response.get("signed"), dict):
            raise TransitionError("security handoff response envelope is not exact")
        signed = cast(dict[str, Any], response["signed"])
        expected_fields = {
            "schema",
            "operation_id",
            "request_sha256",
            "cluster",
            "issued_at",
            "expires_at",
            "status",
            "signer_key_id",
            "result",
        }
        if set(signed) != expected_fields:
            raise TransitionError("security handoff signed payload fields are not exact")
        try:
            self.server_public_key.verify(
                decode_base64url(response["signature"], expected_bytes=64),
                canonical(signed).encode(),
            )
        except InvalidSignature as error:
            raise TransitionError("security handoff signature is invalid") from error
        now = dt.datetime.now(dt.UTC)
        response_issued = self._instant(signed["issued_at"], field="issued_at")
        response_expires = self._instant(signed["expires_at"], field="expires_at")
        if (
            signed["schema"] != RESPONSE_SCHEMA
            or signed["operation_id"] != operation_id
            or signed["request_sha256"] != sha256_json(request)
            or signed["cluster"] != self.cluster
            or signed["signer_key_id"] != self.server_key_id
            or signed["status"] != "approved"
            or response_issued < issued - dt.timedelta(seconds=5)
            or response_issued > now + dt.timedelta(seconds=5)
            or response_expires < now
            or response_expires > response_issued + dt.timedelta(seconds=HANDOFF_SECONDS)
        ):
            raise TransitionError("security handoff response is stale or bound to a different request")
        result = signed["result"]
        if not isinstance(result, dict):
            raise TransitionError("security handoff result is not an object")
        return cast(dict[str, Any], result)


class ProtectedCommand:
    """Read through ordinary RBAC; route exact patches through signed handoff."""

    def __init__(
        self,
        ordinary: Command,
        handoff: SecurityHandoff,
        *,
        allowed_patch_targets: set[tuple[str, str, str]],
        topology: dict[str, Any],
    ) -> None:
        self.ordinary = ordinary
        self.handoff = handoff
        self.allowed_patch_targets = allowed_patch_targets
        self.topology = topology

    @staticmethod
    def _object_evidence(resource_object: dict[str, Any]) -> dict[str, Any]:
        metadata = resource_object.get("metadata", {})
        if not all(
            isinstance(metadata.get(field), str) and metadata.get(field)
            for field in ("name", "namespace", "uid", "resourceVersion")
        ):
            raise TransitionError("protected object has incomplete live identity")
        return {
            "api_version": resource_object.get("apiVersion"),
            "kind": resource_object.get("kind"),
            "namespace": metadata["namespace"],
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

    def _get_object(self, resource: str, name: str, namespace: str) -> dict[str, Any]:
        result = self.ordinary.run("get", resource, name, "--namespace", namespace, "-o", "json")
        try:
            resource_object = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise TransitionError("protected live evidence is not valid JSON") from error
        if not isinstance(resource_object, dict):
            raise TransitionError("protected live evidence is not an object")
        return cast(dict[str, Any], resource_object)

    @staticmethod
    def _semantic_operation(resource: str, name: str, patch: Any, topology: dict[str, Any]) -> str:
        if resource == "lease" and isinstance(patch, list):
            additions = {item.get("path"): item.get("value") for item in patch if item.get("op") == "add"}
            if additions.get("/spec/holderIdentity") == "":
                return "lease-release"
            if "/spec/leaseTransitions" in additions and additions.get("/spec/holderIdentity"):
                return "lease-acquire"
            return "lease-renew"
        if resource == "configmap" and name == RECEIPT_NAME and isinstance(patch, dict):
            return "receipt-write"
        policy_names = topology.get("contract", {}).get("policy_names", {})
        if resource == "networkpolicy" and isinstance(patch, dict):
            if name in {policy_names.get("proxy_guard"), policy_names.get("controller_guard")}:
                return "guard-stage"
            if name == policy_names.get("default_deny"):
                selector = patch.get("spec", {}).get("podSelector")
                if selector == {"matchLabels": RELAXED_SELECTOR}:
                    return "deny-relax"
                if selector == {}:
                    return "deny-activate"
        raise TransitionError("protected patch has no allowlisted semantic transition")

    def run(
        self,
        *arguments: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        if not arguments:
            raise TransitionError("empty protected Kubernetes command")
        if arguments[0] == "get":
            return self.ordinary.run(*arguments, input_text=input_text, check=check)
        if arguments[0] != "patch" or input_text is not None:
            raise TransitionError("protected mutations require an exact signed patch handoff")
        if len(arguments) < 3:
            raise TransitionError("protected patch identity is incomplete")
        resource, name = arguments[1:3]
        namespace = ""
        patch_type = ""
        patch = ""
        dry_run = False
        index = 3
        while index < len(arguments):
            item = arguments[index]
            if item == "--namespace" and index + 1 < len(arguments):
                namespace = arguments[index + 1]
                index += 2
            elif item.startswith("--type="):
                patch_type = item.removeprefix("--type=")
                index += 1
            elif item == "--patch" and index + 1 < len(arguments):
                patch = arguments[index + 1]
                index += 2
            elif item == "--dry-run=server":
                dry_run = True
                index += 1
            elif item == "-o" and index + 1 < len(arguments) and arguments[index + 1] == "json":
                index += 2
            else:
                raise TransitionError("protected patch contains an unsupported kubectl argument")
        if not namespace or patch_type not in {"json", "merge"} or not patch:
            raise TransitionError("protected patch is missing an exact namespace, type, or payload")
        if (resource, name, namespace) not in self.allowed_patch_targets:
            raise TransitionError("protected patch target is outside the signed handoff allowlist")
        try:
            parsed_patch = json.loads(patch)
        except json.JSONDecodeError as error:
            raise TransitionError("protected patch payload is not valid JSON") from error
        target_object = self._get_object(resource, name, namespace)
        operation = self._semantic_operation(resource, name, parsed_patch, self.topology)
        topology_resource = self.topology["resource"]
        topology_metadata = topology_resource.get("metadata", {})
        body: dict[str, Any] = {
            "operation": operation,
            "topology": {
                "namespace": topology_metadata.get("namespace"),
                "name": topology_metadata.get("name"),
                "uid": topology_metadata.get("uid"),
                "resource_version": topology_metadata.get("resourceVersion"),
                "sha256": sha256_json(self.topology["contract"]),
            },
            "target": self._object_evidence(target_object),
            "patch_type": patch_type,
            "patch": parsed_patch,
            "patch_sha256": sha256_json(parsed_patch),
            "dry_run": dry_run,
        }
        if resource != "lease":
            lease = self._get_object("lease", LEASE_NAME, "fs2-system")
            receipt_object = (
                target_object
                if resource == "configmap" and name == RECEIPT_NAME
                else self._get_object("configmap", RECEIPT_NAME, "fs2-system")
            )
            try:
                receipt = json.loads(receipt_object.get("data", {}).get("receipt.json", ""))
            except json.JSONDecodeError as error:
                raise TransitionError("protected receipt evidence is not valid JSON") from error
            body["lease"] = self._object_evidence(lease)
            body["lease"]["holder_identity"] = lease.get("spec", {}).get("holderIdentity")
            body["lease"]["lease_transitions"] = lease.get("spec", {}).get("leaseTransitions")
            body["receipt"] = self._object_evidence(receipt_object)
            body["receipt"]["receipt_sha256"] = sha256_json(receipt)
            body["receipt"]["phase"] = receipt.get("phase")
            body["receipt"]["intent"] = receipt.get("intent")
        result = self.handoff.request(
            "transition-mutation",
            body,
        )
        resource_object = result.get("object")
        if not isinstance(resource_object, dict):
            raise TransitionError("signed patch handoff returned no Kubernetes object")
        expected_type = {
            "configmap": ("v1", "ConfigMap"),
            "lease": ("coordination.k8s.io/v1", "Lease"),
            "networkpolicy": ("networking.k8s.io/v1", "NetworkPolicy"),
        }.get(resource)
        metadata = resource_object.get("metadata", {})
        if (
            expected_type is None
            or (resource_object.get("apiVersion"), resource_object.get("kind")) != expected_type
            or metadata.get("name") != name
            or metadata.get("namespace") != namespace
            or not metadata.get("uid")
            or not metadata.get("resourceVersion")
            or metadata.get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
        ):
            raise TransitionError("signed patch handoff returned a different protected object")
        return CommandResult(returncode=0, stdout=canonical(resource_object), stderr="")


@dataclass
class Candidate:
    candidate_sha256: str
    chart_sha256: str
    values_sha256: str
    complete_render_sha256: str
    network_policy_render_sha256: str
    release: dict[str, Any]
    topology: dict[str, Any]
    proxy: dict[str, Any]
    controller: dict[str, Any]
    deny_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_sha256": self.candidate_sha256,
            "chart_sha256": self.chart_sha256,
            "values_sha256": self.values_sha256,
            "complete_render_sha256": self.complete_render_sha256,
            "network_policy_render_sha256": self.network_policy_render_sha256,
            "release": self.release,
            "topology": self.topology,
            "policies": {
                "public-envoy": self.proxy,
                "envoy-controller": self.controller,
            },
            "deny_name": self.deny_name,
        }


class Transition:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.release = arguments.release
        self.release_namespace = arguments.release_namespace
        self.chart = Path(arguments.chart).resolve()
        self.kubeconfig = Path(arguments.kubeconfig).resolve()
        self.security_handoff_socket = Path(arguments.security_handoff_socket)
        self.security_handoff_server_public_key = arguments.security_handoff_server_public_key
        self.security_handoff_client_public_key = arguments.security_handoff_client_public_key
        self.security_handoff_client_private_key = Path(arguments.security_handoff_client_private_key)
        self.tempdir = Path(tempfile.mkdtemp(prefix="fs2-network-policy-transition."))
        self.holder = f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4()}"
        self.bootstrap_kubectl = Command(self._kubectl_prefix(self.kubeconfig), name="kubectl")
        helm_prefix = ["helm", "--kubeconfig", str(self.kubeconfig)]
        if arguments.context:
            helm_prefix.extend(["--kube-context", arguments.context])
        self.helm = Command(helm_prefix, name="helm")
        self.value_sources: list[dict[str, str]] = []
        self.helm_values = self._helm_values()
        self.security_handoff: SecurityHandoff | None = None
        self.guarded_kubectl: Command | ProtectedCommand | None = None
        self.fence_transitions: int | None = None

    def close(self) -> None:
        shutil.rmtree(self.tempdir)

    def _kubectl_prefix(self, kubeconfig: Path) -> list[str]:
        prefix = ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=30s"]
        if self.arguments.context:
            prefix.extend(["--context", self.arguments.context])
        return prefix

    def _helm_values(self) -> list[str]:
        result: list[str] = []
        for path_value in self.arguments.values:
            path = Path(path_value)
            if not path.is_file():
                raise TransitionError(f"values file does not exist: {path}")
            result.extend(["--values", str(path.resolve())])
            self.value_sources.append(
                {"kind": "file", "name": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
        for index, name in enumerate(self.arguments.values_env):
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name not in os.environ:
                raise TransitionError("values environment name is invalid or unset")
            path = self.tempdir / f"values-{index}.yaml"
            path.write_text(os.environ[name] + "\n", encoding="utf-8")
            result.extend(["--values", str(path)])
            self.value_sources.append(
                {"kind": "environment", "name": name, "sha256": sha256_text(os.environ[name] + "\n")}
            )
        passthrough = list(self.arguments.helm_value_args)
        if passthrough[:1] == ["--"]:
            passthrough.pop(0)
        result.extend(passthrough)
        self.value_sources.append({"kind": "helm-arguments", "name": "remainder", "sha256": sha256_json(passthrough)})
        return result

    def configure_guarded_client(self) -> None:
        topology = self._protected_topology(self.bootstrap_kubectl)
        cluster_server, kube_system_uid = self._cluster_identity(self.bootstrap_kubectl)
        contract = topology["contract"]
        handoff_contract = contract.get("security_handoff", {})
        auditor_bootstrap = handoff_contract.get("auditor_bootstrap", {})
        cluster = {
            "api_server_sha256": sha256_text(cluster_server),
            "kube_system_uid": kube_system_uid,
        }
        identity_boundary = handoff_contract.get("identity_boundary", {})
        epoch = str(identity_boundary.get("identity_epoch", ""))
        prior_epoch = str(identity_boundary.get("prior_identity_epoch", ""))
        successor_epoch = str(identity_boundary.get("successor_identity_epoch", ""))

        def epoch_principal(role: str, value: str) -> str:
            return f"fs2-np-{role}-{hashlib.sha256(value.encode()).hexdigest()[:16]}"

        expected_principals = {
            "release": epoch_principal("release", epoch),
            "security_owner": epoch_principal("security-owner", epoch),
            "security_bootstrap": epoch_principal("security-bootstrap", epoch),
            "prior_owner": epoch_principal("security-owner", prior_epoch),
            "prior_bootstrap": epoch_principal("security-bootstrap", prior_epoch),
            "successor_owner": epoch_principal("security-owner", successor_epoch),
            "successor_bootstrap": epoch_principal("security-bootstrap", successor_epoch),
        }
        try:
            release_whoami = normalized_user_info(
                cast(dict[str, Any], json.loads(self.bootstrap_kubectl.run("auth", "whoami", "-o", "json").stdout))
            )
        except json.JSONDecodeError as error:
            raise TransitionError("release whoami is not valid JSON") from error
        now = dt.datetime.now(dt.UTC)
        if (
            handoff_contract.get("schema") != "fs2-serve.nebius.ai/network-policy-security-handoff/v2"
            or handoff_contract.get("socket_path") != str(self.security_handoff_socket)
            or handoff_contract.get("server_public_key") != self.security_handoff_server_public_key
            or handoff_contract.get("server_public_key_sha256") != sha256_text(self.security_handoff_server_public_key)
            or handoff_contract.get("client_public_key") != self.security_handoff_client_public_key
            or handoff_contract.get("client_public_key_sha256") != sha256_text(self.security_handoff_client_public_key)
            or handoff_contract.get("client_private_key_path") != str(self.security_handoff_client_private_key)
            or not re.fullmatch(r"[0-9a-f]{64}", str(handoff_contract.get("recovery_public_key_sha256", "")))
            or handoff_contract.get("peer_uid") != os.geteuid()
            or handoff_contract.get("peer_gid") != os.getegid()
            or handoff_contract.get("peer_gid_contract") != "effective-dedicated"
            or handoff_contract.get("socket_directory_contract") != "precreated-setgid-02710"
            or handoff_contract.get("cluster") != cluster
            or handoff_contract.get("allowed_actions") != ["transition-mutation", "set-admission-recovery"]
            or handoff_contract.get("delete_allowed") is not False
            or auditor_bootstrap
            != {
                "mechanism": "external-preprovision-declarative-import",
                "cluster_role": "fs2-network-policy-security-auditor",
                "cluster_role_binding": "fs2-network-policy-security-auditor",
                "bootstrap_cluster_role": "fs2-network-policy-security-bootstrap",
                "bootstrap_namespaced_roles": {
                    "state": "fs2-network-policy-transition-bootstrap",
                    "gateway": "fs2-network-policy-transition-gateway-bootstrap",
                    "controller": "fs2-network-policy-transition-controller-bootstrap",
                },
                "immutable_role_bundle_sha256": identity_boundary.get("external_role_bundle_sha256"),
                "preapply_subjects": [
                    expected_principals["prior_bootstrap"],
                    expected_principals["security_bootstrap"],
                ],
                "desired_subjects": [
                    expected_principals["security_bootstrap"],
                    expected_principals["successor_bootstrap"],
                ],
                "observed_sha256": identity_boundary.get("auditor_bootstrap_sha256"),
                "bootstrap_create": False,
                "bootstrap_exact_update": True,
                "delete_allowed": False,
            }
            or handoff_contract.get("recovery_modes") != ["Audit", "Warn", "Deny"]
            or identity_boundary.get("schema")
            != "fs2-serve.nebius.ai/network-policy-identity-boundary/v3"
            or any(
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", value)
                for value in (prior_epoch, epoch, successor_epoch)
            )
            or len({prior_epoch, epoch, successor_epoch}) != 3
            or identity_boundary.get("epoch_principals") != expected_principals
            or contract.get("security_owner_username") != expected_principals["security_owner"]
            or contract.get("security_bootstrap_username") != expected_principals["security_bootstrap"]
            or contract.get("successor_security_bootstrap_username") != expected_principals["successor_bootstrap"]
            or contract.get("successor_security_owner_username") != expected_principals["successor_owner"]
            or release_whoami.get("username") != expected_principals["release"]
            or Path(str(handoff_contract.get("socket_path", ""))).parent.name
            != identity_boundary.get("identity_epoch")
            or identity_boundary.get("release_user_info_sha256") != sha256_json(release_whoami)
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity_boundary.get("credential_set_sha256", "")))
            or identity_boundary.get("release_kubeconfig_sha256")
            != hashlib.sha256(self.kubeconfig.read_bytes()).hexdigest()
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("prior_security_kubeconfig_sha256", ""))
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("prior_bootstrap_kubeconfig_sha256", ""))
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("security_subject_inventory_sha256", ""))
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("provider_subject_snapshot_sha256", ""))
            )
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("provider_trust_anchor_sha256", ""))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity_boundary.get("provider_adapter_sha256", "")))
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("kubernetes_subject_inventory_sha256", ""))
            )
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity_boundary.get("auditor_bootstrap_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity_boundary.get("external_role_bundle_sha256", "")))
            or identity_boundary.get("plan_rotation_phase") not in {"preapply", "resume", "postapply"}
            or not re.fullmatch(
                r"[0-9a-f]{64}", str(identity_boundary.get("rotation_binding_state_sha256", ""))
            )
            or identity_boundary.get("plan_preflight_verified") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(identity_boundary.get("plan_preflight_sha256", "")))
            or identity_boundary.get("bootstrap_must_be_expired") is not True
            or identity_instant(identity_boundary.get("bootstrap_expires_at"), field="bootstrap identity expires_at")
            > now
            or identity_instant(identity_boundary.get("release_expires_at"), field="release identity expires_at")
            <= now
            or identity_instant(identity_boundary.get("rollback_valid_until"), field="rollback validity")
            < now
            + dt.timedelta(seconds=int(identity_boundary.get("minimum_rollback_seconds", 0)))
            or int(identity_boundary.get("minimum_rollback_seconds", 0)) < 3600
            or identity_boundary.get("rotation_contract")
            != {
                "mechanism": "preauthorized-successor-epoch",
                "bootstrap_update_identity": expected_principals["security_bootstrap"],
                "successor_security_owner_identity": expected_principals["successor_owner"],
                "successor_bootstrap_identity": expected_principals["successor_bootstrap"],
                "prior_security_owner_identity": expected_principals["prior_owner"],
                "prior_bootstrap_identity": expected_principals["prior_bootstrap"],
                "new_paths_required": True,
                "allowed_plan_phases": ["preapply", "resume", "postapply"],
                "mutation_binding_states": ["before", "target"],
                "fresh_preapply_prior_owner_authorized": True,
                "prior_bootstrap_protected_mutation_denied": True,
                "postapply_retirement_required": True,
            }
        ):
            raise TransitionError("signed handoff does not match protected same-cluster topology")
        self.verify_external_iam_boundary(contract)
        handoff = SecurityHandoff(
            self.security_handoff_socket,
            self.security_handoff_server_public_key,
            self.security_handoff_client_private_key,
            self.security_handoff_client_public_key,
            cluster=cluster,
            release={"name": self.release, "namespace": self.release_namespace},
        )
        attestation = handoff.request("attest", {})
        if attestation != {
            "schema": ATTESTATION_SCHEMA,
            "security_owner_username": contract.get("security_owner_username"),
            "allowed_actions": ["transition-mutation", "set-admission-recovery"],
            "recovery_modes": ["Audit", "Warn", "Deny"],
            "delete_allowed": False,
            "security_user_info_sha256": identity_boundary.get("security_user_info_sha256"),
            "identity_epoch": epoch,
            "provider_subject_snapshot_sha256": identity_boundary.get("provider_subject_snapshot_sha256"),
            "provider_trust_anchor_sha256": identity_boundary.get("provider_trust_anchor_sha256"),
            "provider_adapter_sha256": identity_boundary.get("provider_adapter_sha256"),
            "kubernetes_subject_inventory_sha256": identity_boundary.get("kubernetes_subject_inventory_sha256"),
            "auditor_bootstrap_sha256": identity_boundary.get("auditor_bootstrap_sha256"),
            "external_role_bundle_sha256": identity_boundary.get("external_role_bundle_sha256"),
            "plan_rotation_phase": identity_boundary.get("plan_rotation_phase"),
            "rotation_binding_state_sha256": identity_boundary.get("rotation_binding_state_sha256"),
        }:
            raise TransitionError("security automation attestation is not the exact narrow contract")
        self.security_handoff = handoff
        policy_names = contract["policy_names"]
        self.guarded_kubectl = ProtectedCommand(
            self.bootstrap_kubectl,
            handoff,
            allowed_patch_targets={
                ("lease", LEASE_NAME, self.release_namespace),
                ("configmap", RECEIPT_NAME, self.release_namespace),
                ("networkpolicy", policy_names["proxy_guard"], contract["gateway_namespace"]),
                ("networkpolicy", policy_names["default_deny"], contract["gateway_namespace"]),
                ("networkpolicy", policy_names["controller_guard"], contract["controller_namespace"]),
            },
            topology=topology,
        )

    @staticmethod
    def _cluster_identity(command: Command) -> tuple[str, str]:
        raw_config = command.run("config", "view", "--minify", "--raw", "-o", "json").stdout
        try:
            clusters = json.loads(raw_config)["clusters"]
            if len(clusters) != 1:
                raise TransitionError("kubeconfig does not select exactly one cluster")
            server = clusters[0]["cluster"]["server"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("could not derive the selected Kubernetes API server") from error
        uid = command.run("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").stdout.strip()
        if not isinstance(server, str) or not server or not uid:
            raise TransitionError("kubeconfig cluster identity is incomplete")
        return server, uid

    @staticmethod
    def _require_can_i(command: Command, expected: str, *arguments: str) -> None:
        result = command.run("auth", "can-i", *arguments, check=False)
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise TransitionError("Kubernetes authorization does not match the external security boundary")

    @staticmethod
    def _protected_topology(command: Command) -> dict[str, Any]:
        result = command.run("get", "configmap", TOPOLOGY_NAME, "--namespace", "fs2-system", "-o", "json")
        resource = cast(dict[str, Any], json.loads(result.stdout))
        try:
            contract = json.loads(resource["data"]["topology.json"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("protected topology has no security boundary contract") from error
        return {"resource": resource, "contract": contract}

    def verify_external_iam_boundary(self, topology: dict[str, Any]) -> None:
        """Fail closed unless the rollout identity is outside security ownership."""
        security_owner = topology.get("security_owner_username")
        if not isinstance(security_owner, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{3,253}", security_owner):
            raise TransitionError("protected topology has an invalid external security-owner identity")
        protected = (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
        for resource in protected:
            for verb in ("patch", "update", "delete"):
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    resource,
                    "--resource-name=fs2-network-policy-boundary",
                )
            self._require_can_i(self.bootstrap_kubectl, "no", "deletecollection", resource)
        policy_names = topology.get("policy_names", {})
        exact_namespaced = (
            ("networkpolicies.networking.k8s.io", policy_names.get("proxy_guard"), topology.get("gateway_namespace")),
            ("networkpolicies.networking.k8s.io", policy_names.get("default_deny"), topology.get("gateway_namespace")),
            (
                "networkpolicies.networking.k8s.io",
                policy_names.get("controller_guard"),
                topology.get("controller_namespace"),
            ),
            ("configmaps", RECEIPT_NAME, self.release_namespace),
            ("configmaps", TOPOLOGY_NAME, self.release_namespace),
            ("configmaps", "fs2-network-policy-boundary-parameters", self.release_namespace),
            ("leases.coordination.k8s.io", LEASE_NAME, self.release_namespace),
        )
        for resource, name, namespace in exact_namespaced:
            if not isinstance(name, str) or not name or not isinstance(namespace, str) or not namespace:
                raise TransitionError("protected topology has an incomplete exact object identity")
            for verb in ("patch", "update", "delete"):
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
            self._require_can_i(
                self.bootstrap_kubectl,
                "no",
                "deletecollection",
                resource,
                "--namespace",
                namespace,
            )
        impersonation_targets = (
            ("users", security_owner),
            ("users", "fs2-network-policy-security-probe"),
            ("users", None),
            ("groups", None),
            ("groups", "system:authenticated"),
            ("groups", "system:serviceaccounts"),
            ("groups", f"system:serviceaccounts:{self.release_namespace}"),
            ("groups", "fs2-network-policy-security-probe"),
            ("serviceaccounts", None),
            (
                "serviceaccounts",
                f"{self.release_namespace}:fs2-network-policy-security-probe",
            ),
            ("uids.authentication.k8s.io", None),
            ("uids.authentication.k8s.io", str(topology.get("security_handoff", {}).get("peer_uid", ""))),
            ("uids.authentication.k8s.io", "00000000-0000-4000-8000-000000000000"),
            ("userextras.authentication.k8s.io", None),
            ("userextras.authentication.k8s.io", "scopes"),
            ("userextras.authentication.k8s.io", "fs2.nebius.ai/security-probe"),
            ("groups", "system:masters"),
            (
                "serviceaccounts",
                f"system:serviceaccount:{self.release_namespace}:fs2-network-policy-transition",
            ),
        )
        for resource, name in impersonation_targets:
            exact_name = (f"--resource-name={name}",) if name else ()
            self._require_can_i(
                self.bootstrap_kubectl,
                "no",
                "impersonate",
                resource,
                *exact_name,
            )
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "create",
            "certificatesigningrequests.certificates.k8s.io",
        )
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "update",
            "certificatesigningrequests.certificates.k8s.io",
            "--subresource=approval",
        )
        for signer in (
            "kubernetes.io/kube-apiserver-client",
            "kubernetes.io/kube-apiserver-client-kubelet",
            "kubernetes.io/legacy-unknown",
        ):
            for verb in ("approve", "sign"):
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    "signers.certificates.k8s.io",
                    f"--resource-name={signer}",
                )
        rbac_targets = (
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-owner", ""),
            ("clusterrolebindings.rbac.authorization.k8s.io", "fs2-network-policy-security-owner", ""),
            ("clusterroles.rbac.authorization.k8s.io", "fs2-network-policy-security-auditor", ""),
            ("clusterrolebindings.rbac.authorization.k8s.io", "fs2-network-policy-security-auditor", ""),
            ("roles.rbac.authorization.k8s.io", RECEIPT_NAME, self.release_namespace),
            ("rolebindings.rbac.authorization.k8s.io", RECEIPT_NAME, self.release_namespace),
            (
                "roles.rbac.authorization.k8s.io",
                f"{RECEIPT_NAME}-gateway",
                str(topology.get("gateway_namespace", "")),
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                f"{RECEIPT_NAME}-gateway",
                str(topology.get("gateway_namespace", "")),
            ),
            (
                "roles.rbac.authorization.k8s.io",
                f"{RECEIPT_NAME}-controller",
                str(topology.get("controller_namespace", "")),
            ),
            (
                "rolebindings.rbac.authorization.k8s.io",
                f"{RECEIPT_NAME}-controller",
                str(topology.get("controller_namespace", "")),
            ),
        )
        for resource, name, namespace in rbac_targets:
            suffix = ("--namespace", namespace) if namespace else ()
            for verb in ("create", "patch", "update", "delete", "bind", "escalate"):
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    resource,
                    *suffix,
                )
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    *suffix,
                )
            self._require_can_i(
                self.bootstrap_kubectl,
                "no",
                "deletecollection",
                resource,
                *suffix,
            )
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "create",
            "serviceaccounts",
            "--resource-name=fs2-network-policy-transition",
            "--subresource=token",
            "--namespace",
            self.release_namespace,
        )
        self._require_can_i(
            self.bootstrap_kubectl,
            "no",
            "create",
            "serviceaccounts",
            "--subresource=token",
            "--all-namespaces",
        )
        for namespace in {
            self.release_namespace,
            str(topology.get("gateway_namespace", "")),
            str(topology.get("controller_namespace", "")),
        }:
            if not namespace:
                raise TransitionError("protected topology has an incomplete namespace identity")
            self._require_can_i(
                self.bootstrap_kubectl,
                "no",
                "create",
                "serviceaccounts",
                "--subresource=token",
                "--namespace",
                namespace,
            )
            for verb in ("update", "delete"):
                self._require_can_i(
                    self.bootstrap_kubectl,
                    "no",
                    verb,
                    "namespaces",
                    f"--resource-name={namespace}",
                )
            self._require_can_i(
                self.bootstrap_kubectl,
                "no",
                "update",
                "namespaces/finalize",
                f"--resource-name={namespace}",
            )

    @property
    def guarded(self) -> Command | ProtectedCommand:
        if self.guarded_kubectl is None:
            raise TransitionError("guarded Kubernetes client is not configured")
        return self.guarded_kubectl

    @property
    def handoff(self) -> SecurityHandoff:
        if self.security_handoff is None:
            raise TransitionError("signed security handoff is not configured")
        return self.security_handoff

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self._acquire_lock()
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            body_error = error
            raise
        finally:
            try:
                self._release_lock()
            except TransitionError:
                if body_error is None:
                    raise

    def _lease(self) -> dict[str, Any]:
        result = self.guarded.run("get", "lease", LEASE_NAME, "--namespace", self.release_namespace, "-o", "json")
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def _lease_expired(lease: dict[str, Any]) -> bool:
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity", "")
        if not holder:
            return True
        renew = spec.get("renewTime")
        duration = int(spec.get("leaseDurationSeconds", 0))
        if not renew or duration <= 0:
            return False
        renewed = dt.datetime.fromisoformat(renew.replace("Z", "+00:00"))
        return dt.datetime.now(dt.UTC) >= renewed + dt.timedelta(seconds=duration)

    def _acquire_lock(self) -> None:
        for _ in range(3):
            lease = self._lease()
            holder = lease.get("spec", {}).get("holderIdentity", "")
            if holder and holder != self.holder and not self._lease_expired(lease):
                raise TransitionError("another NetworkPolicy transition holds the Lease")
            patch = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": lease["metadata"]["resourceVersion"],
                },
                {"op": "add", "path": "/spec/holderIdentity", "value": self.holder},
                {"op": "add", "path": "/spec/leaseDurationSeconds", "value": LOCK_SECONDS},
                {
                    "op": "add",
                    "path": "/spec/leaseTransitions",
                    "value": int(lease.get("spec", {}).get("leaseTransitions", 0)) + 1,
                },
                {
                    "op": "add",
                    "path": "/spec/renewTime",
                    "value": dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                },
            ]
            result = self.guarded.run(
                "patch",
                "lease",
                LEASE_NAME,
                "--namespace",
                self.release_namespace,
                "--type=json",
                "--patch",
                canonical(patch),
                "-o",
                "json",
                check=False,
            )
            if result.returncode == 0:
                updated = json.loads(result.stdout)
                if updated.get("spec", {}).get("holderIdentity") != self.holder:
                    raise TransitionError("Lease acquisition did not preserve holder identity")
                self.fence_transitions = int(updated.get("spec", {}).get("leaseTransitions", -1))
                return
        raise TransitionError("could not acquire the NetworkPolicy transition Lease")

    def _renew_fence(self) -> dict[str, Any]:
        if self.fence_transitions is None:
            raise TransitionError("transition mutation attempted without a Lease fence")
        lease = self._lease()
        spec = lease.get("spec", {})
        if (
            spec.get("holderIdentity") != self.holder
            or int(spec.get("leaseTransitions", -1)) != self.fence_transitions
            or self._lease_expired(lease)
        ):
            raise TransitionError("transition Lease fence is stale")
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": lease["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/holderIdentity", "value": self.holder},
            {"op": "test", "path": "/spec/leaseTransitions", "value": self.fence_transitions},
            {
                "op": "add",
                "path": "/spec/renewTime",
                "value": dt.datetime.now(dt.UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            },
        ]
        result = self.guarded.run(
            "patch",
            "lease",
            LEASE_NAME,
            "--namespace",
            self.release_namespace,
            "--type=json",
            "--patch",
            canonical(patch),
            "-o",
            "json",
        )
        renewed = cast(dict[str, Any], json.loads(result.stdout))
        if (
            renewed.get("spec", {}).get("holderIdentity") != self.holder
            or int(renewed.get("spec", {}).get("leaseTransitions", -1)) != self.fence_transitions
        ):
            raise TransitionError("transition Lease renewal lost its fence")
        return renewed

    @property
    def fence_identity(self) -> str:
        if self.fence_transitions is None:
            raise TransitionError("transition Lease fence is unavailable")
        return f"{self.holder}:{self.fence_transitions}"

    def _release_lock(self) -> None:
        lease = self._renew_fence()
        if lease.get("spec", {}).get("holderIdentity") != self.holder:
            raise TransitionError("transition Lease ownership changed before release")
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": lease["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/holderIdentity", "value": self.holder},
            {
                "op": "test",
                "path": "/spec/leaseTransitions",
                "value": self.fence_transitions,
            },
            {"op": "add", "path": "/spec/holderIdentity", "value": ""},
        ]
        self.guarded.run(
            "patch",
            "lease",
            LEASE_NAME,
            "--namespace",
            self.release_namespace,
            "--type=json",
            "--patch",
            canonical(patch),
        )
        self.fence_transitions = None

    def run_fenced_helm(self, *arguments: str) -> CommandResult:
        """Run one mutating Helm action while renewing and enforcing the Lease."""
        self._renew_fence()
        process = subprocess.Popen(  # noqa: S603 - fixed Helm prefix and validated arguments
            [*self.helm.prefix, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=LOCK_RENEW_SECONDS)
                break
            except subprocess.TimeoutExpired:
                try:
                    self._renew_fence()
                except TransitionError:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
        outcome = CommandResult(process.returncode, stdout, stderr)
        if outcome.returncode != 0:
            detail = outcome.stderr.strip().splitlines()
            suffix = f": {detail[-1]}" if detail else ""
            raise TransitionError(f"fenced helm command failed{suffix}")
        self._renew_fence()
        return outcome

    def _chart_hash(self) -> str:
        digest = hashlib.sha256()
        files = sorted(path for path in self.chart.rglob("*") if path.is_file())
        if not files:
            raise TransitionError("chart contains no files")
        for path in files:
            digest.update(str(path.relative_to(self.chart)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _successful_history_entry(entry: dict[str, Any]) -> bool:
        description = str(entry.get("description", "")).lower()
        successful_description = (
            "complete" in description or re.fullmatch(r"rollback to [1-9][0-9]*", description) is not None
        )
        return (
            str(entry.get("status", "")).lower() in {"deployed", "superseded"}
            and successful_description
            and not any(word in description for word in ("fail", "pending"))
        )

    @staticmethod
    def _release_object_identity(policy: dict[str, Any]) -> dict[str, str]:
        metadata = policy.get("metadata", {})
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise TransitionError("Helm release policy lacks UID/resourceVersion identity")
        return {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "spec_sha256": sha256_json(policy.get("spec")),
        }

    def _release_storage_identity(self, revision: str, status: str) -> dict[str, str]:
        """Read only the exact Helm release Secret metadata, never its payload."""
        if not re.fullmatch(r"[1-9][0-9]*", revision):
            raise TransitionError("Helm storage revision is not a positive integer")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", self.release):
            raise TransitionError("Helm release name cannot identify exact storage")
        name = f"sh.helm.release.v1.{self.release}.v{revision}"
        jsonpath = (
            r'{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.metadata.resourceVersion}'
            r'{"\t"}{.metadata.labels.owner}{"\t"}{.metadata.labels.name}'
            r'{"\t"}{.metadata.labels.version}{"\t"}{.metadata.labels.status}'
        )
        result = self.bootstrap_kubectl.run(
            "get",
            "secret",
            name,
            "--namespace",
            self.release_namespace,
            "-o",
            f"jsonpath={jsonpath}",
        )
        fields = result.stdout.split("\t")
        if len(fields) != 7:
            raise TransitionError("Helm release storage metadata is incomplete")
        storage_name, uid, resource_version, owner, release_name, stored_revision, stored_status = fields
        if (
            storage_name != name
            or not uid
            or not resource_version
            or owner != "helm"
            or release_name != self.release
            or stored_revision != revision
            or stored_status.lower() != status.lower()
        ):
            raise TransitionError("Helm release storage identity does not match list/history")
        return {
            "name": storage_name,
            "uid": uid,
            "resource_version": resource_version,
            "revision": stored_revision,
            "status": stored_status.lower(),
        }

    def _release_identity(
        self,
        proxy: dict[str, Any],
        controller: dict[str, Any],
        *,
        require_deployed: bool = True,
    ) -> dict[str, Any]:
        listing = self.helm.run(
            "list",
            "--namespace",
            self.release_namespace,
            "--all",
            "--filter",
            f"^{re.escape(self.release)}$",
            "-o",
            "json",
        )
        exact = [item for item in json.loads(listing.stdout) if item.get("name") == self.release]
        if not exact:
            return {
                "name": self.release,
                "namespace": self.release_namespace,
                "revision": "absent",
                "status": "absent",
                "deployed_manifest_sha256": None,
                "deployed_values_sha256": None,
                "request_debug_enabled": None,
                "identity_uid": None,
                "storage": None,
                "objects": {},
            }
        if len(exact) != 1:
            raise TransitionError("expected at most one existing exact Helm release")
        revision = str(exact[0].get("revision", ""))
        status = str(exact[0].get("status", "")).lower()
        if not re.fullmatch(r"[1-9][0-9]*", revision):
            raise TransitionError("Helm release revision is not a positive integer")
        if require_deployed and status != "deployed":
            raise TransitionError("existing Helm release is not successfully deployed")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        current = [row for row in history if str(row.get("revision")) == revision]
        if len(current) != 1 or str(current[0].get("status", "")).lower() != status:
            raise TransitionError("Helm list/history identity is inconsistent")
        if require_deployed and not self._successful_history_entry(current[0]):
            raise TransitionError("Helm release is not a successful stable deployment")
        manifest = self.helm.run(
            "get", "manifest", self.release, "--namespace", self.release_namespace, "--revision", revision
        ).stdout
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        storage = self._release_storage_identity(revision, status)
        proxy_policy = self.get_policy(proxy["normal_name"], proxy["namespace"])
        controller_policy = self.get_policy(controller["normal_name"], controller["namespace"])
        objects = {
            "public-envoy": self._release_object_identity(proxy_policy),
            "envoy-controller": self._release_object_identity(controller_policy),
        }
        return {
            "name": self.release,
            "namespace": self.release_namespace,
            "revision": revision,
            "status": status,
            "description": current[0].get("description", ""),
            "chart": current[0].get("chart", ""),
            "app_version": current[0].get("app_version", ""),
            "deployed_manifest_sha256": sha256_json(self._yaml_documents(manifest)),
            "deployed_values_sha256": sha256_json(values),
            "request_debug_enabled": values.get("config", {}).get("requestDebugEnabled"),
            "identity_uid": storage["uid"],
            "storage": storage,
            "objects": objects,
        }

    def _yaml_documents(self, rendered: str) -> list[dict[str, Any]]:
        result = Command(["yq", "eval-all", "-o=json", "[.]", "-"], name="yq").run(input_text=rendered)
        documents = json.loads(result.stdout)
        return [item for item in documents if isinstance(item, dict)]

    def live_topology(self) -> dict[str, Any]:
        result = self.guarded.run(
            "get", "configmap", TOPOLOGY_NAME, "--namespace", self.release_namespace, "-o", "json"
        )
        resource = json.loads(result.stdout)
        metadata = resource.get("metadata", {})
        labels = metadata.get("labels", {})
        if (
            labels.get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or not metadata.get("uid")
            or not metadata.get("resourceVersion")
        ):
            raise TransitionError("live boundary topology is not externally owned")
        try:
            contract = json.loads(resource.get("data", {})["topology.json"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TransitionError("live boundary topology contract is invalid") from error
        if contract.get("schema") != "fs2-serve.nebius.ai/network-policy-boundary-topology/v1":
            raise TransitionError("live boundary topology schema is unsupported")
        if contract.get("mode") != "public":
            raise TransitionError("protected live topology does not authorize a public boundary transition")
        if not isinstance(contract.get("security_owner_username"), str) or not re.fullmatch(
            r"[A-Za-z0-9:@._/-]{3,253}", contract["security_owner_username"]
        ):
            raise TransitionError("protected live topology has no valid external security owner")
        return {
            "contract": contract,
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "sha256": sha256_json(contract),
        }

    def render_candidate(self, *, release_identity: dict[str, Any] | None = None) -> Candidate:
        topology = self.live_topology()
        policy_command = [
            "template",
            self.release,
            str(self.chart),
            "--namespace",
            self.release_namespace,
            *self.helm_values,
        ]
        policy_render = self.helm.run(*policy_command, "--show-only", "templates/networkpolicy.yaml").stdout
        policies = [item for item in self._yaml_documents(policy_render) if item.get("kind") == "NetworkPolicy"]

        def exact(component: str) -> dict[str, Any]:
            matching = [
                item
                for item in policies
                if item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component") == component
            ]
            if len(matching) != 1:
                raise TransitionError(f"expected one rendered {component} NetworkPolicy")
            policy = matching[0]
            namespace = policy.get("metadata", {}).get("namespace")
            name = policy.get("metadata", {}).get("name")
            spec = policy.get("spec")
            if not isinstance(spec, dict) or not isinstance(spec.get("podSelector"), dict):
                raise TransitionError(f"rendered {component} NetworkPolicy is incomplete")
            if not isinstance(namespace, str) or not namespace or not isinstance(name, str) or not name:
                raise TransitionError(f"rendered {component} NetworkPolicy identity is incomplete")
            return {"namespace": namespace, "normal_name": name, "spec": spec}

        proxy = exact("public-edge")
        controller = exact("envoy-controller-xds")
        proxy["guard_name"] = f"{proxy['normal_name']}-transition-guard"
        controller["guard_name"] = f"{controller['normal_name']}-transition-guard"
        deny_name = proxy["normal_name"].removesuffix("-public-envoy") + "-envoy-default-deny"
        expected = topology["contract"]
        if (
            proxy["namespace"] != expected.get("gateway_namespace")
            or controller["namespace"] != expected.get("controller_namespace")
            or proxy["normal_name"] != expected.get("policy_names", {}).get("proxy_normal")
            or proxy["guard_name"] != expected.get("policy_names", {}).get("proxy_guard")
            or controller["normal_name"] != expected.get("policy_names", {}).get("controller_normal")
            or controller["guard_name"] != expected.get("policy_names", {}).get("controller_guard")
            or deny_name != expected.get("policy_names", {}).get("default_deny")
        ):
            raise TransitionError("caller render does not match the protected live boundary topology")
        release = release_identity or self._release_identity(proxy, controller)
        complete_command = list(policy_command)
        if release["status"] != "absent":
            complete_command.append("--is-upgrade")
        complete_render = self.helm.run(*complete_command).stdout
        chart_hash = self._chart_hash()
        values_hash = sha256_json(self.value_sources)
        complete_render_hash = sha256_json(self._yaml_documents(complete_render))
        policy_hash = sha256_json(self._yaml_documents(policy_render))
        material = {
            "schema": SCHEMA,
            "chart_sha256": chart_hash,
            "values_sha256": values_hash,
            "complete_render_sha256": complete_render_hash,
            "network_policy_render_sha256": policy_hash,
            "release": release,
            "topology": topology,
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=chart_hash,
            values_sha256=values_hash,
            complete_render_sha256=complete_render_hash,
            network_policy_render_sha256=policy_hash,
            release=release,
            topology=topology,
            proxy=proxy,
            controller=controller,
            deny_name=deny_name,
        )

    def get_policy(self, name: str, namespace: str) -> dict[str, Any]:
        result = self.guarded.run("get", "networkpolicy", name, "--namespace", namespace, "-o", "json")
        return cast(dict[str, Any], json.loads(result.stdout))

    def get_optional_policy(self, name: str, namespace: str) -> dict[str, Any] | None:
        result = self.guarded.run(
            "get",
            "networkpolicy",
            name,
            "--namespace",
            namespace,
            "--ignore-not-found",
            "-o",
            "json",
            check=False,
        )
        if result.returncode != 0:
            raise TransitionError("default-deny lookup failed without a verified NotFound")
        if not result.stdout.strip():
            return None
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def _verify_boundary_identity(policy: dict[str, Any], *, role: str) -> None:
        metadata = policy.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get(BOUNDARY_LABEL) != BOUNDARY_VALUE or labels.get(ROLE_LABEL) != role:
            raise TransitionError(f"{role} boundary is not externally owned")
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise TransitionError(f"{role} boundary lacks Kubernetes identity")

    def patch_guard(self, role: str, policy: dict[str, Any], candidate: Candidate) -> dict[str, Any]:
        current = self.get_policy(policy["guard_name"], policy["namespace"])
        self._verify_boundary_identity(current, role=role)
        annotations = {
            CANDIDATE_ANNOTATION: candidate.candidate_sha256,
            NORMAL_ANNOTATION: policy["normal_name"],
            FENCE_ANNOTATION: self.fence_identity,
        }
        if role == "public-envoy":
            annotations[DENY_ANNOTATION] = candidate.deny_name
        patch = canonical(
            {
                "metadata": {
                    "resourceVersion": current["metadata"]["resourceVersion"],
                    "annotations": annotations,
                },
                "spec": policy["spec"],
            }
        )
        common = (
            "patch",
            "networkpolicy",
            policy["guard_name"],
            "--namespace",
            policy["namespace"],
            "--type=merge",
            "--patch",
            patch,
        )
        self.guarded.run(*common, "--dry-run=server", "-o", "json")
        self._renew_fence()
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        updated = cast(dict[str, Any], json.loads(result.stdout))
        self.verify_guard(role, policy, candidate, updated)
        return updated

    def verify_guard(
        self,
        role: str,
        policy: dict[str, Any],
        candidate: Candidate,
        live: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = live or self.get_policy(policy["guard_name"], policy["namespace"])
        self._verify_boundary_identity(current, role=role)
        if current.get("spec") != policy["spec"]:
            raise TransitionError(f"{role} boundary spec does not match the candidate")
        annotations = current.get("metadata", {}).get("annotations", {})
        if annotations.get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError(f"{role} boundary is bound to a stale candidate")
        self.verify_ready_pods(policy["namespace"], policy["spec"], role=role)
        return current

    def verify_ready_pods(self, namespace: str, spec: dict[str, Any], *, role: str) -> int:
        selector = spec.get("podSelector", {})
        labels = selector.get("matchLabels")
        if not isinstance(labels, dict) or not labels or selector.get("matchExpressions"):
            raise TransitionError(f"{role} boundary requires an exact matchLabels selector")
        ready = [
            pod
            for pod in self.list_pods_bounded(namespace, labels)
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if not ready:
            raise TransitionError(f"{role} boundary selects zero Ready Pods")
        return len(ready)

    def list_pods_bounded(self, namespace: str, labels: dict[str, str]) -> list[dict[str, Any]]:
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        encoded_namespace = urllib.parse.quote(namespace, safe="")
        encoded_selector = urllib.parse.quote(selector, safe="")
        continuation = ""
        items: list[dict[str, Any]] = []
        for _ in range(MAX_POD_PAGES):
            query = f"limit={POD_PAGE_SIZE}&labelSelector={encoded_selector}"
            if continuation:
                query += f"&continue={urllib.parse.quote(continuation, safe='')}"
            result = self.guarded.run("get", "--raw", f"/api/v1/namespaces/{encoded_namespace}/pods?{query}")
            page = json.loads(result.stdout)
            page_items = page.get("items", [])
            if not isinstance(page_items, list) or len(page_items) > POD_PAGE_SIZE:
                raise TransitionError("Pod list page exceeded its enforced bound")
            items.extend(item for item in page_items if isinstance(item, dict))
            continuation = str(page.get("metadata", {}).get("continue", ""))
            if not continuation:
                return items
        raise TransitionError("Pod selector exceeded bounded pagination")

    def verify_normal(self, role: str, policy: dict[str, Any]) -> None:
        normal = self.get_policy(policy["normal_name"], policy["namespace"])
        if normal.get("spec") != policy["spec"]:
            raise TransitionError(f"{role} Helm policy does not match the staged boundary")
        self.verify_ready_pods(policy["namespace"], normal["spec"], role=role)

    def _deny_patch(
        self,
        candidate: Candidate,
        spec: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        patch = canonical(
            {
                "metadata": {
                    "resourceVersion": current["metadata"]["resourceVersion"],
                    "annotations": {
                        CANDIDATE_ANNOTATION: candidate.candidate_sha256,
                        FENCE_ANNOTATION: self.fence_identity,
                    },
                },
                "spec": spec,
            }
        )
        common = (
            "patch",
            "networkpolicy",
            candidate.deny_name,
            "--namespace",
            candidate.proxy["namespace"],
            "--type=merge",
            "--patch",
            patch,
        )
        self.guarded.run(*common, "--dry-run=server", "-o", "json")
        self._renew_fence()
        result = self.guarded.run(
            *common,
            "-o",
            "json",
        )
        return cast(dict[str, Any], json.loads(result.stdout))

    def activate_deny(self, candidate: Candidate) -> None:
        current = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        self._verify_boundary_identity(current, role="default-deny")
        expected = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
        if self._deny_patch(candidate, expected, current).get("spec") != expected:
            raise TransitionError("default-deny did not become active")

    def verify_deny_active(self, candidate: Candidate) -> None:
        current = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        expected = {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}
        if current.get("spec") != expected:
            raise TransitionError("default-deny is not active")
        if current.get("metadata", {}).get("annotations", {}).get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError("default-deny is bound to a stale candidate")

    def relax_deny(self, candidate: Candidate) -> str:
        current = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
        if current is None:
            return "verified-not-found"
        self._verify_boundary_identity(current, role="default-deny")
        expected = {
            "podSelector": {"matchLabels": RELAXED_SELECTOR},
            "policyTypes": ["Ingress"],
            "ingress": [],
        }
        if self._deny_patch(candidate, expected, current).get("spec") != expected:
            raise TransitionError("default-deny was not relaxed")
        if self.list_pods_bounded(candidate.proxy["namespace"], RELAXED_SELECTOR):
            raise TransitionError("relaxed default-deny still selects Pods")
        proof = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        if proof.get("spec") != expected:
            raise TransitionError("default-deny relaxation proof changed")
        if proof.get("metadata", {}).get("annotations", {}).get(CANDIDATE_ANNOTATION) != candidate.candidate_sha256:
            raise TransitionError("relaxed default-deny is bound to a stale candidate")
        return "relaxed-zero-selected-pods"

    def receipt(self) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self.guarded.run("get", "configmap", RECEIPT_NAME, "--namespace", self.release_namespace, "-o", "json")
        resource = json.loads(result.stdout)
        encoded = resource.get("data", {}).get("receipt.json", "")
        try:
            receipt = json.loads(encoded) if encoded else {}
        except json.JSONDecodeError as error:
            raise TransitionError("transition receipt is not valid JSON") from error
        return resource, receipt

    @staticmethod
    def _guard_receipt(policy: dict[str, Any]) -> dict[str, Any]:
        metadata = policy["metadata"]
        spec = policy["spec"]
        return {
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "spec_sha256": sha256_json(spec),
            "spec": spec,
        }

    def write_receipt(
        self,
        phase: str,
        candidate: Candidate,
        guards: dict[str, dict[str, Any]],
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resource, previous = self.receipt()
        metadata = resource["metadata"]
        receipt = {
            "schema": SCHEMA,
            "phase": phase,
            "candidate": candidate.as_dict(),
            "boundary_objects": {role: self._guard_receipt(policy) for role, policy in sorted(guards.items())},
            "receipt_object": {
                "namespace": metadata["namespace"],
                "name": metadata["name"],
                "uid": metadata["uid"],
                "prior_resource_version": metadata["resourceVersion"],
            },
            "previous_phase": previous.get("phase", "uninitialized"),
            "fence": {
                "holder_identity": self.holder,
                "lease_transitions": self.fence_transitions,
            },
        }
        deny = self.get_optional_policy(candidate.deny_name, candidate.proxy["namespace"])
        if deny is None:
            receipt["boundary_objects"]["default-deny"] = {
                "namespace": candidate.proxy["namespace"],
                "name": candidate.deny_name,
                "status": "verified-not-found",
            }
        else:
            self._verify_boundary_identity(deny, role="default-deny")
            receipt["boundary_objects"]["default-deny"] = self._guard_receipt(deny)
        if extra:
            receipt.update(extra)
        patch = {
            "metadata": {"resourceVersion": metadata["resourceVersion"]},
            "data": {"receipt.json": canonical(receipt)},
        }
        self._renew_fence()
        result = self.guarded.run(
            "patch",
            "configmap",
            RECEIPT_NAME,
            "--namespace",
            self.release_namespace,
            "--type=merge",
            "--patch",
            canonical(patch),
            "-o",
            "json",
        )
        return cast(dict[str, Any], json.loads(result.stdout))

    @staticmethod
    def verify_receipt_boundaries(receipt: dict[str, Any], boundaries: dict[str, dict[str, Any]]) -> None:
        recorded = receipt.get("boundary_objects", {})
        for role, boundary in boundaries.items():
            expected = recorded.get(role, {})
            metadata = boundary.get("metadata", {})
            if (
                expected.get("uid") != metadata.get("uid")
                or expected.get("resource_version") != metadata.get("resourceVersion")
                or expected.get("spec_sha256") != sha256_json(boundary.get("spec"))
                or expected.get("spec") != boundary.get("spec")
            ):
                raise TransitionError(f"{role} boundary no longer matches the durable receipt")

    @staticmethod
    def verify_receipt_boundary_uids(receipt: dict[str, Any], boundaries: dict[str, dict[str, Any]]) -> None:
        """Permit crash-resume spec/RV drift only for the exact durable objects."""
        recorded = receipt.get("boundary_objects", {})
        for role, boundary in boundaries.items():
            expected = recorded.get(role, {})
            metadata = boundary.get("metadata", {})
            if (
                expected.get("namespace") != metadata.get("namespace")
                or expected.get("name") != metadata.get("name")
                or expected.get("uid") != metadata.get("uid")
            ):
                raise TransitionError(f"{role} boundary identity changed during durable intent recovery")

    def boundary_objects(self, candidate: Candidate) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        guards = {
            "public-envoy": self.get_policy(candidate.proxy["guard_name"], candidate.proxy["namespace"]),
            "envoy-controller": self.get_policy(candidate.controller["guard_name"], candidate.controller["namespace"]),
        }
        deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
        for role, boundary in guards.items():
            self._verify_boundary_identity(boundary, role=role)
        self._verify_boundary_identity(deny, role="default-deny")
        return guards, deny

    @staticmethod
    def intent(operation: str, target: Any) -> dict[str, Any]:
        return {
            "operation": operation,
            "target_sha256": sha256_json(target),
            "target": target,
        }

    def current_guards(self, candidate: Candidate) -> dict[str, dict[str, Any]]:
        return {
            "envoy-controller": self.verify_guard("envoy-controller", candidate.controller, candidate),
            "public-envoy": self.verify_guard("public-envoy", candidate.proxy, candidate),
        }

    @staticmethod
    def candidate_from_receipt(receipt: dict[str, Any]) -> Candidate:
        encoded = receipt.get("candidate")
        if not isinstance(encoded, dict) or encoded.get("candidate_sha256") is None:
            raise TransitionError("transition receipt has no candidate identity")
        policies = encoded.get("policies", {})
        return Candidate(
            candidate_sha256=encoded["candidate_sha256"],
            chart_sha256=encoded["chart_sha256"],
            values_sha256=encoded["values_sha256"],
            complete_render_sha256=encoded["complete_render_sha256"],
            network_policy_render_sha256=encoded["network_policy_render_sha256"],
            release=encoded["release"],
            topology=encoded["topology"],
            proxy=policies["public-envoy"],
            controller=policies["envoy-controller"],
            deny_name=encoded["deny_name"],
        )

    def verify_local_candidate(self, receipt: dict[str, Any]) -> Candidate:
        recorded = self.candidate_from_receipt(receipt)
        rendered = self.render_candidate(release_identity=recorded.release)
        if rendered.as_dict() != recorded.as_dict():
            raise TransitionError("local chart/values/render no longer match the durable candidate")
        return recorded

    def verify_live_topology(self, candidate: Candidate) -> None:
        if self.live_topology() != candidate.topology:
            raise TransitionError("protected live boundary topology changed from the durable receipt")

    def verify_deployed_target(self, candidate: Candidate) -> dict[str, Any]:
        deployed = self._release_identity(candidate.proxy, candidate.controller)
        source = candidate.release
        expected_revision = 1 if source["status"] == "absent" else int(source["revision"]) + 1
        if int(deployed["revision"]) != expected_revision:
            raise TransitionError("deployed Helm revision is not the candidate's exact successor")
        if deployed["deployed_manifest_sha256"] != candidate.complete_render_sha256:
            raise TransitionError("deployed Helm manifest does not match the candidate render")
        if source["status"] != "absent":
            for role, identity in source.get("objects", {}).items():
                if deployed.get("objects", {}).get(role, {}).get("uid") != identity.get("uid"):
                    raise TransitionError(f"Helm release {role} UID changed during upgrade")
        return deployed

    def capture_rollback_source(self, candidate: Candidate) -> dict[str, Any] | None:
        """Bind the pre-upgrade release both before and after Helm supersedes it."""
        source = candidate.release
        if source.get("status") == "absent":
            return None
        if source.get("status") != "deployed":
            raise TransitionError("staged rollback source was not a successful deployed release")
        revision = str(source.get("revision"))
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("staged source is no longer a successful stable Helm revision")
        current_storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        staged_storage = source.get("storage", {})
        for field in ("name", "uid", "revision"):
            if current_storage.get(field) != staged_storage.get(field):
                raise TransitionError("staged source Helm storage identity changed across upgrade")
        manifest_sha256 = sha256_json(self._yaml_documents(self._target_manifest(revision)))
        if manifest_sha256 != source.get("deployed_manifest_sha256"):
            raise TransitionError("staged source Helm manifest changed across upgrade")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        values_sha256 = sha256_json(values)
        if values_sha256 != source.get("deployed_values_sha256"):
            raise TransitionError("staged source Helm values changed across upgrade")
        return {
            "target_revision": revision,
            "target_history": matches[0],
            "target_manifest_sha256": manifest_sha256,
            "target_values_sha256": values_sha256,
            "staged_storage": staged_storage,
            "current_storage": current_storage,
            "request_debug_enabled": values.get("config", {}).get("requestDebugEnabled"),
        }

    @staticmethod
    def staged_rollback_source(candidate: Candidate) -> dict[str, Any]:
        source = candidate.release
        return {
            "target_revision": str(source.get("revision")),
            "target_manifest_sha256": source.get("deployed_manifest_sha256"),
            "target_values_sha256": source.get("deployed_values_sha256"),
            "staged_storage": source.get("storage"),
            "current_storage": source.get("storage"),
            "request_debug_enabled": source.get("request_debug_enabled"),
        }

    def stage(self) -> None:
        with self.lock():
            candidate = self.render_candidate()
            if candidate.release["status"] != "deployed":
                raise TransitionError("stage requires a successful deployed release with Ready boundary Pods")
            self._stage_locked(candidate)
        print(f"network-policy-transition=staged candidate={candidate.candidate_sha256}")

    def _stage_locked(self, candidate: Candidate) -> None:
        _, prior = self.receipt()
        phase = prior.get("phase")
        prior_candidate = prior.get("candidate", {}).get("candidate_sha256")
        if phase in IN_FLIGHT_PHASES and prior_candidate != candidate.candidate_sha256:
            raise TransitionError("a different durable NetworkPolicy transition is still in flight")
        if phase in {"staged", "active"} and prior_candidate == candidate.candidate_sha256:
            guards = self.current_guards(candidate)
            self.verify_deny_active(candidate)
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            self.write_receipt(phase, candidate, guards)
            return
        guards, deny = self.boundary_objects(candidate)
        if phase == "guards-ready" and prior_candidate == candidate.candidate_sha256:
            self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            guards = self.current_guards(candidate)
            self.activate_deny(candidate)
            self.write_receipt("staged", candidate, guards)
            return
        if phase == "guards-staging" and prior_candidate == candidate.candidate_sha256:
            self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
        else:
            self.write_receipt(
                "guards-staging",
                candidate,
                guards,
                extra={
                    "intent": self.intent(
                        "stage-guards",
                        {
                            "public-envoy": candidate.proxy["spec"],
                            "envoy-controller": candidate.controller["spec"],
                        },
                    )
                },
            )
        proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
        controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
        guards = {"public-envoy": proxy, "envoy-controller": controller}
        self.write_receipt(
            "guards-ready",
            candidate,
            guards,
            extra={"intent": self.intent("activate-deny", {"podSelector": {}, "policyTypes": ["Ingress"]})},
        )
        self.activate_deny(candidate)
        self.write_receipt("staged", candidate, guards)

    def prepare(self) -> None:
        with self.lock():
            candidate = self.render_candidate()
            if candidate.release["status"] != "absent":
                if candidate.release["status"] != "deployed":
                    raise TransitionError("prepare requires an absent or successful deployed release")
                self._stage_locked(candidate)
                print(f"network-policy-transition=staged candidate={candidate.candidate_sha256}")
                return
            _, prior = self.receipt()
            phase = prior.get("phase")
            prior_candidate = prior.get("candidate", {}).get("candidate_sha256")
            if phase in {"bootstrap-relaxing", "bootstrap-ready"} and prior_candidate != candidate.candidate_sha256:
                raise TransitionError("bootstrap receipt is bound to a different candidate")
            guards, deny = self.boundary_objects(candidate)
            if phase == "bootstrap-ready":
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                print(f"network-policy-transition=bootstrap-ready candidate={candidate.candidate_sha256} deny=relaxed")
                return
            if phase == "bootstrap-relaxing":
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            else:
                self.write_receipt(
                    "bootstrap-relaxing",
                    candidate,
                    guards,
                    extra={"intent": self.intent("relax-deny", RELAXED_SELECTOR)},
                )
            deny_proof = self.relax_deny(candidate)
            self.write_receipt(
                "bootstrap-ready",
                candidate,
                guards,
                extra={"bootstrap": {"deny_proof": deny_proof}},
            )
        print(f"network-policy-transition=bootstrap-ready candidate={candidate.candidate_sha256} deny=relaxed")

    def complete(self) -> None:
        with self.lock():
            _, prior = self.receipt()
            if prior.get("phase") not in {
                "bootstrap-ready",
                "bootstrap-guards-staging",
                "guards-ready",
                "staged",
                "active",
            }:
                raise TransitionError("transition receipt is not in a resumable completion phase")
            candidate = self.verify_local_candidate(prior)
            deployed_release = self.verify_deployed_target(candidate)
            rollback_source = self.capture_rollback_source(candidate)
            completion = {"deployed_release": deployed_release, "rollback_source": rollback_source}
            phase = prior.get("phase")
            if phase == "bootstrap-ready":
                bootstrap_guards, bootstrap_deny = self.boundary_objects(candidate)
                self.verify_receipt_boundaries(prior, {**bootstrap_guards, "default-deny": bootstrap_deny})
                self.write_receipt(
                    "bootstrap-guards-staging",
                    candidate,
                    bootstrap_guards,
                    extra={
                        **completion,
                        "intent": self.intent(
                            "stage-bootstrap-guards",
                            {
                                "public-envoy": candidate.proxy["spec"],
                                "envoy-controller": candidate.controller["spec"],
                            },
                        ),
                    },
                )
                phase = "bootstrap-guards-staging"
                _, prior = self.receipt()
            if phase == "bootstrap-guards-staging":
                bootstrap_guards, bootstrap_deny = self.boundary_objects(candidate)
                self.verify_receipt_boundary_uids(prior, {**bootstrap_guards, "default-deny": bootstrap_deny})
                proxy = self.patch_guard("public-envoy", candidate.proxy, candidate)
                controller = self.patch_guard("envoy-controller", candidate.controller, candidate)
                guards = {"public-envoy": proxy, "envoy-controller": controller}
                self.write_receipt(
                    "guards-ready",
                    candidate,
                    guards,
                    extra={
                        **completion,
                        "intent": self.intent("activate-deny", {"podSelector": {}, "policyTypes": ["Ingress"]}),
                    },
                )
                phase = "guards-ready"
                _, prior = self.receipt()
            if phase == "guards-ready":
                guards = self.current_guards(candidate)
                deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
                self.activate_deny(candidate)
                self.write_receipt("staged", candidate, guards, extra=completion)
                phase = "staged"
                _, prior = self.receipt()
            else:
                guards = self.current_guards(candidate)
                if phase == "active" and (
                    prior.get("deployed_release") != deployed_release or prior.get("rollback_source") != rollback_source
                ):
                    raise TransitionError("receipt is not bound to the exact deployed release")
            self.verify_normal("public-envoy", candidate.proxy)
            self.verify_normal("envoy-controller", candidate.controller)
            self.verify_deny_active(candidate)
            deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
            self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
            self.write_receipt("active", candidate, guards, extra=completion)
        print(f"network-policy-transition=active candidate={candidate.candidate_sha256} boundaries=permanent")

    def _target_manifest(self, revision: str) -> str:
        return self.helm.run(
            "get",
            "manifest",
            self.release,
            "--namespace",
            self.release_namespace,
            "--revision",
            revision,
        ).stdout

    def _current_manifest(self) -> str:
        return self.helm.run("get", "manifest", self.release, "--namespace", self.release_namespace).stdout

    def _rollback_target(
        self,
        source: Candidate,
        revision: str,
        bound_source: dict[str, Any],
    ) -> dict[str, Any]:
        if source.release.get("status") != "deployed" or revision != str(source.release.get("revision")):
            raise TransitionError("rollback revision is not the receipt-bound stable source revision")
        if bound_source.get("target_revision") != revision:
            raise TransitionError("rollback source receipt names a different Helm revision")
        if bound_source.get("staged_storage") != source.release.get("storage"):
            raise TransitionError("rollback source receipt lost the exact staged storage resourceVersion")
        if bound_source.get("target_values_sha256") != source.release.get("deployed_values_sha256"):
            raise TransitionError("rollback source receipt lost the exact staged Helm values")
        if bound_source.get("target_manifest_sha256") != source.release.get("deployed_manifest_sha256"):
            raise TransitionError("rollback source receipt lost the exact staged Helm manifest")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("rollback target is not a successful stable Helm revision")
        target_storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        if target_storage != bound_source.get("current_storage"):
            raise TransitionError("rollback target storage UID/resourceVersion/status changed from its receipt")
        manifest_sha256 = sha256_json(self._yaml_documents(self._target_manifest(revision)))
        if manifest_sha256 != source.release.get("deployed_manifest_sha256"):
            raise TransitionError("rollback target manifest differs from the staged source release")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        values_sha256 = sha256_json(values)
        if values_sha256 != bound_source.get("target_values_sha256"):
            raise TransitionError("rollback target values differ from the staged source receipt")
        if (
            bound_source.get("request_debug_enabled") is not False
            or values.get("config", {}).get("requestDebugEnabled") is not False
        ):
            raise TransitionError("rollback target is ineligible while request debugging is enabled or unknown")
        return {
            "target_revision": revision,
            "target_history": matches[0],
            "target_manifest_sha256": manifest_sha256,
            "target_values_sha256": values_sha256,
            "target_storage": target_storage,
            "staged_storage": bound_source["staged_storage"],
            "request_debug_enabled": False,
            "source_release": source.release,
        }

    def _revalidate_rollback_target(self, rollback: dict[str, Any], revision: str) -> None:
        if rollback.get("target_revision") != revision or rollback.get("request_debug_enabled") is not False:
            raise TransitionError("durable rollback target does not match the request")
        source_release = rollback.get("source_release", {})
        if (
            rollback.get("staged_storage") != source_release.get("storage")
            or rollback.get("target_manifest_sha256") != source_release.get("deployed_manifest_sha256")
            or rollback.get("target_values_sha256") != source_release.get("deployed_values_sha256")
        ):
            raise TransitionError("durable rollback receipt lost its exact staged Helm source binding")
        history = json.loads(
            self.helm.run("history", self.release, "--namespace", self.release_namespace, "-o", "json").stdout
        )
        matches = [entry for entry in history if str(entry.get("revision")) == revision]
        if len(matches) != 1 or not self._successful_history_entry(matches[0]):
            raise TransitionError("durable rollback target is no longer a successful stable revision")
        storage = self._release_storage_identity(revision, str(matches[0].get("status", "")))
        recorded_storage = rollback.get("target_storage", {})
        if storage != recorded_storage:
            raise TransitionError("durable rollback target Helm storage UID/resourceVersion/status changed")
        if sha256_json(self._yaml_documents(self._target_manifest(revision))) != rollback.get("target_manifest_sha256"):
            raise TransitionError("durable rollback target manifest changed")
        values = json.loads(
            self.helm.run(
                "get",
                "values",
                self.release,
                "--namespace",
                self.release_namespace,
                "--revision",
                revision,
                "--all",
                "-o",
                "json",
            ).stdout
        )
        if values.get("config", {}).get("requestDebugEnabled") is not False:
            raise TransitionError("rollback target request-debug state is no longer disabled")
        if sha256_json(values) != rollback.get("target_values_sha256"):
            raise TransitionError("durable rollback target values changed")

    def _candidate_from_live(self, source: Candidate, deployed_release: dict[str, Any]) -> Candidate:
        proxy = dict(source.proxy)
        controller = dict(source.controller)
        proxy["spec"] = self.get_policy(proxy["normal_name"], proxy["namespace"])["spec"]
        controller["spec"] = self.get_policy(controller["normal_name"], controller["namespace"])["spec"]
        material = {
            "schema": SCHEMA,
            "source": "verified-live-rollback",
            "release": deployed_release,
            "topology": source.topology,
            "policies": {"public-envoy": proxy, "envoy-controller": controller},
            "deny_name": source.deny_name,
        }
        return Candidate(
            candidate_sha256=sha256_json(material),
            chart_sha256=source.chart_sha256,
            values_sha256=source.values_sha256,
            complete_render_sha256=deployed_release["deployed_manifest_sha256"],
            network_policy_render_sha256=sha256_json(
                {"public-envoy": proxy["spec"], "envoy-controller": controller["spec"]}
            ),
            release=deployed_release,
            topology=source.topology,
            proxy=proxy,
            controller=controller,
            deny_name=source.deny_name,
        )

    def rollback(self) -> None:
        revision = self.arguments.revision
        with self.lock():
            _, prior = self.receipt()
            candidate = self.candidate_from_receipt(prior)
            self.verify_live_topology(candidate)
            phase = prior.get("phase")
            rollback_state = prior.get("rollback", {})
            if phase == "rolled-back" and rollback_state.get("target_revision") == revision:
                self._revalidate_rollback_target(rollback_state, revision)
                deployed = self._release_identity(candidate.proxy, candidate.controller)
                if deployed != candidate.release:
                    raise TransitionError("rolled-back release identity no longer matches its receipt")
                guards = self.current_guards(candidate)
                self.verify_deny_active(candidate)
                deny = self.get_policy(candidate.deny_name, candidate.proxy["namespace"])
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(
                    "rolled-back",
                    candidate,
                    guards,
                    extra={"rollback": rollback_state},
                )
                print(f"network-policy-transition=rolled-back revision={revision} idempotent=true")
                return
            if phase not in {
                "staged",
                "active",
                "rollback-relaxing",
                "rollback-prepared",
                "rollback-guards-staging",
                "rollback-guards-ready",
            }:
                raise TransitionError("receipt phase does not authorize rollback")
            guards, deny = self.boundary_objects(candidate)
            if phase in {"staged", "active"}:
                bound_source = prior.get("rollback_source") or self.staged_rollback_source(candidate)
                rollback_state = self._rollback_target(candidate, revision, bound_source)
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                current = self._release_identity(candidate.proxy, candidate.controller, require_deployed=False)
                if current["status"] == "absent" or int(current["revision"]) <= int(revision):
                    raise TransitionError("current Helm release is not newer than the rollback target")
                rollback_state["current_release_before_rollback"] = current
                self.write_receipt(
                    "rollback-relaxing",
                    candidate,
                    guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent("relax-deny-for-rollback", RELAXED_SELECTOR),
                    },
                )
                phase = "rollback-relaxing"
                _, prior = self.receipt()
            if phase == "rollback-relaxing":
                self._revalidate_rollback_target(rollback_state, revision)
                guards, deny = self.boundary_objects(candidate)
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
                deny_proof = self.relax_deny(candidate)
                rollback_state["deny_proof"] = deny_proof
                self.write_receipt("rollback-prepared", candidate, guards, extra={"rollback": rollback_state})
                phase = "rollback-prepared"
                _, prior = self.receipt()
            if phase in {"rollback-prepared", "rollback-guards-staging", "rollback-guards-ready"}:
                self._revalidate_rollback_target(rollback_state, revision)
            current_hash = sha256_json(self._yaml_documents(self._current_manifest()))
            target_hash = rollback_state["target_manifest_sha256"]
            if current_hash != target_hash:
                self.run_fenced_helm(
                    "rollback",
                    self.release,
                    revision,
                    "--namespace",
                    self.release_namespace,
                    "--wait",
                    "--wait-for-jobs",
                    "--timeout",
                    self.arguments.timeout,
                )
            if sha256_json(self._yaml_documents(self._current_manifest())) != target_hash:
                raise TransitionError("Helm rollback did not reach the target manifest")
            deployed = self._release_identity(candidate.proxy, candidate.controller)
            if deployed["deployed_manifest_sha256"] != target_hash:
                raise TransitionError("rolled-back release identity has the wrong manifest")
            live_candidate = self._candidate_from_live(candidate, deployed)
            live_guards, live_deny = self.boundary_objects(live_candidate)
            if phase == "rollback-prepared":
                self.write_receipt(
                    "rollback-guards-staging",
                    live_candidate,
                    live_guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent(
                            "stage-rollback-guards",
                            {
                                "public-envoy": live_candidate.proxy["spec"],
                                "envoy-controller": live_candidate.controller["spec"],
                            },
                        ),
                    },
                )
                phase = "rollback-guards-staging"
                _, prior = self.receipt()
            if phase == "rollback-guards-staging":
                self.verify_receipt_boundary_uids(prior, {**live_guards, "default-deny": live_deny})
                proxy = self.patch_guard("public-envoy", live_candidate.proxy, live_candidate)
                controller = self.patch_guard("envoy-controller", live_candidate.controller, live_candidate)
                live_guards = {"public-envoy": proxy, "envoy-controller": controller}
                self.write_receipt(
                    "rollback-guards-ready",
                    live_candidate,
                    live_guards,
                    extra={
                        "rollback": rollback_state,
                        "intent": self.intent("activate-deny-after-rollback", {"podSelector": {}}),
                    },
                )
                phase = "rollback-guards-ready"
                _, prior = self.receipt()
            if phase == "rollback-guards-ready":
                self.verify_receipt_boundary_uids(prior, {**live_guards, "default-deny": live_deny})
            self.activate_deny(live_candidate)
            self.write_receipt("rolled-back", live_candidate, live_guards, extra={"rollback": rollback_state})
        print(f"network-policy-transition=rolled-back revision={revision} deny=active")

    def destroy(self) -> None:
        with self.lock():
            _, prior = self.receipt()
            candidate = self.candidate_from_receipt(prior)
            self.verify_live_topology(candidate)
            phase = prior.get("phase")
            guards, deny = self.boundary_objects(candidate)
            if phase == "destroy-prepared":
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                print("network-policy-transition=destroy-prepared deny=relaxed boundaries=retained")
                return
            if phase == "destroy-relaxing":
                self.verify_receipt_boundary_uids(prior, {**guards, "default-deny": deny})
            else:
                self.verify_receipt_boundaries(prior, {**guards, "default-deny": deny})
                self.write_receipt(
                    "destroy-relaxing",
                    candidate,
                    guards,
                    extra={
                        "destroy": {},
                        "intent": self.intent("relax-deny-for-destroy", RELAXED_SELECTOR),
                    },
                )
            deny_proof = self.relax_deny(candidate)
            self.write_receipt(
                "destroy-prepared",
                candidate,
                guards,
                extra={"destroy": {"deny_proof": deny_proof}},
            )
        print("network-policy-transition=destroy-prepared deny=relaxed boundaries=retained")

    def recover_admission(self, mode: str) -> None:
        if mode not in {"audit-warn", "deny"}:
            raise TransitionError("admission recovery mode is not reversible")
        with self.lock():
            topology = self.live_topology()
            receipt_resource, receipt = self.receipt()
            metadata = receipt_resource.get("metadata", {})
            lease = self._lease()
            lease_metadata = lease.get("metadata", {})
            approval = self._recovery_approval()
            result = self.handoff.request(
                "set-admission-recovery",
                {
                    "mode": mode,
                    "recovery_reference": self.arguments.recovery_reference,
                    "topology": {
                        "namespace": self.release_namespace,
                        "name": TOPOLOGY_NAME,
                        "uid": topology["uid"],
                        "resource_version": topology["resource_version"],
                        "sha256": topology["sha256"],
                    },
                    "receipt": {
                        "namespace": metadata.get("namespace"),
                        "name": metadata.get("name"),
                        "uid": metadata.get("uid"),
                        "resource_version": metadata.get("resourceVersion"),
                        "api_version": receipt_resource.get("apiVersion"),
                        "kind": receipt_resource.get("kind"),
                        "state_sha256": sha256_json(
                            {
                                "labels": metadata.get("labels", {}),
                                "annotations": metadata.get("annotations", {}),
                                "spec": receipt_resource.get("spec"),
                                "data": receipt_resource.get("data"),
                            }
                        ),
                        "receipt_sha256": sha256_json(receipt),
                        "phase": receipt.get("phase"),
                        "intent": receipt.get("intent"),
                    },
                    "lease": {
                        "namespace": lease_metadata.get("namespace"),
                        "name": lease_metadata.get("name"),
                        "uid": lease_metadata.get("uid"),
                        "resource_version": lease_metadata.get("resourceVersion"),
                        "holder_identity": lease.get("spec", {}).get("holderIdentity"),
                        "lease_transitions": lease.get("spec", {}).get("leaseTransitions"),
                        "api_version": lease.get("apiVersion"),
                        "kind": lease.get("kind"),
                        "state_sha256": sha256_json(
                            {
                                "labels": lease_metadata.get("labels", {}),
                                "annotations": lease_metadata.get("annotations", {}),
                                "spec": lease.get("spec"),
                                "data": lease.get("data"),
                            }
                        ),
                    },
                    "binding": {"name": "fs2-network-policy-boundary"},
                    "parameter": {
                        "namespace": self.release_namespace,
                        "name": "fs2-network-policy-boundary-parameters",
                    },
                    "approval": approval,
                    "delete_allowed": False,
                },
            )
            self._verify_recovery_result(
                mode,
                result,
                receipt_uid=metadata.get("uid"),
            )
        print(f"network-policy-admission-recovery={mode} reference={self.arguments.recovery_reference}")

    def _verify_recovery_result(
        self,
        mode: str,
        result: dict[str, Any],
        *,
        receipt_uid: Any,
    ) -> None:
        binding = result.get("binding")
        parameter = result.get("parameter")
        recovery_receipt = result.get("receipt")
        expected_actions = ["Audit", "Warn"] if mode == "audit-warn" else ["Deny"]
        if not isinstance(binding, dict) or not isinstance(parameter, dict) or not isinstance(recovery_receipt, dict):
            raise TransitionError("signed recovery handoff returned incomplete objects")
        if (
            binding.get("metadata", {}).get("name") != "fs2-network-policy-boundary"
            or not binding.get("metadata", {}).get("uid")
            or binding.get("metadata", {}).get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or binding.get("spec", {}).get("policyName") != "fs2-network-policy-boundary"
            or binding.get("spec", {}).get("validationActions") != expected_actions
            or parameter.get("metadata", {}).get("namespace") != self.release_namespace
            or parameter.get("metadata", {}).get("name") != "fs2-network-policy-boundary-parameters"
            or not parameter.get("metadata", {}).get("uid")
            or parameter.get("metadata", {}).get("labels", {}).get(BOUNDARY_LABEL) != BOUNDARY_VALUE
            or parameter.get("data", {}).get("mode") != mode
            or parameter.get("data", {}).get("delete_allowed") != "false"
            or recovery_receipt.get("metadata", {}).get("uid") != receipt_uid
            or not recovery_receipt.get("metadata", {}).get("resourceVersion")
        ):
            raise TransitionError("signed recovery handoff did not reach the exact reversible mode")
        try:
            recovered = json.loads(recovery_receipt.get("data", {}).get("receipt.json", ""))
        except json.JSONDecodeError as error:
            raise TransitionError("signed recovery handoff returned an invalid durable receipt") from error
        recovery = recovered.get("security_recovery", {})

        def recovery_evidence(resource_object: dict[str, Any]) -> dict[str, Any]:
            metadata = resource_object.get("metadata", {})
            evidence = {
                "api_version": resource_object.get("apiVersion"),
                "kind": resource_object.get("kind"),
                "namespace": metadata.get("namespace", ""),
                "name": metadata.get("name"),
                "uid": metadata.get("uid"),
                "resource_version": metadata.get("resourceVersion"),
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
                "spec": resource_object.get("spec"),
                "data": resource_object.get("data"),
            }
            evidence["state_sha256"] = sha256_json(
                {
                    "labels": evidence["labels"],
                    "annotations": evidence["annotations"],
                    "spec": evidence["spec"],
                    "data": evidence["data"],
                }
            )
            return evidence

        if (
            recovery.get("state") != "complete"
            or recovery.get("mode") != mode
            or recovery.get("recovery_reference") != self.arguments.recovery_reference
            or recovery.get("after")
            != {"binding": recovery_evidence(binding), "parameter": recovery_evidence(parameter)}
        ):
            raise TransitionError("signed recovery handoff did not durably complete")

    def _recovery_approval(self) -> dict[str, Any]:
        path = Path(self.arguments.recovery_approval)
        try:
            approval_stat = path.lstat()
        except OSError as error:
            raise TransitionError("security recovery approval is unavailable") from error
        if (
            not stat.S_ISREG(approval_stat.st_mode)
            or stat.S_ISLNK(approval_stat.st_mode)
            or stat.S_IMODE(approval_stat.st_mode) not in {0o400, 0o600}
            or approval_stat.st_uid != os.geteuid()
        ):
            raise TransitionError("security recovery approval ownership or mode is unsafe")
        try:
            approval = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TransitionError("security recovery approval cannot be read safely") from error
        if not isinstance(approval, dict) or set(approval) != {"signed", "signature"}:
            raise TransitionError("security recovery approval envelope is not exact")
        return cast(dict[str, Any], approval)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("prepare", "stage", "complete", "rollback", "destroy", "recover-audit-warn", "recover-deny"),
    )
    parser.add_argument("--release", required=True)
    parser.add_argument("--release-namespace", required=True)
    parser.add_argument("--chart", required=True)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--security-handoff-socket", required=True)
    parser.add_argument("--security-handoff-server-public-key", required=True)
    parser.add_argument("--security-handoff-client-public-key", required=True)
    parser.add_argument("--security-handoff-client-private-key", required=True)
    parser.add_argument("--context", default="")
    parser.add_argument("--values", action="append", default=[])
    parser.add_argument("--values-env", action="append", default=[])
    parser.add_argument("--revision", default="")
    parser.add_argument("--recovery-reference", default="")
    parser.add_argument("--recovery-approval", default="")
    parser.add_argument("--timeout", default="10m")
    parser.add_argument("helm_value_args", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.action == "rollback" and not re.fullmatch(r"[1-9][0-9]*", arguments.revision):
        parser.error("rollback requires a positive --revision")
    if arguments.action.startswith("recover-") and not re.fullmatch(
        r"(?:SEC|INC|CHG)-[1-9][0-9]{2,15}", arguments.recovery_reference
    ):
        parser.error("admission recovery requires an exact SEC, INC, or CHG reference")
    if arguments.action.startswith("recover-") and not Path(arguments.recovery_approval).is_absolute():
        parser.error("admission recovery requires an absolute --recovery-approval path")
    for executable in ("helm", "kubectl", "yq"):
        if shutil.which(executable) is None:
            parser.error(f"required executable is unavailable: {executable}")
    if not Path(arguments.chart).is_dir() or not Path(arguments.kubeconfig).is_file():
        parser.error("chart and ordinary kubeconfig must exist")
    if not Path(arguments.security_handoff_socket).is_absolute():
        parser.error("security handoff socket path must be absolute")
    try:
        decode_base64url(arguments.security_handoff_server_public_key, expected_bytes=32)
        decode_base64url(arguments.security_handoff_client_public_key, expected_bytes=32)
    except TransitionError as error:
        parser.error(str(error))
    if not Path(arguments.security_handoff_client_private_key).is_absolute():
        parser.error("security handoff client signing key path must be absolute")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv or sys.argv[1:])
    transition = Transition(arguments)
    try:
        transition.configure_guarded_client()
        if arguments.action == "recover-audit-warn":
            transition.recover_admission("audit-warn")
        elif arguments.action == "recover-deny":
            transition.recover_admission("deny")
        else:
            getattr(transition, arguments.action)()
    except (TransitionError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"network policy transition failed closed: {error}", file=sys.stderr)
        return 1
    finally:
        transition.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

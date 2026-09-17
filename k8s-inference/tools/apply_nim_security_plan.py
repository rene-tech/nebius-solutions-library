#!/usr/bin/env python3
"""Apply one saved workload plan only behind a fresh SAI-25 activation fence.

This is the only supported activation entrypoint for a plan that can create a
NIM root or descendant. It verifies an independently signed, short-lived
authorization bound to the exact saved-plan bytes, acquires the fixed
security-owned Lease with Kubernetes resourceVersion CAS, repeats the signed
receipt's live object observations twice using current API-server time, and
holds the Lease until the exact plan process exits. The Lease is never deleted;
normal expiry makes an interrupted invocation recoverable.

The tool deliberately requests only PartialObjectMetadata for Secrets.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import ssl
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping

from fs2_serve_catalog.artifacts import canonical_bytes
from fs2_serve_catalog.attestations import verify_signed_attestation
from fs2_serve_catalog.nim_apply_fence import (
    NIM_ROOT_RESOURCES,
    canonical_nim_root_finalizers,
    canonical_nim_root_projection,
)


SCHEMA = "fs2-serve.nebius.ai/nim-admission-apply-fence/v1"
LEASE_NAME = "fs2-nim-admission-apply-fence"
NAMESPACE = "fs2-system"
FENCE_ANNOTATION = "fs2-serve.nebius.ai/apply-fence-nonce"
AUTHORIZATION_PREFIX = "fs2-nim-apply-authorization-"


class KubernetesRequestError(RuntimeError):
    def __init__(self, code: int) -> None:
        super().__init__(f"Kubernetes request failed: {code}")
        self.code = code


class SealedFile:
    """Stable-open a caller-owned, non-symlink regular file for one invocation."""

    def __init__(self, path: Path, *, private: bool, expected_uid: int | None = None) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        self.fd = os.open(path, flags)
        self.initial = os.fstat(self.fd)
        if (
            not stat.S_ISREG(self.initial.st_mode)
            or self.initial.st_uid != (os.getuid() if expected_uid is None else expected_uid)
            or self.initial.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (private and self.initial.st_mode & (stat.S_IRGRP | stat.S_IROTH))
        ):
            raise ValueError(f"unsafe file custody: {path}")

    def bytes(self, maximum: int) -> bytes:
        os.lseek(self.fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(self.fd, min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise ValueError("sealed file is oversized")
        self.assert_unchanged()
        return b"".join(chunks)

    def assert_unchanged(self) -> None:
        current = os.fstat(self.fd)
        if (
            current.st_dev,
            current.st_ino,
            current.st_uid,
            stat.S_IMODE(current.st_mode),
            current.st_size,
            current.st_mtime_ns,
        ) != (
            self.initial.st_dev,
            self.initial.st_ino,
            self.initial.st_uid,
            stat.S_IMODE(self.initial.st_mode),
            self.initial.st_size,
            self.initial.st_mtime_ns,
        ):
            raise RuntimeError("sealed file changed after authorization")

    @property
    def proc_path(self) -> str:
        return f"/proc/self/fd/{self.fd}"


def _loads_unique(raw: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("apply-fence timestamp must be UTC")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _projection(value: Mapping[str, Any], *, secret: bool = False) -> Mapping[str, Any]:
    projected = {
        key: item
        for key, item in value.items()
        if key not in {"metadata", "status"}
        and not (secret and key in {"data", "stringData"})
    }
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("live object metadata is absent")
    projected["metadata"] = {
        key: item
        for key, item in metadata.items()
        if key
        not in {
            "creationTimestamp", "deletionGracePeriodSeconds", "deletionTimestamp",
            "generation", "managedFields", "resourceVersion", "selfLink", "uid",
        }
    }
    return projected


class Kubernetes:
    def __init__(self, *, url: str, token: SealedFile, ca_file: SealedFile) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.ca_file = ca_file
        self.context = ssl.create_default_context(cafile=ca_file.proc_path)

    def request(
        self, method: str, path: str, *, body: Mapping[str, Any] | None = None,
        metadata_only: bool = False,
    ) -> tuple[Mapping[str, Any], datetime]:
        self.token.assert_unchanged()
        self.ca_file.assert_unchanged()
        token = self.token.bytes(64 * 1024).decode("utf-8").strip()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": (
                "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
                if metadata_only
                else "application/json"
            ),
        }
        data = None
        if body is not None:
            data = canonical_bytes(body)
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                server_date = response.headers.get("Date")
        except urllib.error.HTTPError as error:
            raise KubernetesRequestError(error.code) from error
        if len(raw) > 4 * 1024 * 1024 or server_date is None:
            raise RuntimeError("Kubernetes response is oversized or lacks server time")
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            raise RuntimeError("Kubernetes response is not an object")
        return value, parsedate_to_datetime(server_date).astimezone(timezone.utc)


def _verify_authorization(
    *, envelope: Mapping[str, Any], trust: Mapping[str, str], plan_sha256: str,
    server_now: datetime,
) -> Mapping[str, Any]:
    if set(envelope) != {
        "subject", "subject_sha256", "evidence_sha256", "attestation",
        "attestation_sha256",
    }:
        raise ValueError("apply-fence envelope fields differ")
    subject = envelope["subject"]
    if not isinstance(subject, Mapping) or set(subject) != {
        "schema", "cluster_uid", "security_session_id", "plan_sha256",
        "terraform_sha256", "capsule_manifest_sha256", "plan_actions",
        "plan_actions_sha256",
        "nonce", "issued_at", "valid_until",
        "security_handoff_sha256", "receipt_name", "receipt_subject_sha256",
        "provider_generation", "provider_subject_sha256", "principal", "lease_name",
        "lease_uid", "lease_duration_seconds", "objects", "allowed_root_actions",
        "authorization_record_name",
    }:
        raise ValueError("apply-fence subject fields differ")
    digest = hashlib.sha256(canonical_bytes(subject)).hexdigest()
    if (
        subject["schema"] != SCHEMA
        or subject["plan_sha256"] != plan_sha256
        or subject["lease_name"] != LEASE_NAME
        or subject["authorization_record_name"]
        != AUTHORIZATION_PREFIX + hashlib.sha256(str(subject["nonce"]).encode()).hexdigest()[:16]
        or re.fullmatch(
            r"fs2-platform-security-external-automation-[a-f0-9]{64}",
            str(subject["principal"]),
        )
        is None
        or not isinstance(subject["allowed_root_actions"], list)
        or subject["allowed_root_actions"]
        != sorted(subject["allowed_root_actions"], key=canonical_bytes)
        or len(
            {
                item.get("action_id")
                for item in subject["allowed_root_actions"]
                if isinstance(item, Mapping)
            }
        )
        != len(subject["allowed_root_actions"])
        or not 30 <= subject["lease_duration_seconds"] <= 300
        or digest != envelope["subject_sha256"]
        or hashlib.sha256(canonical_bytes(envelope["attestation"])).hexdigest()
        != envelope["attestation_sha256"]
    ):
        raise ValueError("apply-fence digest or plan binding differs")
    issued_at = _timestamp(subject["issued_at"])
    valid_until = _timestamp(subject["valid_until"])
    if issued_at > server_now or valid_until <= server_now or valid_until - issued_at > __import__("datetime").timedelta(minutes=5):
        raise ValueError("apply-fence authorization is stale")
    verified = verify_signed_attestation(
        envelope["attestation"],
        trusted_attestors=trust,
        expected_session_id=subject["security_session_id"],
        expected_kind="nim-admission-apply-fence",
        expected_schema=SCHEMA,
        expected_digest="sha256:" + digest,
        expected_model_id="platform",
    )
    if verified["claims"] != {
        "authorization_id": "nim-admission-apply-fence",
        "decision": "accepted",
        "reviewer_role": "external-platform-security",
        "evidence_sha256": envelope["evidence_sha256"],
    }:
        raise ValueError("apply-fence claims differ")
    return subject


def _object_path(item: Mapping[str, Any]) -> str:
    group, _, version = str(item["api_version"]).partition("/")
    if not version:
        version, group = group, ""
    resource = str(item["resource"])
    name = urllib.parse.quote(str(item["name"]), safe="")
    namespace = str(item["namespace"])
    prefix = f"/apis/{group}/{version}" if group else f"/api/{version}"
    if namespace:
        return f"{prefix}/namespaces/{urllib.parse.quote(namespace, safe='')}/{resource}/{name}"
    return f"{prefix}/{resource}/{name}"


def _observe(kubernetes: Kubernetes, objects: list[Mapping[str, Any]]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for item in objects:
        secret = item["resource"] == "secrets"
        value, _ = kubernetes.request(
            "GET", _object_path(item), metadata_only=secret
        )
        metadata = value.get("metadata")
        key = "|".join(
            str(item[field]) for field in ("api_version", "resource", "namespace", "name")
        )
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("uid") != item["uid"]
            or metadata.get("resourceVersion") != item["resource_version"]
        ):
            raise RuntimeError(f"apply-fence live identity differs: {key}")
        if not secret and hashlib.sha256(canonical_bytes(_projection(value))).hexdigest() != item["projection_sha256"]:
            raise RuntimeError(f"apply-fence live projection differs: {key}")
        snapshot[key] = str(metadata["resourceVersion"])
    return snapshot


def _after_fence_nonce(resource_type: str, after: object) -> str:
    if not isinstance(after, Mapping):
        raise ValueError("mutable resource has no after value")
    if resource_type == "kubernetes_manifest":
        manifest = after.get("manifest")
        annotations = (
            manifest.get("metadata", {}).get("annotations", {})
            if isinstance(manifest, Mapping)
            else {}
        )
    elif resource_type.startswith("kubernetes_") and resource_type.endswith("_v1"):
        metadata = after.get("metadata")
        annotations = (
            metadata[0].get("annotations", {})
            if isinstance(metadata, list)
            and len(metadata) == 1
            and isinstance(metadata[0], Mapping)
            else {}
        )
    else:
        raise ValueError(f"mutable provider resource type is not allowlisted: {resource_type}")
    nonce = annotations.get(FENCE_ANNOTATION) if isinstance(annotations, Mapping) else None
    if not isinstance(nonce, str) or not nonce:
        raise ValueError("Kubernetes mutation lacks its apply-fence nonce")
    return nonce


def _root_action(resource_type: str, actions: list[str], after: object) -> Mapping[str, Any]:
    """Return the exact admission projection for one independently planned NIM root."""

    if resource_type != "kubernetes_manifest" or not isinstance(after, Mapping):
        raise ValueError("the fenced plan may mutate only kubernetes_manifest NIM roots")
    manifest = after.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("the fenced NIM root manifest is absent")
    api_version = manifest.get("apiVersion")
    kind = manifest.get("kind")
    metadata = manifest.get("metadata")
    resource = NIM_ROOT_RESOURCES.get((str(api_version), str(kind)))
    if (
        resource is None
        or not isinstance(metadata, Mapping)
        or not isinstance(metadata.get("namespace"), str)
        or not isinstance(metadata.get("name"), str)
        or actions not in (["create"], ["update"])
    ):
        raise ValueError("the fenced plan contains a non-NIM or unbound root mutation")
    annotations = metadata.get("annotations")
    expected_uid = (
        annotations.get("fs2-serve.nebius.ai/apply-fence-precondition-uid")
        if isinstance(annotations, Mapping)
        else None
    )
    expected_resource_version = (
        annotations.get("fs2-serve.nebius.ai/apply-fence-precondition-resource-version")
        if isinstance(annotations, Mapping)
        else None
    )
    before_projection_sha256 = (
        annotations.get("fs2-serve.nebius.ai/apply-fence-before-projection-sha256")
        if isinstance(annotations, Mapping)
        else None
    )
    before_finalizers_sha256 = (
        annotations.get("fs2-serve.nebius.ai/apply-fence-before-finalizers-sha256")
        if isinstance(annotations, Mapping)
        else None
    )
    if (
        actions == ["create"]
        and (
            expected_uid != "absent"
            or expected_resource_version != "absent"
            or before_projection_sha256 != "absent"
            or before_finalizers_sha256 != "absent"
            or canonical_nim_root_finalizers(manifest)
        )
    ) or (
        actions == ["update"]
        and (
            re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                str(expected_uid),
            )
            is None
            or re.fullmatch(r"[1-9][0-9]*", str(expected_resource_version)) is None
            or re.fullmatch(r"[a-f0-9]{64}", str(before_projection_sha256)) is None
            or re.fullmatch(r"[a-f0-9]{64}", str(before_finalizers_sha256)) is None
        )
    ):
        raise ValueError("the fenced NIM root lacks an exact persistence precondition")
    identity = {
        "api_group": "apps.nvidia.com",
        "api_version": "v1alpha1",
        "resource": resource,
        "kind": kind,
        "namespace": metadata["namespace"],
        "name": metadata["name"],
        "operation": "CREATE" if actions == ["create"] else "UPDATE",
        "expected_uid": expected_uid,
        "expected_resource_version": expected_resource_version,
        "before_projection_sha256": before_projection_sha256,
        "before_finalizers_sha256": before_finalizers_sha256,
        "after_projection_sha256": hashlib.sha256(
            canonical_bytes(canonical_nim_root_projection(manifest))
        ).hexdigest(),
    }
    return {
        **identity,
        "action_id": hashlib.sha256(canonical_bytes(identity)).hexdigest(),
        "replay_semantics": "idempotent-exact-persistence-precondition-and-projection",
    }


def _plan_actions(
    *, terraform: SealedFile, plan: SealedFile, environment: Mapping[str, str],
    working_directory_fd: int, data_directory_fd: int,
) -> list[Mapping[str, Any]]:
    completed = subprocess.run(
        [terraform.proc_path, "show", "-json", plan.proc_path],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
        pass_fds=(terraform.fd, plan.fd, working_directory_fd, data_directory_fd),
        cwd=f"/proc/self/fd/{working_directory_fd}",
    )
    if len(completed.stdout) > 64 * 1024 * 1024:
        raise ValueError("saved-plan JSON is oversized")
    value = json.loads(completed.stdout)
    changes = value.get("resource_changes") if isinstance(value, Mapping) else None
    if not isinstance(changes, list):
        raise ValueError("saved-plan action inventory is absent")
    inventory: list[Mapping[str, Any]] = []
    for change in changes:
        action = change.get("change") if isinstance(change, Mapping) else None
        actions = action.get("actions") if isinstance(action, Mapping) else None
        if (
            not isinstance(actions, list)
            or actions not in (["no-op"], ["create"], ["update"], ["read"])
            or "delete" in actions
        ):
            raise ValueError("saved plan contains a delete or replacement action")
        after = action.get("after")
        resource_type = str(change.get("type", ""))
        observed_nonce = ""
        root_action: Mapping[str, Any] | None = None
        if actions in (["create"], ["update"]):
            if resource_type == "helm_release":
                raise ValueError("Helm mutations are forbidden in the fenced workload plan")
            if resource_type == "terraform_data":
                raise ValueError("terraform_data mutation is forbidden in the fenced plan")
            observed_nonce = _after_fence_nonce(resource_type, after)
            root_action = _root_action(resource_type, actions, after)
        inventory.append(
            {
                "address": change.get("address"),
                "mode": change.get("mode"),
                "type": change.get("type"),
                "provider_name": change.get("provider_name"),
                "actions": actions,
                "observed_fence_nonce": observed_nonce,
                "nim_root_action": root_action,
                "before_sha256": hashlib.sha256(canonical_bytes(action.get("before"))).hexdigest(),
                "after_sha256": hashlib.sha256(canonical_bytes(after)).hexdigest(),
            }
        )
    return sorted(inventory, key=lambda item: str(item["address"]))


def _acquire_lease(kubernetes: Kubernetes, subject: Mapping[str, Any]) -> str:
    path = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE_NAME}"
    lease, server_now = kubernetes.request("GET", path)
    spec = lease.get("spec")
    metadata = lease.get("metadata")
    if not isinstance(spec, Mapping) or not isinstance(metadata, Mapping):
        raise RuntimeError("apply-fence Lease is absent")
    if (
        metadata.get("name") != LEASE_NAME
        or metadata.get("namespace") != NAMESPACE
        or metadata.get("uid") != subject["lease_uid"]
        or metadata.get("labels")
        != {
            "app.kubernetes.io/managed-by": "platform-security",
            "fs2-serve.nebius.ai/immutable-security-boundary": "true",
            "fs2-serve.nebius.ai/apply-fence": "true",
        }
    ):
        raise RuntimeError("apply-fence Lease custody differs")
    renew_time = _timestamp(spec.get("renewTime"))
    duration = int(spec.get("leaseDurationSeconds", 0))
    if spec.get("holderIdentity") and renew_time.timestamp() + duration > server_now.timestamp():
        raise RuntimeError("another release writer holds the apply fence")
    body = dict(lease)
    body["metadata"] = {
        **metadata,
        "annotations": {
            **(
                metadata.get("annotations", {})
                if isinstance(metadata.get("annotations"), Mapping)
                else {}
            ),
            "fs2-serve.nebius.ai/apply-fence-principal": subject["principal"],
            "fs2-serve.nebius.ai/apply-fence-valid-until": subject["valid_until"],
            "fs2-serve.nebius.ai/apply-fence-authorization": subject[
                "authorization_record_name"
            ],
            "fs2-serve.nebius.ai/apply-fence-subject-sha256": hashlib.sha256(
                canonical_bytes(subject)
            ).hexdigest(),
        },
    }
    body["spec"] = {
        **spec,
        "holderIdentity": subject["nonce"],
        "leaseDurationSeconds": subject["lease_duration_seconds"],
        "acquireTime": server_now.isoformat().replace("+00:00", "Z"),
        "renewTime": server_now.isoformat().replace("+00:00", "Z"),
    }
    committed, _ = kubernetes.request("PUT", path, body=body)
    committed_metadata = committed.get("metadata")
    if not isinstance(committed_metadata, Mapping):
        raise RuntimeError("apply-fence Lease commit is invalid")
    return str(committed_metadata["resourceVersion"])


def _publish_authorization_record(
    kubernetes: Kubernetes,
    *,
    envelope: Mapping[str, Any],
    subject: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Create one retained immutable authorization generation, or verify an exact retry."""

    name = str(subject["authorization_record_name"])
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{urllib.parse.quote(name, safe='')}"
    envelope_sha256 = hashlib.sha256(canonical_bytes(envelope)).hexdigest()
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "platform-security",
                "fs2-serve.nebius.ai/immutable-security-boundary": "true",
                "fs2-serve.nebius.ai/apply-fence-authorization": "true",
            },
            "annotations": {
                "helm.sh/resource-policy": "keep",
                "fs2-serve.nebius.ai/security-session-id": subject[
                    "security_session_id"
                ],
                "fs2-serve.nebius.ai/subject-sha256": envelope["subject_sha256"],
            },
        },
        "immutable": True,
        "data": {
            "envelope.json": canonical_bytes(envelope).decode("utf-8"),
            "envelope.sha256": envelope_sha256,
        },
    }
    try:
        committed, _ = kubernetes.request(
            "POST", f"/api/v1/namespaces/{NAMESPACE}/configmaps", body=body
        )
    except KubernetesRequestError as error:
        if error.code != 409:
            raise
        committed, _ = kubernetes.request("GET", path)
    metadata = committed.get("metadata") if isinstance(committed, Mapping) else None
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("name") != name
        or metadata.get("namespace") != NAMESPACE
        or committed.get("immutable") is not True
        or committed.get("data") != body["data"]
        or metadata.get("labels") != body["metadata"]["labels"]
        or metadata.get("annotations") != body["metadata"]["annotations"]
    ):
        raise RuntimeError("apply-fence authorization generation differs")
    return committed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--kubernetes-url", required=True)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--terraform", required=True, type=Path)
    parser.add_argument("--capsule-manifest", required=True, type=Path)
    args = parser.parse_args()
    plan = SealedFile(args.plan, private=False)
    authorization = SealedFile(args.authorization, private=True)
    trust_file = SealedFile(args.trust, private=False)
    token = SealedFile(args.token_file, private=True)
    ca_file = SealedFile(args.ca_file, private=False)
    if not args.terraform.is_absolute():
        raise ValueError("Terraform executable path must be absolute")
    terraform = SealedFile(args.terraform, private=False)
    capsule_file = SealedFile(args.capsule_manifest, private=False)
    if not terraform.initial.st_mode & stat.S_IXUSR:
        raise ValueError("pinned Terraform executable is not owner-executable")
    plan_sha256 = hashlib.sha256(plan.bytes(2 * 1024 * 1024 * 1024)).hexdigest()
    terraform_sha256 = hashlib.sha256(terraform.bytes(512 * 1024 * 1024)).hexdigest()
    capsule = _loads_unique(capsule_file.bytes(4 * 1024 * 1024))
    if not isinstance(capsule, Mapping) or set(capsule) != {
        "schema", "security_owner_uid", "working_directory",
        "terraform_data_directory", "files"
    } or capsule.get("schema") != "fs2-serve.nebius.ai/terraform-sealed-capsule/v1":
        raise ValueError("Terraform capsule manifest differs")
    security_owner_uid = capsule["security_owner_uid"]
    if not isinstance(security_owner_uid, int) or security_owner_uid < 0 or security_owner_uid == os.getuid():
        raise ValueError("Terraform capsule must be owned by a distinct security identity")
    capsule_files: list[SealedFile] = []
    for item in capsule["files"]:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "purpose"}:
            raise ValueError("Terraform capsule file record differs")
        path = Path(item["path"])
        if not path.is_absolute() or item["purpose"] not in {
            "configuration", "dependency-lock", "provider-plugin", "backend-configuration"
        }:
            raise ValueError("Terraform capsule path or purpose differs")
        sealed = SealedFile(
            path,
            private=item["purpose"] == "backend-configuration",
            expected_uid=security_owner_uid,
        )
        if hashlib.sha256(sealed.bytes(512 * 1024 * 1024)).hexdigest() != item["sha256"]:
            raise ValueError("Terraform capsule file digest differs")
        capsule_files.append(sealed)
    capsule_manifest_sha256 = hashlib.sha256(canonical_bytes(capsule)).hexdigest()
    working_directory = Path(capsule["working_directory"])
    terraform_data_directory = Path(capsule["terraform_data_directory"])
    if not working_directory.is_absolute() or not terraform_data_directory.is_absolute():
        raise ValueError("Terraform capsule directories must be absolute")
    for directory in (working_directory, terraform_data_directory):
        details = directory.stat()
        if (
            details.st_uid != security_owner_uid
            or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not os.statvfs(directory).f_flag & getattr(os, "ST_RDONLY", 1)
        ):
            raise ValueError("Terraform capsule directories must be security-owned read-only mounts")
    working_directory_fd = os.open(
        working_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    data_directory_fd = os.open(
        terraform_data_directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TF_IN_AUTOMATION": "1",
        "TF_DATA_DIR": f"/proc/self/fd/{data_directory_fd}",
    }
    plan_actions = _plan_actions(
        terraform=terraform,
        plan=plan,
        environment=environment,
        working_directory_fd=working_directory_fd,
        data_directory_fd=data_directory_fd,
    )
    kubernetes = Kubernetes(
        url=args.kubernetes_url, token=token, ca_file=ca_file
    )
    _, server_now = kubernetes.request("GET", "/version")
    envelope = _loads_unique(authorization.bytes(1024 * 1024))
    trust = _loads_unique(trust_file.bytes(1024 * 1024))
    subject = _verify_authorization(
        envelope=envelope, trust=trust, plan_sha256=plan_sha256, server_now=server_now
    )
    if (
        subject["terraform_sha256"] != terraform_sha256
        or subject["capsule_manifest_sha256"] != capsule_manifest_sha256
        or subject["plan_actions"] != plan_actions
        or subject["plan_actions_sha256"]
        != hashlib.sha256(canonical_bytes(plan_actions)).hexdigest()
        or any(
            row["actions"] in (["create"], ["update"])
            and row["observed_fence_nonce"] != subject["nonce"]
            for row in plan_actions
        )
        or subject["allowed_root_actions"]
        != sorted(
            [
                row["nim_root_action"]
                for row in plan_actions
                if row["actions"] in (["create"], ["update"])
            ],
            key=canonical_bytes,
        )
    ):
        raise ValueError("apply authorization does not bind the executable action inventory")
    _publish_authorization_record(
        kubernetes, envelope=envelope, subject=subject
    )
    first = _observe(kubernetes, subject["objects"])
    lease_resource_version = _acquire_lease(kubernetes, subject)
    second = _observe(kubernetes, subject["objects"])
    if first != second:
        raise RuntimeError("apply-fence observations changed while acquiring the writer fence")

    stop = threading.Event()
    renewal_failed = threading.Event()

    def renew() -> None:
        nonlocal lease_resource_version
        path = f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE_NAME}"
        while not stop.wait(max(10, subject["lease_duration_seconds"] // 3)):
            try:
                _, now = kubernetes.request("GET", "/version")
                _verify_authorization(
                    envelope=envelope,
                    trust=trust,
                    plan_sha256=plan_sha256,
                    server_now=now,
                )
                _observe(kubernetes, subject["objects"])
                authorization.assert_unchanged()
                trust_file.assert_unchanged()
                plan.assert_unchanged()
                terraform.assert_unchanged()
                capsule_file.assert_unchanged()
                for sealed in capsule_files:
                    sealed.assert_unchanged()
                lease, now = kubernetes.request("GET", path)
                metadata = lease.get("metadata", {})
                spec = lease.get("spec", {})
                if (
                    metadata.get("uid") != subject["lease_uid"]
                    or metadata.get("labels", {}).get("fs2-serve.nebius.ai/apply-fence")
                    != "true"
                    or
                    metadata.get("resourceVersion") != lease_resource_version
                    or spec.get("holderIdentity") != subject["nonce"]
                ):
                    renewal_failed.set()
                    return
                lease["spec"] = {
                    **spec,
                    "renewTime": now.isoformat().replace("+00:00", "Z"),
                }
                committed, _ = kubernetes.request("PUT", path, body=lease)
                lease_resource_version = str(committed["metadata"]["resourceVersion"])
            except BaseException:
                renewal_failed.set()
                return

    thread = threading.Thread(target=renew, name="nim-apply-fence-renewal", daemon=True)
    thread.start()
    plan.assert_unchanged()
    terraform.assert_unchanged()
    child = subprocess.Popen(
        [terraform.proc_path, "apply", plan.proc_path],
        env=environment,
        pass_fds=(terraform.fd, plan.fd, working_directory_fd, data_directory_fd),
        cwd=f"/proc/self/fd/{working_directory_fd}",
        start_new_session=True,
    )
    try:
        while child.poll() is None:
            if renewal_failed.wait(timeout=0.5):
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise RuntimeError("apply fence was lost; the plan process group was reaped")
        _, settled_now = kubernetes.request("GET", "/version")
        _verify_authorization(
            envelope=envelope,
            trust=trust,
            plan_sha256=plan_sha256,
            server_now=settled_now,
        )
        if _observe(kubernetes, subject["objects"]) != second:
            raise RuntimeError("security boundary drifted before apply settlement")
        settled_lease, _ = kubernetes.request(
            "GET", f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE_NAME}"
        )
        if settled_lease.get("spec", {}).get("holderIdentity") != subject["nonce"]:
            raise RuntimeError("apply fence was lost before settlement")
        return int(child.returncode)
    finally:
        stop.set()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

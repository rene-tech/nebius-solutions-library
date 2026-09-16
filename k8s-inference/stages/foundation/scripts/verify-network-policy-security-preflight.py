#!/usr/bin/env python3
"""Plan-time proof for the external public-edge security boundary."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class PreflightError(RuntimeError):
    """The plan-time security boundary is not exact."""


def canonical(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def decode_base64url(value: Any, *, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PreflightError("security inventory contains invalid base64url data")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as error:
        raise PreflightError("security inventory contains invalid base64url data") from error
    if len(decoded) != size:
        raise PreflightError("security inventory cryptographic length is invalid")
    return decoded


def verified_subject_inventory(
    raw_inventory: str,
    recovery_public_key: str,
    *,
    cluster: tuple[str, str],
    rollback_valid_until: int,
    forbidden_usernames: set[str],
) -> tuple[list[dict[str, Any]], str]:
    try:
        envelope = json.loads(raw_inventory)
    except json.JSONDecodeError as error:
        raise PreflightError("security subject inventory is not valid JSON") from error
    if not isinstance(envelope, dict) or set(envelope) != {"signed", "signature"}:
        raise PreflightError("security subject inventory envelope is not exact")
    signed = envelope.get("signed")
    expected_fields = {
        "schema",
        "inventory_id",
        "complete",
        "cluster",
        "human_users",
        "human_groups",
        "issued_at",
        "expires_at",
        "signer_key_id",
    }
    if not isinstance(signed, dict) or set(signed) != expected_fields:
        raise PreflightError("security subject inventory fields are not exact")
    key_id = hashlib.sha256(recovery_public_key.encode()).hexdigest()
    try:
        Ed25519PublicKey.from_public_bytes(decode_base64url(recovery_public_key, size=32)).verify(
            decode_base64url(envelope.get("signature"), size=64),
            canonical(signed).encode(),
        )
    except InvalidSignature as error:
        raise PreflightError("security subject inventory signature is invalid") from error
    expected_cluster = {
        "api_server_sha256": hashlib.sha256(cluster[0].encode()).hexdigest(),
        "kube_system_uid": cluster[1],
    }
    now = dt.datetime.now(dt.UTC)
    issued = dt.datetime.fromisoformat(str(signed.get("issued_at", "")).replace("Z", "+00:00"))
    expires = dt.datetime.fromisoformat(str(signed.get("expires_at", "")).replace("Z", "+00:00"))
    users = signed.get("human_users")
    groups = signed.get("human_groups")
    if (
        signed.get("schema") != "fs2-serve.nebius.ai/security-subject-inventory/v1"
        or signed.get("complete") is not True
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(signed.get("inventory_id", "")))
        or signed.get("cluster") != expected_cluster
        or signed.get("signer_key_id") != key_id
        or issued.tzinfo is None
        or expires.tzinfo is None
        or issued.astimezone(dt.UTC) > now
        or expires.astimezone(dt.UTC) <= now
        or int(expires.timestamp()) < rollback_valid_until
        or not isinstance(users, list)
        or not users
        or not isinstance(groups, list)
        or not groups
    ):
        raise PreflightError("security subject inventory is incomplete, stale or for another cluster")
    if len(groups) != len(set(groups)) or any(
        not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group) for group in groups
    ):
        raise PreflightError("security subject group inventory is invalid")
    normalized_users: list[dict[str, Any]] = []
    for subject in users:
        if (
            not isinstance(subject, dict)
            or set(subject) != {"username", "groups"}
            or not re.fullmatch(r"[A-Za-z0-9:@._/-]{3,253}", str(subject.get("username", "")))
            or subject.get("username") in forbidden_usernames
            or not isinstance(subject.get("groups"), list)
            or not subject["groups"]
            or len(subject["groups"]) != len(set(subject["groups"]))
            or any(
                not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9:@._/-]{1,253}", group)
                for group in subject["groups"]
            )
        ):
            raise PreflightError("security subject user inventory is invalid")
        normalized_users.append({"username": subject["username"], "groups": sorted(subject["groups"])})
    if len({subject["username"] for subject in normalized_users}) != len(normalized_users):
        raise PreflightError("security subject user inventory contains duplicates")
    group_subjects = [
        {
            "username": f"fs2-security-group-probe-{hashlib.sha256(group.encode()).hexdigest()[:12]}",
            "groups": [group],
        }
        for group in sorted(groups)
    ]
    return normalized_users + group_subjects, hashlib.sha256(canonical(signed).encode()).hexdigest()


def run(kubeconfig: Path, context: str, *arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed kubectl and validated arguments
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, *arguments],
        input=input_text,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise PreflightError("Kubernetes boundary preflight failed closed")
    return result.stdout


def exact_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PreflightError("boundary kubeconfig is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise PreflightError("boundary kubeconfig ownership or mode is unsafe")


def user_info(kubeconfig: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(run(kubeconfig, context, "auth", "whoami", "-o", "json"))["status"]["userInfo"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PreflightError("boundary whoami response is incomplete") from error
    if (
        not isinstance(value.get("username"), str)
        or not value["username"]
        or not isinstance(value.get("uid"), str)
        or not value["uid"]
        or not isinstance(value.get("groups", []), list)
        or not isinstance(value.get("extra", {}), dict)
        or not all(isinstance(group, str) and group for group in value.get("groups", []))
        or not all(
            isinstance(key, str)
            and key
            and isinstance(items, list)
            and all(isinstance(item, str) for item in items)
            for key, items in value.get("extra", {}).items()
        )
    ):
        raise PreflightError("boundary whoami tuple is invalid")
    return {
        "username": value["username"],
        "uid": value["uid"],
        "groups": sorted(value.get("groups", [])),
        "extra": {key: sorted(items) for key, items in sorted(value.get("extra", {}).items())},
    }


def credential_expiry(kubeconfig: Path, context: str) -> int:
    try:
        config = json.loads(run(kubeconfig, context, "config", "view", "--minify", "--raw", "-o", "json"))
        users = config["users"]
        if len(users) != 1:
            raise PreflightError("boundary kubeconfig must contain one selected user")
        credential = users[0]["user"]
        token = credential.get("token")
        if isinstance(token, str) and token:
            parts = token.split(".")
            if len(parts) != 3:
                raise PreflightError("boundary bearer credential is not an expiring JWT")
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(payload.get("exp"), int):
                raise PreflightError("boundary JWT has no integer expiry")
            return int(payload["exp"])
        certificate = credential.get("client-certificate-data")
        if not isinstance(certificate, str) or not certificate:
            raise PreflightError("boundary credential has no cryptographically inspectable expiry")
        decoded = base64.b64decode(certificate, validate=True)
        result = subprocess.run(  # noqa: S603 -- fixed executable and input bytes
            ["openssl", "x509", "-noout", "-enddate"],
            input=decoded,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise PreflightError("boundary client certificate expiry is unreadable")
        not_after = result.stdout.decode("ascii").strip().removeprefix("notAfter=")
        return int(dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.UTC).timestamp())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PreflightError("boundary credential expiry proof is invalid") from error


def cluster_identity(kubeconfig: Path, context: str) -> tuple[str, str]:
    try:
        config = json.loads(run(kubeconfig, context, "config", "view", "--minify", "--raw", "-o", "json"))
        clusters = config["clusters"]
        server = clusters[0]["cluster"]["server"] if len(clusters) == 1 else None
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PreflightError("boundary kubeconfig cluster is incomplete") from error
    uid = run(kubeconfig, context, "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").strip()
    if not isinstance(server, str) or not server or not uid:
        raise PreflightError("boundary cluster identity is incomplete")
    return server, uid


def can_i(kubeconfig: Path, context: str, expected: str, *arguments: str) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed kubectl and validated exact arguments
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, "auth", "can-i", *arguments],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.stdout.strip() != expected or (expected == "yes" and result.returncode != 0):
        raise PreflightError("boundary authorization is broader or narrower than its contract")


def subject_denied(
    kubeconfig: Path,
    context: str,
    subject: dict[str, Any],
    *,
    verb: str,
    group: str,
    resource: str,
    namespace: str = "",
    name: str = "",
    subresource: str = "",
) -> None:
    attributes = {"verb": verb, "group": group, "resource": resource}
    for key, value in (("namespace", namespace), ("name", name), ("subresource", subresource)):
        if value:
            attributes[key] = value
    review = {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SubjectAccessReview",
        "spec": {
            "user": subject["username"],
            "groups": sorted(subject["groups"]),
            "resourceAttributes": attributes,
        },
    }
    try:
        response = json.loads(
            run(
                kubeconfig,
                context,
                "create",
                "--raw",
                "/apis/authorization.k8s.io/v1/subjectaccessreviews",
                "-f",
                "-",
                input_text=canonical(review),
            )
        )
    except json.JSONDecodeError as error:
        raise PreflightError("human-subject authorization review is invalid") from error
    if response.get("status", {}).get("allowed") is not False:
        raise PreflightError("a reviewed human subject can cross the security boundary")


def parse_query() -> dict[str, Any]:
    try:
        query = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise PreflightError("external preflight query is invalid") from error
    required = {
        "mode",
        "context",
        "kube_system_uid",
        "release_kubeconfig",
        "security_kubeconfig",
        "bootstrap_kubeconfig",
        "release_identity",
        "security_identity",
        "bootstrap_identity",
        "subject_inventory",
        "recovery_public_key",
        "minimum_rollback_seconds",
        "gateway_namespace",
        "controller_namespace",
        "security_owner_username",
        "security_bootstrap_username",
        "peer_uid",
        "peer_gid",
        "socket_path",
        "identity_epoch",
    }
    if (
        not isinstance(query, dict)
        or set(query) != required
        or not all(isinstance(value, str) for value in query.values())
    ):
        raise PreflightError("external preflight query fields are not exact strings")
    return query


def main() -> int:
    try:
        query = parse_query()
        paths = {
            role: Path(query[field])
            for role, field in (
                ("release", "release_kubeconfig"),
                ("security", "security_kubeconfig"),
                ("bootstrap", "bootstrap_kubeconfig"),
            )
        }
        if len({str(path) for path in paths.values()}) != 3 or not all(path.is_absolute() for path in paths.values()):
            raise PreflightError("boundary kubeconfig paths are not distinct absolute paths")
        for path in paths.values():
            exact_file(path)
        credential_hashes = {
            role: hashlib.sha256(path.read_bytes()).hexdigest() for role, path in sorted(paths.items())
        }
        credential_set_sha256 = hashlib.sha256(canonical(credential_hashes).encode()).hexdigest()
        if query["mode"] == "public":
            socket_path = Path(query["socket_path"])
            socket_parent = socket_path.parent
            try:
                socket_parent_metadata = socket_parent.lstat()
            except OSError as error:
                raise PreflightError("versioned security socket parent is unavailable") from error
            try:
                socket_path.lstat()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise PreflightError("versioned security socket path cannot be inspected") from error
            else:
                raise PreflightError("versioned security socket path must be absent")
            if (
                not socket_path.is_absolute()
                or socket_parent.name != query["identity_epoch"]
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", query["identity_epoch"])
                or not stat.S_ISDIR(socket_parent_metadata.st_mode)
                or stat.S_ISLNK(socket_parent_metadata.st_mode)
                or socket_parent_metadata.st_uid != os.geteuid()
                or socket_parent_metadata.st_gid != int(query["peer_gid"])
                or stat.S_IMODE(socket_parent_metadata.st_mode) != 0o2710
            ):
                raise PreflightError("versioned security socket parent is not the exact setgid contract")
        clusters = {role: cluster_identity(path, query["context"]) for role, path in paths.items()}
        if len(set(clusters.values())) != 1 or next(iter(clusters.values()))[1] != query["kube_system_uid"]:
            raise PreflightError("boundary identities are not bound to the same reviewed cluster")

        if query["mode"] == "public":
            expected = {
                role: json.loads(query[field])
                for role, field in (
                    ("release", "release_identity"),
                    ("security", "security_identity"),
                    ("bootstrap", "bootstrap_identity"),
                )
            }
            actual = {role: user_info(path, query["context"]) for role, path in paths.items()}
            normalized_expected = {
                role: {
                    "username": value["username"],
                    "uid": value["uid"],
                    "groups": sorted(value["groups"]),
                    "extra": {key: sorted(items) for key, items in sorted(value["extra"].items())},
                }
                for role, value in expected.items()
            }
            if actual != normalized_expected:
                raise PreflightError("live whoami tuples do not match the reviewed identities")
            if len({value["username"] for value in actual.values()}) != 3 or len(
                {value["uid"] for value in actual.values()}
            ) != 3:
                raise PreflightError("release, security and bootstrap identities are not disjoint")
            allowed_shared = {"system:authenticated", "system:serviceaccounts"}
            roles = list(actual)
            for index, left in enumerate(roles):
                for right in roles[index + 1 :]:
                    if (set(actual[left]["groups"]) & set(actual[right]["groups"])) - allowed_shared:
                        raise PreflightError("boundary identities share an unreviewed group")
            now = int(dt.datetime.now(dt.UTC).timestamp())
            limits = {"release": 28800, "security": 28800, "bootstrap": 900}
            expiries: dict[str, int] = {}
            for role, value in expected.items():
                configured = int(dt.datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")).timestamp())
                expiries[role] = configured
                if credential_expiry(paths[role], query["context"]) != configured:
                    raise PreflightError("configured identity expiry is not cryptographically bound")
                if not now < configured <= now + limits[role]:
                    raise PreflightError("boundary credential lifetime exceeds its limit")
            minimum_rollback_seconds = int(query["minimum_rollback_seconds"])
            rollback_valid_until = min(expiries["release"], expiries["security"])
            if (
                minimum_rollback_seconds < 3600
                or rollback_valid_until < expiries["bootstrap"] + minimum_rollback_seconds
            ):
                raise PreflightError("boundary credentials do not preserve the minimum post-bootstrap rollback window")
            humans, inventory_sha256 = verified_subject_inventory(
                query["subject_inventory"],
                query["recovery_public_key"],
                cluster=next(iter(clusters.values())),
                rollback_valid_until=rollback_valid_until,
                forbidden_usernames={value["username"] for value in actual.values()},
            )
        else:
            humans = []
            inventory_sha256 = hashlib.sha256(b"internal-only").hexdigest()

        release = paths["release"]
        security = paths["security"]
        bootstrap = paths["bootstrap"]
        context = query["context"]
        protected_cluster = (
            "validatingadmissionpolicies.admissionregistration.k8s.io",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io",
        )
        for resource in protected_cluster:
            for verb in ("patch", "update"):
                can_i(release, context, "no", verb, resource, "--resource-name=fs2-network-policy-boundary")
                can_i(security, context, "no", verb, resource)
                can_i(security, context, "yes", verb, resource, "--resource-name=fs2-network-policy-boundary")
                can_i(bootstrap, context, "no", verb, resource)
                can_i(bootstrap, context, "yes", verb, resource, "--resource-name=fs2-network-policy-boundary")
            can_i(release, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(security, context, "no", "delete", resource)
            can_i(security, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(bootstrap, context, "no", "delete", resource, "--resource-name=fs2-network-policy-boundary")
            can_i(release, context, "no", "deletecollection", resource)
            can_i(security, context, "no", "deletecollection", resource)
            can_i(bootstrap, context, "no", "deletecollection", resource)
            can_i(security, context, "no", "create", resource)
            can_i(bootstrap, context, "yes", "create", resource)

        namespaced = (
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-public-envoy-transition-guard",
                query["gateway_namespace"],
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-default-deny",
                query["gateway_namespace"],
            ),
            (
                "networkpolicies.networking.k8s.io",
                "fs2-serve-control-plane-envoy-controller-xds-transition-guard",
                query["controller_namespace"],
            ),
            ("configmaps", "fs2-network-policy-transition", "fs2-system"),
            ("configmaps", "fs2-network-policy-boundary-topology", "fs2-system"),
            ("configmaps", "fs2-network-policy-boundary-parameters", "fs2-system"),
            ("leases.coordination.k8s.io", "fs2-network-policy-transition", "fs2-system"),
        )
        for resource, name, namespace in namespaced:
            for verb in ("patch", "update"):
                can_i(release, context, "no", verb, resource, f"--resource-name={name}", "--namespace", namespace)
                can_i(security, context, "no", verb, resource, "--namespace", namespace)
                can_i(
                    security,
                    context,
                    "no" if name == "fs2-network-policy-boundary-topology" else "yes",
                    verb,
                    resource,
                    f"--resource-name={name}",
                    "--namespace",
                    namespace,
                )
                can_i(bootstrap, context, "no", verb, resource, "--namespace", namespace)
                can_i(bootstrap, context, "yes", verb, resource, f"--resource-name={name}", "--namespace", namespace)
            for verb in ("delete", "deletecollection"):
                arguments = (verb, resource, "--namespace", namespace)
                can_i(security, context, "no", *arguments)
                can_i(bootstrap, context, "no", *arguments)
            can_i(release, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)
            can_i(security, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)
            can_i(bootstrap, context, "no", "delete", resource, f"--resource-name={name}", "--namespace", namespace)

        identities = (release, security, bootstrap)
        impersonation_targets = (
            ("users.authentication.k8s.io", query["security_owner_username"]),
            ("users.authentication.k8s.io", query["security_bootstrap_username"]),
            ("users.authentication.k8s.io", "fs2-network-policy-security-probe"),
            ("groups.authentication.k8s.io", "system:masters"),
            ("groups.authentication.k8s.io", "system:authenticated"),
            ("groups.authentication.k8s.io", "system:serviceaccounts"),
            ("groups.authentication.k8s.io", "system:serviceaccounts:fs2-system"),
            (
                "serviceaccounts.authentication.k8s.io",
                "system:serviceaccount:fs2-system:fs2-network-policy-transition",
            ),
            ("uids.authentication.k8s.io", query["peer_uid"]),
            ("userextras.authentication.k8s.io", "scopes"),
        )
        for identity in identities:
            for resource in (
                "users.authentication.k8s.io",
                "groups.authentication.k8s.io",
                "serviceaccounts.authentication.k8s.io",
                "uids.authentication.k8s.io",
                "userextras.authentication.k8s.io",
            ):
                can_i(identity, context, "no", "impersonate", resource)
            for resource, name in impersonation_targets:
                can_i(identity, context, "no", "impersonate", resource, f"--resource-name={name}")
            can_i(
                identity,
                context,
                "no",
                "create",
                "serviceaccounts",
                "--subresource=token",
                "--namespace",
                "fs2-system",
            )
            can_i(
                identity,
                context,
                "no",
                "create",
                "serviceaccounts",
                "--subresource=token",
                "--all-namespaces",
            )
            for namespace in {"fs2-system", query["gateway_namespace"], query["controller_namespace"]}:
                can_i(
                    identity,
                    context,
                    "no",
                    "create",
                    "serviceaccounts",
                    "--subresource=token",
                    "--namespace",
                    namespace,
                )
            can_i(
                identity,
                context,
                "no",
                "create",
                "serviceaccounts",
                "--resource-name=fs2-network-policy-transition",
                "--subresource=token",
                "--namespace",
                "fs2-system",
            )
        for identity in (release, security, bootstrap):
            for verb in ("bind", "escalate"):
                for resource in ("clusterroles.rbac.authorization.k8s.io", "roles.rbac.authorization.k8s.io"):
                    can_i(identity, context, "no", verb, resource)
                can_i(
                    identity,
                    context,
                    "no",
                    verb,
                    "clusterroles.rbac.authorization.k8s.io",
                    f"--resource-name={query['security_owner_username']}",
                )
                can_i(
                    identity,
                    context,
                    "no",
                    verb,
                    "roles.rbac.authorization.k8s.io",
                    "--resource-name=fs2-network-policy-transition",
                    "--namespace",
                    "fs2-system",
                )
            for namespace in {"fs2-system", query["gateway_namespace"], query["controller_namespace"]}:
                for verb in ("update", "delete"):
                    can_i(identity, context, "no", verb, "namespaces", f"--resource-name={namespace}")
                can_i(identity, context, "no", "update", "namespaces/finalize", f"--resource-name={namespace}")

        can_i(bootstrap, context, "yes", "create", "subjectaccessreviews.authorization.k8s.io")
        for subject in humans:
            for resource, name, namespace in namespaced:
                if resource.startswith("networkpolicies."):
                    group, short_resource = "networking.k8s.io", "networkpolicies"
                elif resource.startswith("leases."):
                    group, short_resource = "coordination.k8s.io", "leases"
                else:
                    group, short_resource = "", resource
                for verb in ("patch", "update", "delete"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group=group,
                        resource=short_resource,
                        namespace=namespace,
                        name=name,
                    )
            for resource in ("validatingadmissionpolicies", "validatingadmissionpolicybindings"):
                for verb in ("patch", "update", "delete"):
                    subject_denied(
                        bootstrap,
                        context,
                        subject,
                        verb=verb,
                        group="admissionregistration.k8s.io",
                        resource=resource,
                        name="fs2-network-policy-boundary",
                    )
            for namespace in {"fs2-system", query["gateway_namespace"], query["controller_namespace"]}:
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="update",
                    group="",
                    resource="namespaces",
                    name=namespace,
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="delete",
                    group="",
                    resource="namespaces",
                    name=namespace,
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="update",
                    group="",
                    resource="namespaces",
                    name=namespace,
                    subresource="finalize",
                )
            for namespace in ("", "fs2-system", query["gateway_namespace"], query["controller_namespace"]):
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb="create",
                    group="",
                    resource="serviceaccounts",
                    namespace=namespace,
                    name="fs2-network-policy-transition",
                    subresource="token",
                )
            for group, resource in (
                ("authentication.k8s.io", "users"),
                ("authentication.k8s.io", "groups"),
                ("authentication.k8s.io", "serviceaccounts"),
                ("authentication.k8s.io", "uids"),
                ("authentication.k8s.io", "userextras"),
            ):
                subject_denied(bootstrap, context, subject, verb="impersonate", group=group, resource=resource)
            for verb in ("bind", "escalate"):
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb=verb,
                    group="rbac.authorization.k8s.io",
                    resource="clusterroles",
                )
                subject_denied(
                    bootstrap,
                    context,
                    subject,
                    verb=verb,
                    group="rbac.authorization.k8s.io",
                    resource="roles",
                    namespace="fs2-system",
                )

        digest = hashlib.sha256(
            canonical({"query": query, "credential_set_sha256": credential_set_sha256}).encode()
        ).hexdigest()
        print(
            canonical(
                {
                    "verified": "true",
                    "contract_sha256": digest,
                    "subject_inventory_sha256": inventory_sha256,
                    "credential_set_sha256": credential_set_sha256,
                    "release_kubeconfig_sha256": credential_hashes["release"],
                    "security_kubeconfig_sha256": credential_hashes["security"],
                    "bootstrap_kubeconfig_sha256": credential_hashes["bootstrap"],
                }
            )
        )
        return 0
    except (OSError, PreflightError, ValueError, json.JSONDecodeError) as error:
        print(f"network-policy security preflight failed closed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

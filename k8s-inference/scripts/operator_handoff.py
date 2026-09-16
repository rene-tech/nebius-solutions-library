#!/usr/bin/env python3
"""Acknowledge and verify an externally issued least-privilege handoff.

The private key and append-only receipts stay in one owner-only directory.
Command output is limited to non-secret status and resource identifiers; no
Secret data is read. This executable has no issuance, profile creation,
credential creation, revocation, or cleanup command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.append_only_evidence import EvidenceError, append_event, latest_state


FIXED_NEBIUS = "/usr/local/bin/nebius"
FIXED_KUBECTL = "/snap/bin/kubectl"
FIXED_OPENSSL = "/usr/bin/openssl"
FIXED_ADMIN_PROFILE = "sandbox"
PRODUCTION_AUTHORITY_COMMAND = (
    "/usr/bin/python3",
    str(Path(__file__).resolve().parent / "credential_provider_adapter.py"),
)


class HandoffError(RuntimeError):
    pass


SAFE_SELF_REVIEW_MUTATIONS = frozenset(
    {
        "selfsubjectaccessreviews.authorization.k8s.io",
        "selfsubjectrulesreviews.authorization.k8s.io",
        "selfsubjectreviews.authentication.k8s.io",
    }
)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise HandoffError("expiry must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0)


def host_cidrs(values: Sequence[str]) -> list[str]:
    normalized: set[str] = set()
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise HandoffError(f"invalid approved egress CIDR: {value}") from exc
        if network.prefixlen != network.max_prefixlen:
            raise HandoffError(
                "approved egress entries must be exact IPv4 /32 or IPv6 /128 hosts"
            )
        normalized.add(network.with_prefixlen)
    if not 1 <= len(normalized) <= 8:
        raise HandoffError("one to eight approved egress hosts are required")
    return sorted(normalized)


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if not path.exists() or path.is_symlink() or not path.is_dir():
        raise HandoffError("handoff directory must be a real directory")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise HandoffError("handoff directory path must not contain a symlink")
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise HandoffError("handoff directory must be owner-owned mode 0700")
    return path


def private_json(path: Path, value: Any) -> None:
    """Append a full state snapshot to an immutable hash-chained stream."""

    stream = f"operator-handoff:{path.name}"
    event = (
        value.get("phase")
        or value.get("status")
        or ("receipt-state" if path.name == "receipt.json" else "state")
    )
    try:
        append_event(
            path.with_name(f"{path.name}.events"),
            stream=stream,
            event=event,
            state=value,
        )
    except EvidenceError as error:
        raise HandoffError(str(error)) from error


@contextmanager
def handoff_lock(directory: Path):
    lock_path = directory / "handoff.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run(
    arguments: Sequence[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    lowered = tuple(value.lower() for value in arguments)
    if arguments and arguments[0] == FIXED_NEBIUS and any(
        verb in lowered for verb in ("create", "delete", "revoke", "update", "set")
    ):
        raise HandoffError("operator handoff tool forbids Nebius mutation commands")
    try:
        return subprocess.run(
            list(arguments),
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise HandoffError(
            f"handoff command failed: {arguments[0]} {arguments[1]}"
        ) from exc


def authority_json(request: dict[str, Any]) -> dict[str, Any]:
    """Call the fixed read-only authority; no caller command or profile exists."""

    try:
        completed = subprocess.run(
            list(PRODUCTION_AUTHORITY_COMMAND),
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            check=True,
        )
        document = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise HandoffError("operator handoff authority did not return evidence") from error
    if not isinstance(document, dict):
        raise HandoffError("operator handoff authority returned malformed evidence")
    return document


def load_receipt(directory: Path) -> tuple[Path, dict[str, Any]]:
    receipt_path = directory / "receipt.json"
    stream_path = receipt_path.with_name(f"{receipt_path.name}.events")
    try:
        value = latest_state(
            stream_path, stream=f"operator-handoff:{receipt_path.name}"
        )
    except EvidenceError as error:
        raise HandoffError("owner receipt is absent or unsafe") from error
    if value.get("schema") not in {
        "fs2-serve.nebius.ai/operator-handoff/v2",
        "fs2-serve.nebius.ai/operator-handoff/v3",
        "fs2-serve.nebius.ai/operator-handoff/v4",
    }:
        raise HandoffError("owner receipt has the wrong schema")
    return receipt_path, value


def auth_key_binding(document: Any) -> dict[str, str | None]:
    """Return the provider-observed identity of one authentication key."""

    if not isinstance(document, dict):
        raise HandoffError("Nebius returned a malformed authentication key")
    metadata = document.get("metadata")
    spec = document.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise HandoffError("Nebius authentication key lacks metadata or spec")
    account = spec.get("account")
    service_account = (
        account.get("service_account") if isinstance(account, dict) else None
    )
    binding = {
        "public_key_id": metadata.get("id"),
        "service_account_id": service_account.get("id")
        if isinstance(service_account, dict)
        else None,
        "project_id": metadata.get("parent_id"),
        "expires_at": spec.get("expires_at"),
        "name": metadata.get("name"),
        "lineage_id": (
            metadata.get("labels", {}).get("handoff_lineage")
            if isinstance(metadata.get("labels"), dict)
            else None
        ),
        "generation": (
            metadata.get("labels", {}).get("handoff_generation")
            if isinstance(metadata.get("labels"), dict)
            else None
        ),
        "public_key_sha256": (
            hashlib.sha256(spec["data"].encode()).hexdigest()
            if isinstance(spec.get("data"), str)
            else None
        ),
    }
    if not all(
        isinstance(binding[key], str) and binding[key]
        for key in ("public_key_id", "service_account_id", "project_id")
    ):
        raise HandoffError("Nebius authentication key has an incomplete identity")
    if binding["expires_at"] is not None and not isinstance(binding["expires_at"], str):
        raise HandoffError("Nebius authentication key has a malformed expiry")
    return binding


def provider_json(
    args: argparse.Namespace, arguments: Sequence[str], label: str
) -> Any:
    result = run(
        [args.nebius, *arguments, "--profile", args.admin_profile, "--format", "json"],
        capture=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HandoffError(f"Nebius returned an unreadable {label}") from error


def list_items(document: Any, *, label: str) -> list[dict[str, Any]]:
    if isinstance(document, list):
        items = document
    elif isinstance(document, dict):
        items = next(
            (value for value in document.values() if isinstance(value, list)), None
        )
    else:
        items = None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise HandoffError(f"Nebius returned a malformed {label} inventory")
    return items


def service_account_lineage(
    args: argparse.Namespace,
    *,
    service_account_id: str,
    group_id: str,
    project_id: str,
    lineage_id: str,
    generation: int,
    required_role: str,
) -> dict[str, Any]:
    """Derive handoff identity, membership and role exclusively from Nebius."""

    expected_purpose = (
        "operator-handoff-viewer"
        if required_role == "viewer"
        else "operator-handoff-admin"
    )
    account = provider_json(
        args,
        ["iam", "service-account", "get", "--id", service_account_id],
        "service account",
    )
    metadata = account.get("metadata") if isinstance(account, dict) else None
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("id") != service_account_id
        or metadata.get("parent_id") != project_id
        or not isinstance(labels, dict)
        or labels.get("purpose") != expected_purpose
        or labels.get("handoff_lineage") != lineage_id
        or labels.get("handoff_generation") != str(generation)
    ):
        raise HandoffError(
            "service account is outside the provider-derived handoff lineage"
        )

    memberships = list_items(
        provider_json(
            args,
            ["iam", "group-membership", "list", "--parent-id", group_id, "--all"],
            "group membership",
        ),
        label="group membership",
    )
    members = {
        item.get("member_id")
        or (
            item.get("spec", {}).get("member_id")
            if isinstance(item.get("spec"), dict)
            else None
        )
        for item in memberships
    }
    if members != {service_account_id}:
        raise HandoffError(
            "handoff group does not contain exactly its provider-derived service account"
        )

    account_memberships = list_items(
        provider_json(
            args,
            [
                "iam",
                "group-membership",
                "list",
                "--member-id",
                service_account_id,
                "--all",
            ],
            "service-account group membership",
        ),
        label="service-account group membership",
    )
    account_group_ids = {
        (
            item.get("metadata", {}).get("parent_id")
            if isinstance(item.get("metadata"), dict)
            else None
        )
        or item.get("group_id")
        or (
            item.get("spec", {}).get("group_id")
            if isinstance(item.get("spec"), dict)
            else None
        )
        for item in account_memberships
    }
    if account_group_ids != {group_id}:
        raise HandoffError(
            "handoff service account has an unreviewed provider group membership"
        )

    permits = list_items(
        provider_json(
            args,
            ["iam", "access-permit", "list", "--parent-id", group_id, "--all"],
            "access permit",
        ),
        label="access permit",
    )
    normalized = {
        (
            item.get("resource_id")
            or (
                item.get("spec", {}).get("resource_id")
                if isinstance(item.get("spec"), dict)
                else None
            ),
            item.get("role")
            or (
                item.get("spec", {}).get("role")
                if isinstance(item.get("spec"), dict)
                else None
            ),
        )
        for item in permits
    }
    if normalized != {(project_id, required_role)}:
        raise HandoffError(
            "handoff group does not have exactly the required project role"
        )
    return {
        "service_account_id": service_account_id,
        "group_id": group_id,
        "project_id": project_id,
        "lineage_id": lineage_id,
        "generation": generation,
        "role": required_role,
    }


def read_auth_key(
    args: argparse.Namespace, public_key_id: str
) -> dict[str, str | None]:
    result = run(
        [
            args.nebius,
            "iam",
            "auth-public-key",
            "get",
            "--profile",
            args.admin_profile,
            "--id",
            public_key_id,
            "--format",
            "json",
        ],
        capture=True,
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HandoffError(
            "Nebius returned an unreadable authentication key"
        ) from error
    return auth_key_binding(document)


def require_binding(
    observed: dict[str, str | None],
    *,
    public_key_id: str,
    service_account_id: str,
    project_id: str,
    expires_at: str | None = None,
) -> None:
    expected = {
        "public_key_id": public_key_id,
        "service_account_id": service_account_id,
        "project_id": project_id,
    }
    if any(observed[key] != value for key, value in expected.items()):
        raise HandoffError("authentication key identity differs from its bound receipt")
    if expires_at is not None:
        observed_expiry = observed["expires_at"]
        if not isinstance(observed_expiry, str) or timestamp(
            observed_expiry
        ) != timestamp(expires_at):
            raise HandoffError(
                "provider-enforced authentication key expiry differs from the request"
            )


def listed_auth_key_ids(document: Any) -> set[str]:
    if isinstance(document, list):
        items = document
    elif isinstance(document, dict):
        items = next(
            (
                document[key]
                for key in ("items", "auth_public_keys", "authPublicKeys")
                if isinstance(document.get(key), list)
            ),
            None,
        )
        if items is None:
            raise HandoffError(
                "Nebius returned a malformed authentication key inventory"
            )
    else:
        raise HandoffError("Nebius returned a malformed authentication key inventory")
    identifiers: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise HandoffError(
                "Nebius returned a malformed authentication key inventory item"
            )
        metadata = item.get("metadata")
        identifier = (
            metadata.get("id") if isinstance(metadata, dict) else item.get("id")
        )
        if not isinstance(identifier, str) or not identifier:
            raise HandoffError("Nebius authentication key inventory item has no ID")
        identifiers.add(identifier)
    return identifiers


def authorization_rules(output: str) -> list[dict[str, Any]]:
    """Parse kubectl's stable no-header SelfSubjectRulesReview table."""

    rules: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith(("warning:", "resources")):
            continue
        opening = line.rfind("[")
        if opening < 0 or not line.endswith("]"):
            raise HandoffError("kubectl returned an unreadable authorization rule")
        verbs = line[opening + 1 : -1].split()
        fields = line[:opening].split()
        if len(fields) < 3 or not verbs:
            raise HandoffError("kubectl returned an incomplete authorization rule")
        rules.append({"resource": fields[0], "verbs": verbs})
    if not rules:
        raise HandoffError("kubectl returned no authorization rules")
    return rules


def require_viewer_rules(rules: list[dict[str, Any]]) -> None:
    mutation_verbs = {
        "*",
        "approve",
        "bind",
        "create",
        "delete",
        "deletecollection",
        "escalate",
        "impersonate",
        "patch",
        "sign",
        "update",
    }
    secret_read_verbs = {"*", "get", "list", "watch"}
    for rule in rules:
        verbs = set(rule["verbs"])
        resource = rule["resource"]
        observed_mutations = verbs & mutation_verbs
        if observed_mutations and not (
            resource in SAFE_SELF_REVIEW_MUTATIONS and observed_mutations == {"create"}
        ):
            raise HandoffError(
                "viewer authorization inventory contains mutation access"
            )
        if "secret" in resource.lower() and verbs & secret_read_verbs:
            raise HandoffError(
                "viewer authorization inventory contains Secret read access"
            )


def negative_authorization_probes() -> dict[str, list[str]]:
    """Enumerate durable mutation, escalation, impersonation, and Secret checks."""

    probes: dict[str, list[str]] = {}
    namespaced_resources = (
        "pods",
        "deployments.apps",
        "statefulsets.apps",
        "daemonsets.apps",
        "replicasets.apps",
        "jobs.batch",
        "cronjobs.batch",
        "configmaps",
        "services",
        "persistentvolumeclaims",
        "roles.rbac.authorization.k8s.io",
        "rolebindings.rbac.authorization.k8s.io",
    )
    mutations = ("create", "update", "patch", "delete", "deletecollection")
    for resource in namespaced_resources:
        for verb in mutations:
            probes[f"{verb}_{resource}"] = [verb, resource, "--all-namespaces"]
    for resource in (
        "clusterroles.rbac.authorization.k8s.io",
        "clusterrolebindings.rbac.authorization.k8s.io",
    ):
        for verb in mutations:
            probes[f"{verb}_{resource}"] = [verb, resource]
    for resource in ("pods/exec", "pods/attach", "pods/portforward"):
        probes[f"create_{resource}"] = ["create", resource, "--all-namespaces"]
    probes["create_serviceaccounts_token"] = [
        "create",
        "serviceaccounts/token",
        "--all-namespaces",
    ]
    probes["create_tokenreviews"] = [
        "create",
        "tokenreviews.authentication.k8s.io",
    ]
    probes["escalate_roles"] = [
        "escalate",
        "roles.rbac.authorization.k8s.io",
        "--all-namespaces",
    ]
    probes["bind_roles"] = [
        "bind",
        "roles.rbac.authorization.k8s.io",
        "--all-namespaces",
    ]
    probes["escalate_clusterroles"] = [
        "escalate",
        "clusterroles.rbac.authorization.k8s.io",
    ]
    probes["bind_clusterroles"] = [
        "bind",
        "clusterroles.rbac.authorization.k8s.io",
    ]
    for resource in ("users", "groups", "serviceaccounts"):
        probes[f"impersonate_{resource}"] = ["impersonate", resource]
    for verb in (
        "get",
        "list",
        "watch",
        "create",
        "update",
        "patch",
        "delete",
        "deletecollection",
    ):
        probes[f"{verb}_secrets"] = [verb, "secrets", "--all-namespaces"]
    return probes


def read_auth_key_ids(args: argparse.Namespace, project_id: str) -> set[str]:
    result = run(
        [
            args.nebius,
            "iam",
            "auth-public-key",
            "list",
            "--profile",
            args.admin_profile,
            "--parent-id",
            project_id,
            "--all",
            "--format",
            "json",
        ],
        capture=True,
    )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HandoffError(
            "Nebius returned an unreadable authentication key inventory"
        ) from error
    return listed_auth_key_ids(document)


def require_authoritative_not_found(result: subprocess.CompletedProcess[str]) -> None:
    """Accept provider absence only when the provider explicitly reports it.

    A non-zero exit alone is not absence: authentication, transport, throttling,
    and server failures must leave the revocation journal pending so an operator
    can reconcile them safely.
    """

    if result.returncode == 0:
        raise HandoffError("revoked predecessor is still returned by authoritative get")
    diagnostic = (result.stderr or "").strip().lower()
    normalized = diagnostic.replace("_", " ").replace("-", " ")
    if not any(
        marker in normalized
        for marker in (
            "not found",
            "does not exist",
            "no such auth public key",
        )
    ):
        raise HandoffError(
            "authoritative predecessor absence was not proven by provider get"
        )


def read_auth_keys(
    args: argparse.Namespace, project_id: str
) -> list[dict[str, str | None]]:
    document = provider_json(
        args,
        [
            "iam",
            "auth-public-key",
            "list",
            "--parent-id",
            project_id,
            "--all",
        ],
        "authentication key inventory",
    )
    return [
        auth_key_binding(item)
        for item in list_items(document, label="authentication key")
    ]


def reconcile_issued_key(
    args: argparse.Namespace, journal: dict[str, Any]
) -> dict[str, str | None] | None:
    matches = []
    for binding in read_auth_keys(args, journal["project_id"]):
        if (
            binding["service_account_id"] == journal["service_account_id"]
            and binding["project_id"] == journal["project_id"]
            and binding["name"] == journal["name"]
            and binding["lineage_id"] == journal["lineage_id"]
            and binding["generation"] == str(journal["generation"])
            and binding["expires_at"] is not None
            and timestamp(binding["expires_at"]) == timestamp(journal["expires_at"])
            and binding["public_key_sha256"] == journal["public_key_sha256"]
        ):
            matches.append(binding)
    if len(matches) > 1:
        raise HandoffError("provider reconciliation found duplicate handoff keys")
    return matches[0] if matches else None


def _issue(args: argparse.Namespace) -> dict[str, Any]:
    raise HandoffError(
        "disabled legacy implementation: this read-only executable cannot issue credentials"
    )

    # Preserved only as historical source context until a separately reviewed
    # issuance service lands. The unconditional refusal above and the absence
    # of an issue CLI route make the block non-executable.
    directory = private_directory(args.directory)
    journal_path = directory / "issuance.journal.json"
    expiry = timestamp(args.expires_at)
    now = utc_now()
    if expiry <= now or expiry > now + timedelta(days=366):
        raise HandoffError(
            "handoff expiry must be future and no more than one year away"
        )
    if args.generation != args.predecessor_generation + 1:
        raise HandoffError("handoff generations must advance by exactly one")
    successor_lineage = service_account_lineage(
        args,
        service_account_id=args.service_account_id,
        group_id=args.viewer_group_id,
        project_id=args.project_id,
        lineage_id=args.lineage_id,
        generation=args.generation,
        required_role="viewer",
    )
    predecessor_lineage = service_account_lineage(
        args,
        service_account_id=args.predecessor_service_account_id,
        group_id=args.predecessor_group_id,
        project_id=args.predecessor_project_id,
        lineage_id=args.lineage_id,
        generation=args.predecessor_generation,
        required_role=args.predecessor_role,
    )
    predecessor = read_auth_key(args, args.predecessor_public_key_id)
    require_binding(
        predecessor,
        public_key_id=args.predecessor_public_key_id,
        service_account_id=args.predecessor_service_account_id,
        project_id=args.predecessor_project_id,
    )
    private_key, public_key = (
        directory / "private-key.pem",
        directory / "public-key.pem",
    )
    if any(
        path.exists()
        for path in (
            private_key,
            public_key,
            directory / "receipt.json.events",
            directory / "issuance.journal.json.events",
        )
    ):
        raise HandoffError("handoff directory already contains key or receipt material")
    run(
        [
            args.openssl,
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:4096",
            "-out",
            str(private_key),
        ]
    )
    private_key.chmod(0o600)
    run(
        [
            args.openssl,
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ]
    )
    public_key.chmod(0o600)

    public_key_text = public_key.read_text(encoding="ascii")
    public_key_sha256 = hashlib.sha256(public_key_text.encode()).hexdigest()
    journal = {
        "schema": "fs2-serve.nebius.ai/operator-handoff-issuance/v1",
        "phase": "create-pending",
        "name": args.name,
        "lineage_id": args.lineage_id,
        "generation": args.generation,
        "service_account_id": args.service_account_id,
        "project_id": args.project_id,
        "cluster_id": args.cluster_id,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "public_key_sha256": public_key_sha256,
        "predecessor": predecessor,
        "predecessor_lineage": predecessor_lineage,
        "successor_lineage": successor_lineage,
        "public_key_id": None,
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
    }
    private_json(journal_path, journal)

    request = {
        "metadata": {
            "parent_id": args.project_id,
            "name": args.name,
            "labels": {
                "handoff_lineage": args.lineage_id,
                "handoff_generation": str(args.generation),
            },
        },
        "spec": {
            "account": {"service_account": {"id": args.service_account_id}},
            "data": public_key_text,
            "description": "Expiring viewer-only Kubernetes operator handoff",
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        },
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    request_path = directory / f"issuance-request-{request_sha256}.json"
    descriptor = os.open(
        request_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(request, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        result = run(
            [
                args.nebius,
                "iam",
                "auth-public-key",
                "create",
                "--profile",
                args.admin_profile,
                "--file",
                str(request_path),
                "--format",
                "json",
            ],
            capture=True,
        )
        created = json.loads(result.stdout)
    except BaseException:
        journal["phase"] = "create-uncertain"
        journal["request_sha256"] = request_sha256
        private_json(journal_path, journal)
        reconciled = reconcile_issued_key(args, journal)
        if reconciled is not None:
            journal["public_key_id"] = reconciled["public_key_id"]
            journal["phase"] = "provider-reconciled"
            private_json(journal_path, journal)
        raise
    key_id = created.get("metadata", {}).get("id") or created.get("id")
    if not isinstance(key_id, str) or not key_id:
        raise HandoffError("Nebius did not return the authentication key ID")
    journal["public_key_id"] = key_id
    journal["phase"] = "create-returned"
    private_json(journal_path, journal)
    requested_expiry = expiry.isoformat().replace("+00:00", "Z")
    try:
        issued = read_auth_key(args, key_id)
        require_binding(
            issued,
            public_key_id=key_id,
            service_account_id=args.service_account_id,
            project_id=args.project_id,
            expires_at=requested_expiry,
        )
        if (
            issued.get("name") != args.name
            or issued.get("lineage_id") != args.lineage_id
            or issued.get("generation") != str(args.generation)
            or issued.get("public_key_sha256") != public_key_sha256
        ):
            raise HandoffError("issued authentication key is outside the exact lineage")
        reconciled = reconcile_issued_key(args, journal)
        if reconciled != issued:
            raise HandoffError(
                "issued authentication key differs from provider inventory"
            )
    except HandoffError as error:
        journal["phase"] = "verification-failed-resource-preserved"
        journal["public_key_id"] = key_id
        journal["verification_error"] = str(error)
        private_json(journal_path, journal)
        raise HandoffError(
            "issued authentication key failed verification; the append-only journal "
            "preserves its exact identity and no cleanup or revocation was attempted"
        ) from error
    receipt = {
        "schema": "fs2-serve.nebius.ai/operator-handoff/v4",
        "service_account_id": args.service_account_id,
        "project_id": args.project_id,
        "cluster_id": args.cluster_id,
        "public_key_id": key_id,
        "issued_at": utc_now().isoformat().replace("+00:00", "Z"),
        "expires_at": requested_expiry,
        "provider_expiry_verified_at": utc_now().isoformat().replace("+00:00", "Z"),
        "lineage": {
            "lineage_id": args.lineage_id,
            "generation": args.generation,
            "predecessor": predecessor_lineage,
            "successor": successor_lineage,
        },
        "public_key_sha256": public_key_sha256,
        "predecessor": predecessor,
        "delivery": None,
        "verification": None,
        "predecessor_retention": {
            "public_key_id": predecessor["public_key_id"],
            "status": "retained-no-irreversible-action",
        },
    }
    private_json(directory / "receipt.json", receipt)
    journal["phase"] = "receipt-committed"
    private_json(journal_path, journal)
    return {
        "status": "issued",
        "public_key_id": key_id,
        "expires_at": receipt["expires_at"],
    }


def issue(args: argparse.Namespace) -> dict[str, Any]:
    raise HandoffError(
        "credential issuance is outside this read-only tool; use the separately "
        "reviewed, externally journaled issuance service"
    )


def reconcile_issue(args: argparse.Namespace) -> dict[str, Any]:
    raise HandoffError(
        "disabled legacy implementation: this read-only executable cannot reconcile issuance"
    )

    directory = private_directory(args.directory)
    journal_path = directory / "issuance.journal.json"
    with handoff_lock(directory):
        try:
            journal = latest_state(
                journal_path.with_name(f"{journal_path.name}.events"),
                stream=f"operator-handoff:{journal_path.name}",
            )
        except EvidenceError as error:
            raise HandoffError("issuance journal is absent or unsafe") from error
        if journal.get("schema") != "fs2-serve.nebius.ai/operator-handoff-issuance/v1":
            raise HandoffError("issuance journal has the wrong schema")
        reconciled = reconcile_issued_key(args, journal)
        if reconciled is None:
            if journal.get("public_key_id") is not None:
                raise HandoffError(
                    "journaled authentication key is absent from provider"
                )
            return {"status": "no-key-created"}
        if journal.get("public_key_id") not in (None, reconciled["public_key_id"]):
            raise HandoffError("journaled key ID differs from provider reconciliation")
        journal["public_key_id"] = reconciled["public_key_id"]
        journal["phase"] = "provider-reconciled"
        private_json(journal_path, journal)
        return {
            "status": "provider-reconciled",
            "public_key_id": reconciled["public_key_id"],
        }


def prove_receipt_lineage(args: argparse.Namespace, receipt: dict[str, Any]) -> None:
    lineage = receipt.get("lineage")
    if receipt.get(
        "schema"
    ) != "fs2-serve.nebius.ai/operator-handoff/v4" or not isinstance(lineage, dict):
        raise HandoffError("provider-derived handoff lineage is required")
    predecessor = lineage.get("predecessor")
    successor = lineage.get("successor")
    if not isinstance(predecessor, dict) or not isinstance(successor, dict):
        raise HandoffError("handoff lineage is incomplete")
    if (
        not isinstance(predecessor.get("generation"), int)
        or not isinstance(successor.get("generation"), int)
        or predecessor.get("lineage_id") != lineage.get("lineage_id")
        or successor.get("lineage_id") != lineage.get("lineage_id")
        or predecessor.get("generation") + 1 != successor.get("generation")
        or successor.get("generation") != lineage.get("generation")
    ):
        raise HandoffError("handoff lineage generations are not adjacent")
    for expected in (predecessor, successor):
        observed = service_account_lineage(
            args,
            service_account_id=expected["service_account_id"],
            group_id=expected["group_id"],
            project_id=expected["project_id"],
            lineage_id=expected["lineage_id"],
            generation=expected["generation"],
            required_role=expected["role"],
        )
        if observed != expected:
            raise HandoffError("live handoff lineage differs from the issuance receipt")


def acknowledge(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    receipt_path, receipt = load_receipt(directory)
    if receipt["delivery"] is not None:
        raise HandoffError("delivery is already acknowledged")
    receipt["delivery"] = {
        "recipient": args.recipient,
        "acknowledged_at": utc_now().isoformat().replace("+00:00", "Z"),
        "public_key_id": receipt["public_key_id"],
        "expires_at": receipt["expires_at"],
    }
    private_json(receipt_path, receipt)
    return {
        "status": "delivery-acknowledged",
        "public_key_id": receipt["public_key_id"],
    }


def verify(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    receipt_path, receipt = load_receipt(directory)
    if receipt["delivery"] is None:
        raise HandoffError("delivery must be acknowledged before verification")
    if timestamp(receipt["expires_at"]) <= utc_now():
        raise HandoffError("handoff credential is expired")
    evidence = authority_json(
        {
            "operation": "viewer-handoff-inventory",
            "key_id": receipt["public_key_id"],
        }
    )
    denials = evidence.get("denials")
    inventory = evidence.get("inventory")
    provider_lineage = evidence.get("provider_lineage")
    if not isinstance(provider_lineage, dict):
        raise HandoffError("provider did not return the exact handoff lineage")
    handoff_id = hashlib.sha256(
        json.dumps(
            provider_lineage, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    successor = receipt.get("lineage", {}).get("successor")
    if not isinstance(successor, dict):
        raise HandoffError("issuance receipt lacks a successor lineage")
    expected_provider_lineage = {
        "project_id": receipt["project_id"],
        "cluster_id": receipt["cluster_id"],
        "lineage_id": successor["lineage_id"],
        "generation": successor["generation"],
        "service_account_id": successor["service_account_id"],
        "group_id": successor["group_id"],
        "role": "viewer",
        "key_id": receipt["public_key_id"],
        "expires_at": receipt["expires_at"],
        "public_key_sha256": receipt.get("public_key_sha256"),
    }
    if any(
        provider_lineage.get(key) != value
        for key, value in expected_provider_lineage.items()
    ):
        raise HandoffError("issuance receipt differs from provider-derived viewer lineage")
    if (
        evidence.get("handoff_id") != handoff_id
        or evidence.get("key_id") != receipt["public_key_id"]
        or not isinstance(denials, dict)
        or denials.get("create_pods") is not True
        or denials.get("get_secrets") is not True
        or denials.get("create_serviceaccount_tokens") is not True
        or denials.get("create_tokenreviews") is not True
        or not all(value is True for value in denials.values())
        or not isinstance(inventory, dict)
        or inventory.get("allowed") is not True
        or not isinstance(evidence.get("allowed_cidrs"), list)
        or not evidence["allowed_cidrs"]
        or not isinstance(evidence.get("externalEvidence"), dict)
    ):
        raise HandoffError("provider did not prove the complete viewer boundary")
    receipt["verification"] = {
        "verified_at": utc_now().isoformat().replace("+00:00", "Z"),
        "inventory_allowed": True,
        "create_pods_denied": True,
        "read_secrets_denied": True,
        "service_account_token_denied": True,
        "token_review_denied": True,
        "negative_probe_count": len(denials),
        "approved_egress_cidrs": evidence["allowed_cidrs"],
        "provider_expiry_verified": True,
        "external_evidence_sha256": hashlib.sha256(
            json.dumps(
                evidence["externalEvidence"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    private_json(receipt_path, receipt)
    return {
        "status": "verified-read-only",
        "public_key_id": receipt["public_key_id"],
        "expires_at": receipt["expires_at"],
    }

    # Legacy direct-provider verification remains unreachable for receipt
    # compatibility only.  All executable verification uses the fixed root
    # authority above and cannot create a profile, kubeconfig, or cloud key.
    prove_receipt_lineage(args, receipt)
    issued = read_auth_key(args, receipt["public_key_id"])
    require_binding(
        issued,
        public_key_id=receipt["public_key_id"],
        service_account_id=receipt["service_account_id"],
        project_id=receipt["project_id"],
        expires_at=receipt["expires_at"],
    )
    approved = host_cidrs(args.approved_egress)
    private_key = directory / "private-key.pem"
    config, kubeconfig = directory / "nebius-config.yaml", directory / "kubeconfig"
    if (
        config.exists()
        or config.is_symlink()
        or kubeconfig.exists()
        or kubeconfig.is_symlink()
    ):
        raise HandoffError(
            "verification refuses to overwrite an existing provider config or kubeconfig"
        )
    profile = "fs2-handoff-viewer"
    run(
        [
            args.nebius,
            "profile",
            "create",
            profile,
            "--config",
            str(config),
            "--service-account-id",
            receipt["service_account_id"],
            "--public-key-id",
            receipt["public_key_id"],
            "--private-key-file-path",
            str(private_key),
            "--parent-id",
            receipt["project_id"],
        ]
    )
    config.chmod(0o600)
    run(
        [
            args.nebius,
            "mk8s",
            "v1",
            "cluster",
            "get-credentials",
            "--config",
            str(config),
            "--profile",
            profile,
            "--id",
            receipt["cluster_id"],
            "--kubeconfig",
            str(kubeconfig),
        ]
    )
    kubeconfig.chmod(0o600)
    run(
        [
            args.kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "get",
            "namespaces",
            "-o",
            "name",
        ],
        capture=True,
    )

    rule_result = run(
        [
            args.kubectl,
            "--kubeconfig",
            str(kubeconfig),
            "auth",
            "can-i",
            "--list",
            "--all-namespaces",
            "--no-headers",
        ],
        capture=True,
    )
    rules = authorization_rules(rule_result.stdout)
    require_viewer_rules(rules)

    denials: dict[str, bool] = {}
    for label, request in negative_authorization_probes().items():
        result = run(
            [args.kubectl, "--kubeconfig", str(kubeconfig), "auth", "can-i", *request],
            capture=True,
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise HandoffError("viewer authorization probe failed")
        denials[label] = (
            result.returncode == 1 and result.stdout.strip().lower() == "no"
        )
    if not all(denials.values()):
        raise HandoffError(
            "viewer handoff has forbidden pod-create or Secret-read access"
        )

    cluster = json.loads(
        run(
            [
                args.nebius,
                "mk8s",
                "v1",
                "cluster",
                "get",
                "--config",
                str(config),
                "--profile",
                profile,
                "--id",
                receipt["cluster_id"],
                "--format",
                "json",
            ],
            capture=True,
        ).stdout
    )
    observed = host_cidrs(
        cluster["spec"]["control_plane"]["endpoints"]["public_endpoint"][
            "allowed_cidrs"
        ]
    )
    if observed != approved:
        raise HandoffError(
            "live control-plane CIDRs differ from the approved egress set"
        )
    receipt["verification"] = {
        "verified_at": utc_now().isoformat().replace("+00:00", "Z"),
        "inventory_allowed": True,
        "create_pods_denied": denials["create_pods"],
        "read_secrets_denied": denials["get_secrets"],
        "authorization_rule_count": len(rules),
        "authorization_rules_sha256": hashlib.sha256(
            json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "negative_probe_count": len(denials),
        "approved_egress_cidrs": approved,
        "provider_expiry_verified": True,
    }
    private_json(receipt_path, receipt)
    return {
        "status": "verified",
        "public_key_id": receipt["public_key_id"],
        "expires_at": receipt["expires_at"],
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    acknowledge_parser = subparsers.add_parser("acknowledge-delivery")
    acknowledge_parser.add_argument("--recipient", required=True)
    subparsers.add_parser("verify")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    args.nebius = FIXED_NEBIUS
    args.kubectl = FIXED_KUBECTL
    args.openssl = FIXED_OPENSSL
    args.admin_profile = FIXED_ADMIN_PROFILE
    handlers = {
        "acknowledge-delivery": acknowledge,
        "verify": verify,
    }
    directory = private_directory(args.directory)
    with handoff_lock(directory):
        result = handlers[args.command](args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HandoffError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error

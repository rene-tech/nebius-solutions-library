#!/usr/bin/env python3
"""Issue, verify, receipt, and revoke a least-privilege operator handoff.

The private key and receipt stay in one owner-only directory. Command output is
limited to non-secret status and resource identifiers; no Secret data is read.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class HandoffError(RuntimeError):
    pass


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
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise HandoffError("handoff directory must be a real directory")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise HandoffError("handoff directory path must not contain a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    if path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise HandoffError("handoff directory must be owner-owned mode 0700")
    return path


def private_json(path: Path, value: Any) -> None:
    if path.exists() and path.is_symlink():
        raise HandoffError("receipt path must not be a symlink")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(
    arguments: Sequence[str],
    *,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
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


def load_receipt(directory: Path) -> tuple[Path, dict[str, Any]]:
    receipt_path = directory / "receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise HandoffError("owner receipt is absent or unsafe")
    if stat.S_IMODE(receipt_path.stat().st_mode) != 0o600:
        raise HandoffError("owner receipt must be mode 0600")
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    if value.get("schema") != "fs2-serve.nebius.ai/operator-handoff/v2":
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
    }
    if not all(
        isinstance(binding[key], str) and binding[key]
        for key in ("public_key_id", "service_account_id", "project_id")
    ):
        raise HandoffError("Nebius authentication key has an incomplete identity")
    if binding["expires_at"] is not None and not isinstance(binding["expires_at"], str):
        raise HandoffError("Nebius authentication key has a malformed expiry")
    return binding


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
        if verbs & mutation_verbs:
            raise HandoffError(
                "viewer authorization inventory contains mutation access"
            )
        if "secret" in resource.lower() and verbs & secret_read_verbs:
            raise HandoffError(
                "viewer authorization inventory contains Secret read access"
            )


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


def issue(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    expiry = timestamp(args.expires_at)
    now = utc_now()
    if expiry <= now or expiry > now + timedelta(days=366):
        raise HandoffError(
            "handoff expiry must be future and no more than one year away"
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
        path.exists() for path in (private_key, public_key, directory / "receipt.json")
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

    request = {
        "metadata": {"parent_id": args.project_id, "name": args.name},
        "spec": {
            "account": {"service_account": {"id": args.service_account_id}},
            "data": public_key.read_text(encoding="ascii"),
            "description": "Expiring viewer-only Kubernetes operator handoff",
            "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        },
    }
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, encoding="utf-8", delete=False
    ) as stream:
        json.dump(request, stream)
        request_path = Path(stream.name)
    request_path.chmod(0o600)
    try:
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
    finally:
        request_path.unlink(missing_ok=True)
    key_id = created.get("metadata", {}).get("id") or created.get("id")
    if not isinstance(key_id, str) or not key_id:
        raise HandoffError("Nebius did not return the authentication key ID")
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
    except HandoffError as error:
        try:
            run(
                [
                    args.nebius,
                    "iam",
                    "auth-public-key",
                    "delete",
                    "--profile",
                    args.admin_profile,
                    "--id",
                    key_id,
                ]
            )
            if key_id in read_auth_key_ids(args, args.project_id):
                raise HandoffError(
                    "unverified authentication key remains present after cleanup"
                )
            private_key.unlink(missing_ok=True)
            public_key.unlink(missing_ok=True)
        except HandoffError as cleanup_error:
            raise HandoffError(
                "issued authentication key did not prove its expiry and cleanup failed"
            ) from cleanup_error
        raise HandoffError(
            "issued authentication key did not prove provider-enforced expiry; it was revoked"
        ) from error
    receipt = {
        "schema": "fs2-serve.nebius.ai/operator-handoff/v2",
        "service_account_id": args.service_account_id,
        "project_id": args.project_id,
        "cluster_id": args.cluster_id,
        "public_key_id": key_id,
        "issued_at": utc_now().isoformat().replace("+00:00", "Z"),
        "expires_at": requested_expiry,
        "provider_expiry_verified_at": utc_now().isoformat().replace("+00:00", "Z"),
        "predecessor": predecessor,
        "delivery": None,
        "verification": None,
        "revocation_attempt": None,
        "revoked_old_key": None,
    }
    private_json(directory / "receipt.json", receipt)
    return {
        "status": "issued",
        "public_key_id": key_id,
        "expires_at": receipt["expires_at"],
    }


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
    for label, request in {
        "create_pods": ["create", "pods", "--all-namespaces"],
        "update_pods": ["update", "pods", "--all-namespaces"],
        "patch_deployments": ["patch", "deployments.apps", "--all-namespaces"],
        "delete_pods": ["delete", "pods", "--all-namespaces"],
        "create_rolebindings": [
            "create",
            "rolebindings.rbac.authorization.k8s.io",
            "--all-namespaces",
        ],
        "create_clusterrolebindings": [
            "create",
            "clusterrolebindings.rbac.authorization.k8s.io",
        ],
        "read_secrets": ["get", "secrets", "--all-namespaces"],
        "list_secrets": ["list", "secrets", "--all-namespaces"],
        "watch_secrets": ["watch", "secrets", "--all-namespaces"],
    }.items():
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
        "create_pods_denied": True,
        "read_secrets_denied": True,
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


def revoke_old(args: argparse.Namespace) -> dict[str, Any]:
    directory = private_directory(args.directory)
    receipt_path, receipt = load_receipt(directory)
    if receipt["verification"] is None or receipt["delivery"] is None:
        raise HandoffError(
            "verified delivery is required before revoking the old handoff"
        )
    predecessor = receipt.get("predecessor")
    if not isinstance(predecessor, dict):
        raise HandoffError("receipt has no bound predecessor identity")
    old_public_key_id = predecessor.get("public_key_id")
    old_service_account_id = predecessor.get("service_account_id")
    old_project_id = predecessor.get("project_id")
    if not all(
        isinstance(value, str) and value
        for value in (old_public_key_id, old_service_account_id, old_project_id)
    ):
        raise HandoffError("receipt predecessor identity is incomplete")
    if args.confirm_predecessor_public_key_id != old_public_key_id:
        raise HandoffError("revocation confirmation differs from the bound predecessor")
    if old_public_key_id == receipt["public_key_id"]:
        raise HandoffError("refusing to revoke the newly verified handoff key")
    revoked = receipt.get("revoked_old_key")
    if isinstance(revoked, dict) and revoked.get("public_key_id") == old_public_key_id:
        raise HandoffError("bound predecessor was already revoked")

    attempt = receipt.get("revocation_attempt")
    expected_predecessor = {
        "public_key_id": old_public_key_id,
        "service_account_id": old_service_account_id,
        "project_id": old_project_id,
    }
    if attempt is None:
        observed = read_auth_key(args, old_public_key_id)
        require_binding(observed, **expected_predecessor)
        receipt["revocation_attempt"] = {
            **expected_predecessor,
            "initiated_at": utc_now().isoformat().replace("+00:00", "Z"),
        }
        private_json(receipt_path, receipt)
        predecessor_present = True
    elif isinstance(attempt, dict) and all(
        attempt.get(key) == value for key, value in expected_predecessor.items()
    ):
        predecessor_present = old_public_key_id in read_auth_key_ids(
            args, old_project_id
        )
        if predecessor_present:
            observed = read_auth_key(args, old_public_key_id)
            require_binding(observed, **expected_predecessor)
    else:
        raise HandoffError("revocation attempt does not match the bound predecessor")

    if predecessor_present:
        run(
            [
                args.nebius,
                "iam",
                "auth-public-key",
                "delete",
                "--profile",
                args.admin_profile,
                "--id",
                old_public_key_id,
            ]
        )
    absence_get = run(
        [
            args.nebius,
            "iam",
            "auth-public-key",
            "get",
            "--profile",
            args.admin_profile,
            "--id",
            old_public_key_id,
            "--format",
            "json",
        ],
        capture=True,
        check=False,
    )
    if absence_get.returncode == 0:
        raise HandoffError("revoked predecessor is still returned by authoritative get")
    remaining_ids = read_auth_key_ids(args, old_project_id)
    if old_public_key_id in remaining_ids:
        raise HandoffError("revoked predecessor remains present in provider inventory")
    if (
        old_project_id == receipt["project_id"]
        and receipt["public_key_id"] not in remaining_ids
    ):
        raise HandoffError(
            "provider inventory does not contain the verified successor key"
        )
    receipt["revoked_old_key"] = {
        "public_key_id": old_public_key_id,
        "service_account_id": old_service_account_id,
        "project_id": old_project_id,
        "revoked_at": utc_now().isoformat().replace("+00:00", "Z"),
        "post_delete_absence_verified": True,
        "authoritative_get_absent": True,
    }
    receipt["revocation_attempt"] = None
    private_json(receipt_path, receipt)
    return {"status": "old-key-revoked", "public_key_id": old_public_key_id}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--nebius", default="nebius")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--openssl", default="openssl")
    parser.add_argument("--admin-profile", default="sandbox")
    subparsers = parser.add_subparsers(dest="command", required=True)
    issue_parser = subparsers.add_parser("issue")
    issue_parser.add_argument("--service-account-id", required=True)
    issue_parser.add_argument("--project-id", required=True)
    issue_parser.add_argument("--cluster-id", required=True)
    issue_parser.add_argument("--expires-at", required=True)
    issue_parser.add_argument("--predecessor-public-key-id", required=True)
    issue_parser.add_argument("--predecessor-service-account-id", required=True)
    issue_parser.add_argument("--predecessor-project-id", required=True)
    issue_parser.add_argument("--name", default="fs2-operator-handoff")
    acknowledge_parser = subparsers.add_parser("acknowledge-delivery")
    acknowledge_parser.add_argument("--recipient", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--approved-egress", action="append", required=True)
    revoke_parser = subparsers.add_parser("revoke-old")
    revoke_parser.add_argument("--confirm-predecessor-public-key-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    args = parse_args(argv)
    handlers = {
        "issue": issue,
        "acknowledge-delivery": acknowledge,
        "verify": verify,
        "revoke-old": revoke_old,
    }
    print(json.dumps(handlers[args.command](args), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (HandoffError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        raise SystemExit(2) from error

#!/usr/bin/env python3
"""Request release signatures without materializing a long-lived signing key.

The protected GitHub environment supplies only an OIDC identity and a broker
URL. The broker authenticates the short-lived token, enforces the identity
contract embedded in every subject, and returns raw detached signatures made
by the independently reviewed key in image-attestation-trust.json.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __name__ == "__main__" or __package__ != "security":
    print(
        "OIDC broker access is an import-only external-capsule payload",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .execution_toolchain import (  # noqa: E402
    ToolchainError,
    environment_toolchain,
    validate_current_python,
    validate_source_file,
)


def _capsule_source_root() -> Path:
    value = os.environ.get("FS2_CAPSULE_SOURCE_ROOT", "")
    if not value and __name__ != "__main__":
        return Path(__file__).resolve().parent.parent
    if not value or not Path(value).is_absolute():
        raise BrokerError("capsule read-only source root is absent")
    return Path(value).resolve()


DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BrokerError(ValueError):
    """Raised when the protected signing contract is incomplete."""


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise BrokerError(f"required environment variable is absent: {name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trust_policy(trust_path: Path) -> dict[str, Any]:
    trust = json.loads(trust_path.read_text(encoding="utf-8"))
    if not isinstance(trust, dict):
        raise BrokerError("OIDC attestation trust root must be an object")
    policy = trust.get("oidc_release_attestation")
    if trust.get("state") != "trusted" or not isinstance(policy, dict):
        raise BrokerError("OIDC attestation trust is not active")
    return policy


def identity(
    audience: str, environment: str, trust_path: Path
) -> dict[str, str]:
    repository = _required_environment("GITHUB_REPOSITORY")
    result = {
        "issuer": "https://token.actions.githubusercontent.com",
        "audience": audience,
        "subject": f"repo:{repository}:environment:{environment}",
        "repository": repository,
        "workflow_ref": _required_environment("GITHUB_WORKFLOW_REF"),
        "environment": environment,
        "run_id": _required_environment("GITHUB_RUN_ID"),
        "run_attempt": _required_environment("GITHUB_RUN_ATTEMPT"),
    }
    policy = _trust_policy(trust_path)
    expected = {
        "issuer": result["issuer"],
        "audience": audience,
        "repository": repository,
        "protected_environment": environment,
    }
    if any(policy.get(field) != value for field, value in expected.items()):
        raise BrokerError("runner identity differs from attestation trust policy")
    allowed = policy.get("allowed_workflow_refs")
    if not isinstance(allowed, list) or result["workflow_ref"] not in allowed:
        raise BrokerError("workflow ref is not authorized to request signatures")
    return result


def _json_response(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = json.loads(response.read())
    except (OSError, ValueError) as exc:
        raise BrokerError("attestation broker request failed") from exc
    if not isinstance(document, dict):
        raise BrokerError("attestation broker returned a non-object response")
    return document


def _oidc_token(audience: str) -> str:
    request_url = _required_environment("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = _required_environment("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    separator = "&" if "?" in request_url else "?"
    request = urllib.request.Request(
        f"{request_url}{separator}{urllib.parse.urlencode({'audience': audience})}",
        headers={"Authorization": f"Bearer {request_token}"},
    )
    response = _json_response(request)
    token = response.get("value")
    if not isinstance(token, str) or not token:
        raise BrokerError("GitHub OIDC response did not contain a token")
    return token


def sign(
    subjects: list[Path],
    broker_url: str,
    audience: str,
    identity_path: Path,
    trust_path: Path,
) -> None:
    policy = _trust_policy(trust_path)
    if policy.get("signing_broker_url") != broker_url:
        raise BrokerError("signing broker URL differs from attestation trust policy")
    expected_identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if expected_identity != identity(
        audience, expected_identity.get("environment", ""), trust_path
    ):
        raise BrokerError("attestation identity file differs from the runner identity")
    subject_rows = [
        {"name": subject.name, "sha256": _sha256(subject)} for subject in subjects
    ]
    payload = json.dumps(
        {
            "schema": "fs2-serve.nebius.ai/oidc-signing-request/v1",
            "identity": expected_identity,
            "subjects": subject_rows,
            "oidc_token": _oidc_token(audience),
            "signature_contract": "raw-sha256-detached",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        broker_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    response = _json_response(request)
    if response.get("schema") != "fs2-serve.nebius.ai/oidc-signing-response/v1":
        raise BrokerError("attestation broker response schema is unsupported")
    if response.get("identity") != expected_identity:
        raise BrokerError("attestation broker signed for a different identity")
    signatures = response.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != len(subjects):
        raise BrokerError("attestation broker returned an incomplete signature set")
    by_digest = {
        row.get("subject_sha256"): row for row in signatures if isinstance(row, dict)
    }
    for subject in subjects:
        digest = _sha256(subject)
        row = by_digest.get(digest)
        encoded = row.get("signature_base64") if isinstance(row, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise BrokerError(f"attestation broker omitted signature for {subject.name}")
        try:
            signature = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise BrokerError("attestation broker returned malformed base64") from exc
        Path(f"{subject}.sig").write_bytes(signature)


def registry_auth(
    subjects: list[str],
    broker_url: str,
    audience: str,
    identity_path: Path,
    trust_path: Path,
    output: Path,
) -> None:
    """Exchange protected OIDC for exact repository/digest pull-only credentials."""

    trust = json.loads(trust_path.read_text(encoding="utf-8"))
    policy = trust.get("registry_authentication") if isinstance(trust, dict) else None
    if not isinstance(policy, dict) or policy.get("broker_url") != broker_url:
        raise BrokerError("registry authentication broker is not trusted")
    if policy.get("audience") != audience:
        raise BrokerError("registry authentication audience is not trusted")
    allowed = policy.get("allowed_registries")
    requested = sorted(set(subjects))
    parsed: list[dict[str, Any]] = []
    for subject in requested:
        if not DIGEST_REFERENCE.fullmatch(subject):
            raise BrokerError("registry authentication subject is not digest-bound")
        repository, digest = subject.rsplit("@", 1)
        registry, separator, repository_path = repository.partition("/")
        if not separator or not repository_path:
            raise BrokerError("registry authentication repository is invalid")
        parsed.append(
            {
                "subject": subject,
                "registry": registry,
                "repository": repository_path,
                "digest": digest,
                "actions": ["pull"],
            }
        )
    if (
        not requested
        or not isinstance(allowed, list)
        or any(item["registry"] not in allowed for item in parsed)
    ):
        raise BrokerError("registry authentication scope is not authorized")
    expected_identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if expected_identity != identity(
        audience, expected_identity.get("environment", ""), trust_path
    ):
        raise BrokerError("registry authentication identity differs from the runner")
    payload = json.dumps(
        {
            "schema": "fs2-serve.nebius.ai/oidc-registry-auth-request/v1",
            "identity": expected_identity,
            "subjects": parsed,
            "oidc_token": _oidc_token(audience),
            "credential_format": "docker-config-json",
            "required_actions": ["pull"],
        }
    ).encode("utf-8")
    response = _json_response(
        urllib.request.Request(
            broker_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )
    if response.get("schema") != "fs2-serve.nebius.ai/oidc-registry-auth-response/v1":
        raise BrokerError("registry authentication response schema is unsupported")
    authorized_brokers = policy.get("authorized_broker_ids")
    if (
        response.get("identity") != expected_identity
        or response.get("authorized_subjects") != parsed
        or response.get("required_actions") != ["pull"]
        or policy.get("required_authorization_model")
        != "repository-digest-action"
        or response.get("authorization_model")
        != "repository-digest-action"
        or not isinstance(authorized_brokers, list)
        or response.get("broker_id") not in authorized_brokers
        or response.get("digest_scope_enforced") is not True
    ):
        raise BrokerError("registry authentication response scope differs")
    expires_value = response.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires_value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrokerError("registry authentication expiry is invalid") from exc
    now = datetime.now(timezone.utc)
    maximum_ttl = policy.get("maximum_ttl_seconds")
    if (
        expires_at.tzinfo is None
        or not isinstance(maximum_ttl, int)
        or maximum_ttl < 60
        or not now < expires_at.astimezone(timezone.utc) <= now + timedelta(seconds=maximum_ttl)
    ):
        raise BrokerError("registry authentication lifetime is not bounded")
    encoded = response.get("docker_config_base64")
    expected_sha256 = response.get("docker_config_sha256")
    if not isinstance(encoded, str) or not isinstance(expected_sha256, str):
        raise BrokerError("registry authentication payload is missing")
    try:
        docker_config = base64.b64decode(encoded, validate=True)
        document = json.loads(docker_config)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BrokerError("registry authentication payload is malformed") from exc
    if hashlib.sha256(docker_config).hexdigest() != expected_sha256:
        raise BrokerError("registry authentication payload hash differs")
    auths = document.get("auths") if isinstance(document, dict) else None
    expected_registries = sorted({item["registry"] for item in parsed})
    if not isinstance(auths, dict) or sorted(auths) != expected_registries:
        raise BrokerError("registry authentication Docker config scope differs")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(docker_config)
    output.chmod(0o600)


def workload_registry_auth(
    subjects: list[str],
    identity_path: Path,
    token_path: Path,
    trust_path: Path,
    docker_config_output: Path,
    receipt_output: Path,
) -> None:
    """Exchange a projected Kubernetes identity for exact pull-only auth."""

    trust = json.loads(trust_path.read_text(encoding="utf-8"))
    policy = trust.get("workload_registry_authentication") if isinstance(trust, dict) else None
    if not isinstance(policy, dict) or not isinstance(policy.get("broker_url"), str):
        raise BrokerError("workload registry authentication broker is not trusted")
    contract_relative = policy.get("refresh_controller_contract_path")
    contract_sha256 = policy.get("refresh_controller_contract_sha256")
    source_root = _capsule_source_root()
    if (
        not isinstance(contract_relative, str)
        or Path(contract_relative).is_absolute()
        or not isinstance(contract_sha256, str)
    ):
        raise BrokerError("refresh-controller trust is incomplete")
    refresh_contract_path = (source_root / "security" / contract_relative).resolve()
    refresh_contract = json.loads(refresh_contract_path.read_text(encoding="utf-8"))
    refresh_runtime = (
        refresh_contract.get("runtime") if isinstance(refresh_contract, dict) else None
    )
    admission_relative = policy.get("secret_admission_contract_path")
    admission_sha256 = policy.get("secret_admission_contract_sha256")
    if (
        not isinstance(admission_relative, str)
        or Path(admission_relative).is_absolute()
        or not isinstance(admission_sha256, str)
    ):
        raise BrokerError("Secret admission trust is incomplete")
    admission_contract_path = (
        source_root / "security" / admission_relative
    ).resolve()
    admission_contract = json.loads(
        admission_contract_path.read_text(encoding="utf-8")
    )
    admission_runtime = (
        admission_contract.get("runtime")
        if isinstance(admission_contract, dict)
        else None
    )
    admission_handoff = (
        admission_contract.get("authoritative_handoff")
        if isinstance(admission_contract, dict)
        else None
    )
    if (
        _sha256(refresh_contract_path) != contract_sha256
        or refresh_contract.get("schema")
        != "fs2-serve.nebius.ai/workload-registry-refresh-contract/v2"
        or refresh_contract.get("state") != "trusted"
        or not isinstance(refresh_runtime, dict)
        or refresh_runtime.get("owner_id")
        not in policy.get("authorized_refresh_owner_ids", [])
        or _sha256(admission_contract_path) != admission_sha256
        or admission_contract.get("schema")
        != "fs2-serve.nebius.ai/workload-registry-secret-admission-contract/v3"
        or admission_contract.get("state") != "trusted"
        or admission_contract.get("provider_proxy_mutates_planned_metadata") is not False
        or admission_contract.get("provider_proxy_mutates_data_wo_revision") is not False
        or admission_contract.get("provider_proxy_mutates_only_write_only_secret_data") is not True
        or not isinstance(refresh_contract.get("watched_secret_inventory"), dict)
        or refresh_contract["watched_secret_inventory"].get("selection")
        != "exact-enabled-subset-only"
        or refresh_contract["watched_secret_inventory"].get(
            "readiness_receipt_must_bind_inventory_sha256"
        )
        is not True
        or refresh_contract.get("readiness_receipt_schema")
        != "fs2-serve.nebius.ai/workload-registry-refresh-readiness/v2"
        or not isinstance(admission_contract.get("enabled_secret_inventory"), dict)
        or admission_contract["enabled_secret_inventory"].get(
            "static_superset_allowed"
        )
        is not False
        or not isinstance(admission_handoff, dict)
        or admission_handoff.get("schema")
        != "fs2-serve.nebius.ai/workload-registry-secret-admission-handoff/v2"
        or admission_handoff.get("terraform_apply_success_requires_verified_handoff")
        is not True
        or not isinstance(admission_runtime, dict)
        or admission_runtime.get("proxy_id")
        not in policy.get("authorized_secret_admission_proxy_ids", [])
        or admission_runtime.get("handoff_signer_id")
        not in policy.get("authorized_secret_admission_handoff_signer_ids", [])
        or not all(
            isinstance(admission_runtime.get(field), str)
            and HEX_SHA256.fullmatch(admission_runtime[field])
            for field in ("handoff_public_key_sha256", "handoff_verifier_sha256")
        )
    ):
        raise BrokerError(
            "refresh-controller and Secret-admission runtimes are not trusted"
        )
    identity_document = json.loads(identity_path.read_text(encoding="utf-8"))
    if not isinstance(identity_document, dict):
        raise BrokerError("workload identity document must be an object")
    service_account = identity_document.get("service_account")
    expected_identity = {
        "issuer": policy.get("issuer"),
        "audience": policy.get("audience"),
        "subject": f"system:serviceaccount:{service_account}",
        "service_account": service_account,
    }
    allowed_accounts = policy.get("authorized_service_accounts")
    if (
        identity_document != expected_identity
        or not isinstance(allowed_accounts, list)
        or service_account not in allowed_accounts
    ):
        raise BrokerError("projected workload identity is not authorized")
    if token_path.is_symlink() or not token_path.is_file():
        raise BrokerError("projected workload token must be one regular file")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise BrokerError("projected workload token is empty")
    parsed: list[dict[str, Any]] = []
    allowed_registries = policy.get("allowed_registries")
    for subject in sorted(set(subjects)):
        if not DIGEST_REFERENCE.fullmatch(subject):
            raise BrokerError("workload registry subject is not digest-bound")
        repository, digest = subject.rsplit("@", 1)
        registry, separator, repository_path = repository.partition("/")
        if (
            not separator
            or not isinstance(allowed_registries, list)
            or registry not in allowed_registries
        ):
            raise BrokerError("workload registry subject is not authorized")
        parsed.append(
            {
                "subject": subject,
                "registry": registry,
                "repository": repository_path,
                "digest": digest,
                "actions": ["pull"],
            }
        )
    if not parsed:
        raise BrokerError("at least one workload registry subject is required")
    response = _json_response(
        urllib.request.Request(
            policy["broker_url"],
            data=json.dumps(
                {
                    "schema": "fs2-serve.nebius.ai/workload-registry-auth-request/v1",
                    "identity": expected_identity,
                    "subjects": parsed,
                    "oidc_token": token,
                    "credential_format": "docker-config-json",
                    "required_actions": ["pull"],
                    "secret_admission": {
                        "proxy_id": admission_runtime["proxy_id"],
                        "contract_sha256": admission_sha256,
                        "provider_rpc_mode": (
                            "broker-immediately-before-secret-create-or-update"
                        ),
                        "planning_credential_forwarded_to_apply": False,
                        "credential_reuse_across_resource_rpcs": False,
                        "mutate_write_only_secret_data_only": True,
                        "preserve_planned_metadata_and_data_wo_revision": True,
                        "token_receipt_destination": (
                            "external-signed-admission-handoff"
                        ),
                    },
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )
    if response.get("schema") != "fs2-serve.nebius.ai/workload-registry-auth-response/v1":
        raise BrokerError("workload registry broker response schema is unsupported")
    receipt = response.get("authorization_receipt")
    signature_value = response.get("authorization_receipt_signature_base64")
    encoded = response.get("docker_config_base64")
    if not isinstance(receipt, dict) or not isinstance(signature_value, str):
        raise BrokerError("workload registry broker omitted its signed receipt")
    if (
        receipt.get("schema")
        != "fs2-serve.nebius.ai/workload-registry-auth-receipt/v1"
        or receipt.get("identity") != expected_identity
        or receipt.get("authorized_subjects") != parsed
        or receipt.get("authorization_model") != "repository-digest-action"
        or receipt.get("broker_id") not in policy.get("authorized_broker_ids", [])
        or not isinstance(receipt.get("refresh"), dict)
        or receipt["refresh"].get("owner_id")
        not in policy.get("authorized_refresh_owner_ids", [])
        or receipt["refresh"].get("owner_id") != refresh_runtime.get("owner_id")
        or receipt["refresh"].get("interval_seconds")
        != policy.get("maximum_refresh_interval_seconds")
        or receipt["refresh"].get("management_mode")
        != "external-short-lived-refresh-controller"
        or not isinstance(receipt.get("revision"), int)
        or isinstance(receipt.get("revision"), bool)
        or receipt.get("revision") < 1
        or not isinstance(receipt["refresh"].get("rotate_before_expiry_seconds"), int)
        or receipt["refresh"].get("rotate_before_expiry_seconds") < 60
        or receipt["refresh"].get("rotate_before_expiry_seconds")
        >= policy.get("maximum_ttl_seconds", 0)
        or receipt["refresh"].get("retire_superseded_without_delete") is not True
        or receipt.get("secret_admission")
        != {
            "proxy_id": admission_runtime["proxy_id"],
            "contract_sha256": admission_sha256,
            "provider_rpc_mode": "broker-immediately-before-secret-create-or-update",
            "planning_credential_forwarded_to_apply": False,
            "credential_reuse_across_resource_rpcs": False,
            "mutate_write_only_secret_data_only": True,
            "preserve_planned_metadata_and_data_wo_revision": True,
            "token_receipt_destination": "external-signed-admission-handoff",
        }
    ):
        raise BrokerError("workload registry receipt scope differs")
    try:
        expires_at = datetime.fromisoformat(
            str(receipt.get("expires_at")).replace("Z", "+00:00")
        )
        issued_at = datetime.fromisoformat(
            str(receipt.get("issued_at")).replace("Z", "+00:00")
        )
        docker_config = base64.b64decode(str(encoded), validate=True)
        signature = base64.b64decode(signature_value, validate=True)
        config_document = json.loads(docker_config)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BrokerError("workload registry broker payload is malformed") from exc
    maximum_ttl = policy.get("maximum_ttl_seconds")
    now = datetime.now(timezone.utc)
    if (
        expires_at.tzinfo is None
        or issued_at.tzinfo is None
        or not isinstance(maximum_ttl, int)
        or not now < expires_at.astimezone(timezone.utc)
        or expires_at - issued_at > timedelta(seconds=maximum_ttl)
    ):
        raise BrokerError("workload registry credential lifetime is not bounded")
    if receipt.get("docker_config_sha256") != hashlib.sha256(docker_config).hexdigest():
        raise BrokerError("workload registry Docker config hash differs")
    auths = config_document.get("auths") if isinstance(config_document, dict) else None
    if not isinstance(auths, dict) or sorted(auths) != sorted(
        {row["registry"] for row in parsed}
    ):
        raise BrokerError("workload registry Docker config host scope differs")
    docker_config_output.parent.mkdir(parents=True, exist_ok=True)
    docker_config_output.write_bytes(docker_config)
    docker_config_output.chmod(0o600)
    receipt_output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    receipt_output.chmod(0o600)
    Path(f"{receipt_output}.sig").write_bytes(signature)
    Path(f"{receipt_output}.sig").chmod(0o600)


def main() -> int:
    if os.environ.get("FS2_EXTERNAL_CAPSULE_ACTIVE") != "1":
        print("OIDC broker client requires the external capsule", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    identity_command = commands.add_parser("identity")
    identity_command.add_argument("--audience", required=True)
    identity_command.add_argument("--environment", required=True)
    identity_command.add_argument("--trust", required=True, type=Path)
    identity_command.add_argument("--output", required=True, type=Path)
    sign_command = commands.add_parser("sign")
    sign_command.add_argument("--broker-url", required=True)
    sign_command.add_argument("--audience", required=True)
    sign_command.add_argument("--identity", required=True, type=Path)
    sign_command.add_argument("--trust", required=True, type=Path)
    sign_command.add_argument("subjects", nargs="+", type=Path)
    registry_command = commands.add_parser("registry-auth")
    registry_command.add_argument("--broker-url", required=True)
    registry_command.add_argument("--audience", required=True)
    registry_command.add_argument("--identity", required=True, type=Path)
    registry_command.add_argument("--trust", required=True, type=Path)
    registry_command.add_argument("--subject", action="append", required=True)
    registry_command.add_argument("--output", required=True, type=Path)
    workload_command = commands.add_parser("workload-registry-auth")
    workload_command.add_argument("--identity", required=True, type=Path)
    workload_command.add_argument("--token-file", required=True, type=Path)
    workload_command.add_argument("--trust", required=True, type=Path)
    workload_command.add_argument("--subject", action="append", required=True)
    workload_command.add_argument("--docker-config-output", required=True, type=Path)
    workload_command.add_argument("--receipt-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        lock_path, environment_trust = environment_toolchain()
        if environment_trust.resolve() != args.trust.resolve():
            raise BrokerError("broker execution trust differs from requested trust")
        validate_current_python(lock_path=lock_path, trust_path=args.trust.resolve())
        validate_source_file(
            "security/oidc_attestation_broker.py",
            source_root=_capsule_source_root(),
            lock_path=lock_path,
            trust_path=args.trust.resolve(),
        )
        if args.command == "identity":
            args.output.write_text(
                json.dumps(
                    identity(args.audience, args.environment, args.trust),
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        elif args.command == "sign":
            sign(
                args.subjects,
                args.broker_url,
                args.audience,
                args.identity,
                args.trust,
            )
        elif args.command == "registry-auth":
            registry_auth(
                args.subject,
                args.broker_url,
                args.audience,
                args.identity,
                args.trust,
                args.output,
            )
        else:
            workload_registry_auth(
                args.subject,
                args.identity,
                args.token_file,
                args.trust,
                args.docker_config_output,
                args.receipt_output,
            )
    except (BrokerError, OSError, ToolchainError, json.JSONDecodeError) as exc:
        print(f"OIDC attestation broker: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

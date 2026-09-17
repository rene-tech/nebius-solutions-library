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


DIGEST_REFERENCE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


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


def main() -> int:
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
    args = parser.parse_args()
    try:
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
        else:
            registry_auth(
                args.subject,
                args.broker_url,
                args.audience,
                args.identity,
                args.trust,
                args.output,
            )
    except (BrokerError, OSError, json.JSONDecodeError) as exc:
        print(f"OIDC attestation broker: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Sign and verify the exact Nebius API DNS-to-host-route egress contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import importlib.metadata
import ipaddress
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SCHEMA = "fs2-serve.nebius.ai/customer-storage-egress-contract/v1"
PROVIDER = "nebius"
SDK_PACKAGE = "nebius"
SDK_VERSION = "0.6.10"
SDK_SERVICES = (
    (
        "nebius.api.nebius.storage.v1",
        "BucketServiceClient",
        "nebius.storage.v1.BucketService",
        "cpl.storage",
    ),
    (
        "nebius.api.nebius.iam.v1",
        "GroupServiceClient",
        "nebius.iam.v1.GroupService",
        "cpl.iam",
    ),
    (
        "nebius.api.nebius.iam.v1",
        "GroupMembershipServiceClient",
        "nebius.iam.v1.GroupMembershipService",
        "cpl.iam",
    ),
    (
        "nebius.api.nebius.iam.v1",
        "ServiceAccountServiceClient",
        "nebius.iam.v1.ServiceAccountService",
        "cpl.iam",
    ),
    (
        "nebius.api.nebius.iam.v2",
        "AccessKeyServiceClient",
        "nebius.iam.v2.AccessKeyService",
        "cpl.iam",
    ),
    (
        "nebius.api.nebius.iam.v1",
        "TokenExchangeServiceClient",
        "nebius.iam.v1.TokenExchangeService",
        "tokens.iam",
    ),
)
ENDPOINTS = tuple(sorted({f"{item[3]}.api.nebius.cloud" for item in SDK_SERVICES}))
MAX_CIDRS = 32
MAX_VALIDITY = timedelta(hours=24)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("contract timestamps must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("contract timestamps must include a timezone")
    return parsed.astimezone(UTC)


def provider_sdk_services() -> list[dict[str, str]]:
    """Resolve the endpoint authority from the exact pinned generated SDK."""

    if importlib.metadata.version(SDK_PACKAGE) != SDK_VERSION:
        raise ValueError("the pinned Nebius SDK version is unavailable")
    services: list[dict[str, str]] = []
    for module_name, class_name, service_name, api_service_name in SDK_SERVICES:
        client = getattr(importlib.import_module(module_name), class_name)
        if (
            client.__service_name__ != service_name
            or client.__api_service_name__ != api_service_name
        ):
            raise ValueError("the Nebius SDK service route metadata differs")
        services.append(
            {
                "service": client.__service_name__,
                "api_service_name": client.__api_service_name__,
                "endpoint": f"{client.__api_service_name__}.api.nebius.cloud",
            }
        )
    return services


def expected_sdk_services() -> list[dict[str, str]]:
    return [
        {
            "service": service_name,
            "api_service_name": api_service_name,
            "endpoint": f"{api_service_name}.api.nebius.cloud",
        }
        for _, _, service_name, api_service_name in SDK_SERVICES
    ]


def resolve_endpoints(
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, list[str]]:
    resolved: dict[str, list[str]] = {}
    for endpoint in ENDPOINTS:
        addresses = {
            item[4][0]
            for item in resolver(endpoint, 443, type=socket.SOCK_STREAM)
            if item[0] in {socket.AF_INET, socket.AF_INET6}
        }
        if not addresses:
            raise ValueError(f"provider endpoint did not resolve: {endpoint}")
        resolved[endpoint] = sorted(
            addresses, key=lambda item: (ipaddress.ip_address(item).version, item)
        )
    return resolved


def _host_cidrs(resolutions: dict[str, list[str]]) -> list[str]:
    cidrs = {
        str(
            ipaddress.ip_network(
                f"{address}/{32 if ipaddress.ip_address(address).version == 4 else 128}"
            )
        )
        for addresses in resolutions.values()
        for address in addresses
    }
    if not cidrs or len(cidrs) > MAX_CIDRS:
        raise ValueError("provider endpoint resolution count is empty or unbounded")
    return sorted(cidrs, key=lambda item: (ipaddress.ip_network(item).version, item))


def create_contract(
    private_key: Ed25519PrivateKey,
    *,
    now: datetime | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, Any]:
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    body: dict[str, Any] = {
        "schema": SCHEMA,
        "provider": PROVIDER,
        "sdk_package": SDK_PACKAGE,
        "sdk_version": SDK_VERSION,
        "sdk_services": provider_sdk_services(),
        "endpoints": list(ENDPOINTS),
        "resolutions": resolve_endpoints(resolver),
        "cidrs": [],
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "valid_until": (observed + MAX_VALIDITY).isoformat().replace("+00:00", "Z"),
    }
    body["cidrs"] = _host_cidrs(body["resolutions"])
    payload = _canonical(body)
    body["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    body["signature"] = base64.b64encode(private_key.sign(payload)).decode()
    return body


def verify_contract(
    contract: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    now: datetime | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> dict[str, str]:
    required = {
        "schema",
        "provider",
        "sdk_package",
        "sdk_version",
        "sdk_services",
        "endpoints",
        "resolutions",
        "cidrs",
        "observed_at",
        "valid_until",
        "payload_sha256",
        "signature",
    }
    if set(contract) != required:
        raise ValueError("egress contract fields are missing or unexpected")
    if (
        contract["schema"] != SCHEMA
        or contract["provider"] != PROVIDER
        or contract["sdk_package"] != SDK_PACKAGE
        or contract["sdk_version"] != SDK_VERSION
        or contract["sdk_services"] != expected_sdk_services()
        or contract["endpoints"] != list(ENDPOINTS)
    ):
        raise ValueError("egress contract provider or SDK identity differs")
    if not isinstance(contract["resolutions"], dict) or set(
        contract["resolutions"]
    ) != set(ENDPOINTS):
        raise ValueError("egress contract endpoint set differs")
    expected_cidrs = _host_cidrs(contract["resolutions"])
    if contract["cidrs"] != expected_cidrs:
        raise ValueError("egress CIDRs are not exact provider host routes")
    observed = _timestamp(contract["observed_at"])
    valid_until = _timestamp(contract["valid_until"])
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    if observed > checked_at + timedelta(minutes=5) or valid_until <= checked_at:
        raise ValueError("egress contract is not currently valid")
    if valid_until - observed > MAX_VALIDITY or valid_until <= observed:
        raise ValueError("egress contract lifetime exceeds its bound")
    body = {
        key: value
        for key, value in contract.items()
        if key not in {"payload_sha256", "signature"}
    }
    payload = _canonical(body)
    digest = hashlib.sha256(payload).hexdigest()
    if contract["payload_sha256"] != digest:
        raise ValueError("egress contract payload digest differs")
    try:
        signature = base64.b64decode(contract["signature"], validate=True)
        public_key.verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("egress contract signature is invalid") from exc
    live = resolve_endpoints(resolver)
    if live != contract["resolutions"] or _host_cidrs(live) != contract["cidrs"]:
        raise ValueError(
            "live Nebius endpoint resolution differs from the signed contract"
        )
    return {
        "cidrs_json": json.dumps(contract["cidrs"], separators=(",", ":")),
        "endpoints_json": json.dumps(contract["endpoints"], separators=(",", ":")),
        "contract_sha256": hashlib.sha256(_canonical(contract)).hexdigest(),
        "valid_until": contract["valid_until"],
    }


def verify_network_policy(policy: dict[str, Any], expected_cidrs: list[str]) -> None:
    rules = policy.get("spec", {}).get("egress", [])
    if not rules or any(not rule.get("to") for rule in rules):
        raise ValueError(
            "every live customer-storage egress rule must have explicit destinations"
        )
    https_rules = []
    for rule in rules:
        ports = rule.get("ports", [])
        if any(port.get("port") == 443 for port in ports):
            https_rules.append(rule)
    if len(https_rules) != 1:
        raise ValueError("live NetworkPolicy does not equal the signed egress CIDR set")
    rule = https_rules[0]
    if rule.get("ports") != [{"port": 443, "protocol": "TCP"}]:
        raise ValueError("customer storage egress must be exact TCP/443")
    peers = rule.get("to", [])
    if any(
        set(peer) != {"ipBlock"} or set(peer["ipBlock"]) != {"cidr"} for peer in peers
    ):
        raise ValueError("customer storage HTTPS peers must be exact IP blocks")
    cidrs = [peer["ipBlock"]["cidr"] for peer in peers]
    if sorted(cidrs) != sorted(expected_cidrs) or len(cidrs) != len(set(cidrs)):
        raise ValueError("live NetworkPolicy does not equal the signed egress CIDR set")


def _private_key(path: Path) -> Ed25519PrivateKey:
    value = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(value, Ed25519PrivateKey):
        raise ValueError("contract signing key must be Ed25519")
    return value


def _public_key(value: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(value.encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("contract verification key must be Ed25519")
    return key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--network-policy", type=Path)
    parser.add_argument("--terraform-external", action="store_true")
    args = parser.parse_args()
    try:
        if args.create:
            if args.private_key is None:
                raise ValueError("--private-key is required with --create")
            print(
                json.dumps(
                    create_contract(_private_key(args.private_key)),
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        if args.terraform_external:
            query = json.load(sys.stdin)
            contract = json.loads(query["contract_json"])
            result = verify_contract(contract, _public_key(query["public_key_pem"]))
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.contract is None or args.public_key is None:
            raise ValueError("--contract and --public-key are required")
        contract = json.loads(args.contract.read_text(encoding="utf-8"))
        result = verify_contract(
            contract, _public_key(args.public_key.read_text(encoding="utf-8"))
        )
        if args.network_policy is not None:
            verify_network_policy(
                json.loads(args.network_policy.read_text(encoding="utf-8")),
                contract["cidrs"],
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"customer storage egress contract rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

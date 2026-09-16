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
import os
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
MAX_FILE_BYTES = 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def safe_read(path: Path, *, maximum: int = MAX_FILE_BYTES) -> bytes:
    """Read one immutable regular file through descriptor-relative nofollow IO."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("contract input path is invalid")
    directory_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > maximum:
                raise ValueError("contract input must be a bounded non-empty regular file")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(file_fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(file_fd)
            if (
                len(payload) > maximum
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(payload) != before.st_size
            ):
                raise ValueError("contract input changed during its descriptor-bound read")
            return payload
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _json_bytes(payload: bytes, label: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains a duplicate field")
            value[key] = item
        return value

    try:
        return json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc


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
        if client.__service_name__ != service_name or client.__api_service_name__ != api_service_name:
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
        resolved[endpoint] = sorted(addresses, key=lambda item: (ipaddress.ip_address(item).version, item))
    return resolved


def _host_cidrs(resolutions: dict[str, list[str]]) -> list[str]:
    cidrs = {
        str(ipaddress.ip_network(f"{address}/{32 if ipaddress.ip_address(address).version == 4 else 128}"))
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
    if not isinstance(contract["resolutions"], dict) or set(contract["resolutions"]) != set(ENDPOINTS):
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
    body = {key: value for key, value in contract.items() if key not in {"payload_sha256", "signature"}}
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
        raise ValueError("live Nebius endpoint resolution differs from the signed contract")
    return {
        "cidrs_json": json.dumps(contract["cidrs"], separators=(",", ":")),
        "endpoints_json": json.dumps(contract["endpoints"], separators=(",", ":")),
        "contract_sha256": hashlib.sha256(_canonical(contract)).hexdigest(),
        "valid_until": contract["valid_until"],
    }


def verify_network_policy(
    policy: dict[str, Any],
    expected_cidrs: list[str],
    expected_kubernetes_api_cidrs: list[str] | None = None,
) -> None:
    rules = policy.get("spec", {}).get("egress", [])
    if not rules or any(not rule.get("to") for rule in rules):
        raise ValueError("every live customer-storage egress rule must have explicit destinations")
    https_rules = []
    for rule in rules:
        ports = rule.get("ports", [])
        if any(port.get("port") == 443 for port in ports):
            https_rules.append(rule)
    expected_sets = [sorted(expected_cidrs)]
    if expected_kubernetes_api_cidrs is not None:
        expected_sets.append(sorted(expected_kubernetes_api_cidrs))
    if len(https_rules) != len(expected_sets):
        raise ValueError("live NetworkPolicy does not equal the signed egress CIDR set")
    observed_sets: list[list[str]] = []
    for rule in https_rules:
        if rule.get("ports") != [{"port": 443, "protocol": "TCP"}]:
            raise ValueError("customer storage egress must be exact TCP/443")
        peers = rule.get("to", [])
        if any(set(peer) != {"ipBlock"} or set(peer["ipBlock"]) != {"cidr"} for peer in peers):
            raise ValueError("customer storage HTTPS peers must be exact IP blocks")
        cidrs = [peer["ipBlock"]["cidr"] for peer in peers]
        if len(cidrs) != len(set(cidrs)):
            raise ValueError("live NetworkPolicy contains duplicate HTTPS destinations")
        observed_sets.append(sorted(cidrs))
    if sorted(observed_sets) != sorted(expected_sets):
        raise ValueError("live NetworkPolicy does not equal the signed egress CIDR set")
    if expected_kubernetes_api_cidrs is None:
        return
    expected_non_https = [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/instance": "coredns",
                            "app.kubernetes.io/name": "coredns",
                            "k8s-app": "coredns",
                        }
                    },
                }
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "fs2-data"}
                    },
                    "podSelector": {"matchLabels": {"cnpg.io/cluster": "fs2-control-db"}},
                }
            ],
            "ports": [{"port": 5432, "protocol": "TCP"}],
        },
    ]
    non_https = [rule for rule in rules if rule not in https_rules]
    if non_https != expected_non_https or len(rules) != 4:
        raise ValueError("live NetworkPolicy DNS/database rules do not equal the canonical policy")


def _selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """Evaluate the NetworkPolicy LabelSelector subset used by this boundary."""

    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise ValueError("NetworkPolicy pod selector contains unsupported fields")
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        raise ValueError("NetworkPolicy pod selector is malformed")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in match_labels.items()):
        raise ValueError("NetworkPolicy matchLabels must contain strings")
    if any(labels.get(key) != value for key, value in match_labels.items()):
        return False
    for expression in expressions:
        if not isinstance(expression, dict) or set(expression) - {"key", "operator", "values"}:
            raise ValueError("NetworkPolicy matchExpression is malformed")
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(key, str) or not isinstance(operator, str) or not isinstance(values, list):
            raise ValueError("NetworkPolicy matchExpression fields are invalid")
        if any(not isinstance(value, str) for value in values):
            raise ValueError("NetworkPolicy matchExpression values must be strings")
        present = key in labels
        if operator == "In":
            matches = present and labels[key] in values and bool(values)
        elif operator == "NotIn":
            matches = present and labels[key] not in values and bool(values)
        elif operator == "Exists":
            matches = present and not values
        elif operator == "DoesNotExist":
            matches = not present and not values
        else:
            raise ValueError("NetworkPolicy matchExpression operator is unsupported")
        if not matches:
            return False
    return True


def _canonical_egress_rules(
    expected_cidrs: list[str], expected_kubernetes_api_cidrs: list[str]
) -> list[dict[str, Any]]:
    return [
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                    },
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/instance": "coredns",
                            "app.kubernetes.io/name": "coredns",
                            "k8s-app": "coredns",
                        }
                    },
                }
            ],
            "ports": [
                {"port": 53, "protocol": "UDP"},
                {"port": 53, "protocol": "TCP"},
            ],
        },
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "fs2-data"}
                    },
                    "podSelector": {"matchLabels": {"cnpg.io/cluster": "fs2-control-db"}},
                }
            ],
            "ports": [{"port": 5432, "protocol": "TCP"}],
        },
        {
            "to": [{"ipBlock": {"cidr": cidr}} for cidr in expected_cidrs],
            "ports": [{"port": 443, "protocol": "TCP"}],
        },
        {
            "to": [{"ipBlock": {"cidr": cidr}} for cidr in expected_kubernetes_api_cidrs],
            "ports": [{"port": 443, "protocol": "TCP"}],
        },
    ]


def verify_effective_network_policies(
    policies: list[dict[str, Any]],
    pod_labels: dict[str, str],
    expected_cidrs: list[str],
    expected_kubernetes_api_cidrs: list[str],
) -> None:
    """Prove the effective egress union of every policy selecting one pod.

    Kubernetes unions the allowed egress of all selecting policies. Checking a
    single canonical object therefore misses a retained or independently
    managed policy that widens the same pod. Every observed rule must be a
    member of the canonical set and the effective union must equal that set.
    """

    if not policies or not pod_labels:
        raise ValueError("effective NetworkPolicy verification needs policies and pod labels")
    canonical = _canonical_egress_rules(expected_cidrs, expected_kubernetes_api_cidrs)
    canonical_by_digest = {_canonical(rule): rule for rule in canonical}
    effective: dict[bytes, dict[str, Any]] = {}
    selected = 0
    exact_selector = 0
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError("NetworkPolicy list contains a non-object")
        spec = policy.get("spec", {})
        selector = spec.get("podSelector")
        if not isinstance(selector, dict):
            raise ValueError("NetworkPolicy is missing a pod selector")
        if not _selector_matches(selector, pod_labels):
            continue
        selected += 1
        if selector == {"matchLabels": pod_labels}:
            exact_selector += 1
        policy_types = spec.get("policyTypes", [])
        rules = spec.get("egress", [])
        if not isinstance(policy_types, list) or not isinstance(rules, list):
            raise ValueError("selecting NetworkPolicy has malformed policyTypes or egress")
        isolates_egress = "Egress" in policy_types or bool(rules)
        if not isolates_egress:
            continue
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("to"):
                raise ValueError("every selecting egress rule must have explicit destinations")
            digest = _canonical(rule)
            if digest not in canonical_by_digest:
                raise ValueError("a selecting NetworkPolicy widens the canonical egress set")
            effective[digest] = rule
    if selected == 0 or exact_selector != 1:
        raise ValueError("exactly one generation policy must select the reconciler labels exactly")
    if set(effective) != set(canonical_by_digest):
        raise ValueError("effective selecting NetworkPolicy union differs from the canonical egress set")


def _private_key(path: Path) -> Ed25519PrivateKey:
    value = serialization.load_pem_private_key(safe_read(path, maximum=16 * 1024), password=None)
    if not isinstance(value, Ed25519PrivateKey):
        raise ValueError("contract signing key must be Ed25519")
    return value


def _public_key(value: bytes | str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(value.encode() if isinstance(value, str) else value)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("contract verification key must be Ed25519")
    return key


def _cluster_network_request(path: str, label: str) -> dict[str, Any]:
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    if host is None:
        raise ValueError("in-cluster Kubernetes API identity is unavailable")
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text(encoding="ascii")
    ca = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt").read_text(encoding="ascii")
    context = ssl.create_default_context(cadata=ca)
    url = f"https://{host}:{port}{path}"
    request = urllib.request.Request(  # noqa: S310 - exact in-cluster HTTPS authority
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=10) as response:  # noqa: S310
            payload = response.read(MAX_FILE_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError(f"live {label} could not be read") from exc
    if len(payload) > MAX_FILE_BYTES:
        raise ValueError(f"live {label} response exceeds its bound")
    value = _json_bytes(payload, f"live {label}")
    if not isinstance(value, dict):
        raise ValueError(f"live {label} must be an object")
    return value


def _cluster_network_policy(namespace: str, name: str) -> dict[str, Any]:
    return _cluster_network_request(
        f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies/{name}",
        "NetworkPolicy",
    )


def _cluster_network_policies(namespace: str) -> list[dict[str, Any]]:
    value = _cluster_network_request(
        f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies",
        "NetworkPolicyList",
    )
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("live NetworkPolicyList items are invalid")
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--private-key", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--network-policy", type=Path)
    parser.add_argument("--expected-cidrs", type=Path)
    parser.add_argument("--expected-kubernetes-api-cidrs", type=Path)
    parser.add_argument("--kubernetes-network-policy", nargs=2, metavar=("NAMESPACE", "NAME"))
    parser.add_argument("--kubernetes-network-policy-set", metavar="NAMESPACE")
    parser.add_argument("--pod-label", action="append", default=[])
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
            if "network_policies_json" in query:
                policies = json.loads(query["network_policies_json"])
                pod_labels = json.loads(query["pod_labels_json"])
                kubernetes_api_cidrs = json.loads(query["kubernetes_api_cidrs_json"])
                if not isinstance(policies, list) or not isinstance(pod_labels, dict):
                    raise ValueError("Terraform effective-policy inputs are malformed")
                verify_effective_network_policies(
                    policies,
                    pod_labels,
                    contract["cidrs"],
                    kubernetes_api_cidrs,
                )
                result["effective_policy_verified"] = "true"
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.contract is None or args.public_key is None:
            raise ValueError("--contract and --public-key are required")
        contract_payload = safe_read(args.contract)
        contract = _json_bytes(contract_payload, "egress contract")
        if not isinstance(contract, dict):
            raise ValueError("egress contract must be an object")
        result = verify_contract(contract, _public_key(safe_read(args.public_key, maximum=16 * 1024)))
        if args.expected_cidrs is not None:
            expected = _json_bytes(safe_read(args.expected_cidrs, maximum=64 * 1024), "expected CIDRs")
            if expected != contract["cidrs"]:
                raise ValueError("rendered egress CIDRs do not equal the signed contract")
        expected_kubernetes_api_cidrs = None
        if args.expected_kubernetes_api_cidrs is not None:
            expected_kubernetes_api_cidrs = _json_bytes(
                safe_read(args.expected_kubernetes_api_cidrs, maximum=64 * 1024),
                "expected Kubernetes API CIDRs",
            )
            if not isinstance(expected_kubernetes_api_cidrs, list) or not expected_kubernetes_api_cidrs:
                raise ValueError("expected Kubernetes API CIDRs must be a non-empty list")
        if args.network_policy is not None:
            verify_network_policy(
                _json_bytes(safe_read(args.network_policy), "live NetworkPolicy"),
                contract["cidrs"],
                expected_kubernetes_api_cidrs,
            )
        if args.kubernetes_network_policy is not None:
            verify_network_policy(
                _cluster_network_policy(*args.kubernetes_network_policy),
                contract["cidrs"],
                expected_kubernetes_api_cidrs,
            )
        if args.kubernetes_network_policy_set is not None:
            if expected_kubernetes_api_cidrs is None:
                raise ValueError("effective policy verification requires Kubernetes API CIDRs")
            pod_labels: dict[str, str] = {}
            for item in args.pod_label:
                if "=" not in item:
                    raise ValueError("--pod-label requires key=value")
                key, value = item.split("=", 1)
                if not key or not value or key in pod_labels:
                    raise ValueError("--pod-label entries must be unique non-empty key=value pairs")
                pod_labels[key] = value
            verify_effective_network_policies(
                _cluster_network_policies(args.kubernetes_network_policy_set),
                pod_labels,
                contract["cidrs"],
                expected_kubernetes_api_cidrs,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"customer storage egress contract rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

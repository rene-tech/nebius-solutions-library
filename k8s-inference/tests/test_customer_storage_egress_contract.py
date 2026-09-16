"""Fail-closed tests for the signed Nebius customer-storage egress contract."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = (
    Path(__file__).parents[1]
    / "stages/workloads/scripts/customer_storage_egress_contract.py"
)
SPEC = importlib.util.spec_from_file_location(
    "customer_storage_egress_contract", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
contract_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract_module)

NOW = datetime(2026, 9, 16, 18, tzinfo=UTC)


def resolver(host: str, port: int, *, type: int):
    assert port == 443 and type == socket.SOCK_STREAM
    addresses = {
        "cpl.iam.api.nebius.cloud": "198.51.100.10",
        "cpl.storage.api.nebius.cloud": "198.51.100.11",
        "tokens.iam.api.nebius.cloud": "2001:db8::12",
    }
    address = addresses[host]
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]


def resign(value: dict, private_key: Ed25519PrivateKey) -> dict:
    body = {
        key: item
        for key, item in value.items()
        if key not in {"payload_sha256", "signature"}
    }
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    value["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    value["signature"] = base64.b64encode(private_key.sign(payload)).decode()
    return value


@pytest.fixture
def signed_contract():
    private_key = Ed25519PrivateKey.generate()
    contract = contract_module.create_contract(private_key, now=NOW, resolver=resolver)
    return private_key, contract


def test_signed_contract_requires_live_provider_equality_and_host_routes(
    signed_contract,
):
    private_key, contract = signed_contract
    result = contract_module.verify_contract(
        contract, private_key.public_key(), now=NOW, resolver=resolver
    )
    assert json.loads(result["cidrs_json"]) == [
        "198.51.100.10/32",
        "198.51.100.11/32",
        "2001:db8::12/128",
    ]
    assert len(result["contract_sha256"]) == 64
    assert contract["sdk_services"] == contract_module.expected_sdk_services()
    assert {item["endpoint"] for item in contract["sdk_services"]} == set(
        contract["endpoints"]
    )


def test_endpoint_authority_matches_pinned_generated_sdk():
    assert (
        contract_module.provider_sdk_services()
        == contract_module.expected_sdk_services()
    )


@pytest.mark.parametrize(
    "cidrs",
    [
        ["0.0.0.0/1", "128.0.0.0/1"],
        ["2000::/3"],
        ["203.0.113.44/32"],
        [],
    ],
)
def test_rejects_aggregate_broad_arbitrary_and_empty_sets(signed_contract, cidrs):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    adversarial["cidrs"] = cidrs
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="host routes|empty"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_rejects_missing_set_even_with_valid_signature(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    del adversarial["cidrs"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="missing"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_rejects_signed_non_provider_resolution(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    adversarial["resolutions"]["cpl.iam.api.nebius.cloud"] = ["203.0.113.44"]
    adversarial["cidrs"] = ["198.51.100.11/32", "203.0.113.44/32", "2001:db8::12/128"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="live Nebius"):
        contract_module.verify_contract(
            adversarial, private_key.public_key(), now=NOW, resolver=resolver
        )


def test_live_network_policy_must_equal_contract_with_explicit_to_and_tcp_443(
    signed_contract,
):
    _, contract = signed_contract
    policy = {
        "spec": {
            "egress": [
                {
                    "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                }
            ]
        }
    }
    contract_module.verify_network_policy(policy, contract["cidrs"])
    policy["spec"]["egress"][0]["ports"] = [{"port": 443}]
    with pytest.raises(ValueError, match="TCP/443"):
        contract_module.verify_network_policy(policy, contract["cidrs"])


def test_live_network_policy_rejects_extra_https_rule_or_non_ip_peer(signed_contract):
    _, contract = signed_contract
    exact = {
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    policy = {"spec": {"egress": [copy.deepcopy(exact)]}}
    policy["spec"]["egress"][0]["to"].append(
        {"namespaceSelector": {"matchLabels": {"unreviewed": "true"}}}
    )
    with pytest.raises(ValueError, match="exact IP blocks"):
        contract_module.verify_network_policy(policy, contract["cidrs"])

    policy = {
        "spec": {
            "egress": [
                exact,
                {
                    "to": [{"ipBlock": {"cidr": "203.0.113.1/32"}}],
                    "ports": [{"port": 443, "protocol": "TCP"}],
                },
            ]
        }
    }
    with pytest.raises(ValueError, match="does not equal"):
        contract_module.verify_network_policy(policy, contract["cidrs"])

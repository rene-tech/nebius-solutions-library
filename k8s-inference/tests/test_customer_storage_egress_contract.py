"""Fail-closed tests for the signed Nebius customer-storage egress contract."""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).parents[1] / "stages/workloads/scripts/customer_storage_egress_contract.py"
TERRAFORM = Path(__file__).parents[1] / "stages/workloads/customer_storage.tf"
SPEC = importlib.util.spec_from_file_location("customer_storage_egress_contract", SCRIPT)
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
    body = {key: item for key, item in value.items() if key not in {"payload_sha256", "signature"}}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
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
    result = contract_module.verify_contract(contract, private_key.public_key(), now=NOW, resolver=resolver)
    assert json.loads(result["cidrs_json"]) == [
        "198.51.100.10/32",
        "198.51.100.11/32",
        "2001:db8::12/128",
    ]
    assert len(result["contract_sha256"]) == 64
    assert contract["sdk_services"] == contract_module.expected_sdk_services()
    assert {item["endpoint"] for item in contract["sdk_services"]} == set(contract["endpoints"])


def test_endpoint_authority_matches_pinned_generated_sdk():
    assert contract_module.provider_sdk_services() == contract_module.expected_sdk_services()


def test_terraform_uses_an_external_immutable_trust_root() -> None:
    source = TERRAFORM.read_text(encoding="utf-8")
    assert 'data "kubernetes_secret_v1" "customer_storage_egress_trust"' in source
    assert 'resource "kubernetes_secret_v1" "customer_storage_egress_trust"' not in source
    assert "self.immutable == true" in source
    assert 'data.kubernetes_secret_v1.customer_storage_egress_trust[0].data["public-key.pem"]' in source
    assert "egress_contract_public_key_pem" not in source


def test_terraform_owns_a_fail_closed_admission_boundary_outside_helm() -> None:
    source = TERRAFORM.read_text(encoding="utf-8")
    control_plane = (TERRAFORM.parent / "control_plane.tf").read_text(encoding="utf-8")
    assert 'resource "kubernetes_manifest" "customer_storage_egress_admission_policy"' in source
    assert 'resource "kubernetes_manifest" "customer_storage_egress_admission_binding"' in source
    assert 'failurePolicy = "Fail"' in source
    assert 'operations  = ["CREATE", "UPDATE", "DELETE"]' in source
    assert 'expression = "request.operation != \'DELETE\'"' in source
    assert "object.spec == ${jsonencode(local.customer_storage_network_policy_spec)}" in source
    assert "data.external.customer_storage_egress[0].result.contract_sha256" in source
    assert "customer_storage_egress_admission_binding" in control_plane


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
        contract_module.verify_contract(adversarial, private_key.public_key(), now=NOW, resolver=resolver)


def test_rejects_missing_set_even_with_valid_signature(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    del adversarial["cidrs"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="missing"):
        contract_module.verify_contract(adversarial, private_key.public_key(), now=NOW, resolver=resolver)


def test_rejects_signed_non_provider_resolution(signed_contract):
    private_key, contract = signed_contract
    adversarial = copy.deepcopy(contract)
    adversarial["resolutions"]["cpl.iam.api.nebius.cloud"] = ["203.0.113.44"]
    adversarial["cidrs"] = ["198.51.100.11/32", "203.0.113.44/32", "2001:db8::12/128"]
    resign(adversarial, private_key)
    with pytest.raises(ValueError, match="live Nebius"):
        contract_module.verify_contract(adversarial, private_key.public_key(), now=NOW, resolver=resolver)


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
    policy["spec"]["egress"][0]["to"].append({"namespaceSelector": {"matchLabels": {"unreviewed": "true"}}})
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


def test_readiness_requires_exact_dns_database_provider_and_api_rules(signed_contract):
    _, contract = signed_contract
    provider_rule = {
        "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    api_rule = {
        "to": [{"ipBlock": {"cidr": "192.0.2.1/32"}}],
        "ports": [{"port": 443, "protocol": "TCP"}],
    }
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/instance": "coredns",
                        "app.kubernetes.io/name": "coredns",
                        "k8s-app": "coredns",
                    }
                },
            }
        ],
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
    }
    database_rule = {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "fs2-data"}},
                "podSelector": {"matchLabels": {"cnpg.io/cluster": "fs2-control-db"}},
            }
        ],
        "ports": [{"port": 5432, "protocol": "TCP"}],
    }
    policy = {"spec": {"egress": [dns_rule, database_rule, provider_rule, api_rule]}}
    contract_module.verify_network_policy(policy, contract["cidrs"], ["192.0.2.1/32"])

    for mutation in ("destinationless", "dns-selector", "extra-rule"):
        adversarial = copy.deepcopy(policy)
        if mutation == "destinationless":
            del adversarial["spec"]["egress"][2]["to"]
        elif mutation == "dns-selector":
            adversarial["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"]["k8s-app"] = "kube-dns"
        else:
            adversarial["spec"]["egress"].append(copy.deepcopy(provider_rule))
        with pytest.raises(ValueError):
            contract_module.verify_network_policy(adversarial, contract["cidrs"], ["192.0.2.1/32"])


def cli_inputs(tmp_path: Path, *, observed_at: datetime | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    contract = contract_module.create_contract(
        private_key,
        now=observed_at or datetime.now(UTC),
        resolver=resolver,
    )
    contract_path = tmp_path / "contract.json"
    public_key_path = tmp_path / "public-key.pem"
    cidrs_path = tmp_path / "cidrs.json"
    policy_path = tmp_path / "network-policy.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    cidrs_path.write_text(json.dumps(contract["cidrs"]), encoding="utf-8")
    policy_path.write_text(
        json.dumps(
            {
                "spec": {
                    "egress": [
                        {
                            "to": [{"ipBlock": {"cidr": cidr}} for cidr in contract["cidrs"]],
                            "ports": [{"port": 443, "protocol": "TCP"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return private_key, contract, contract_path, public_key_path, cidrs_path, policy_path


def run_verify_cli(monkeypatch, contract: Path, public_key: Path, cidrs: Path, policy: Path | None = None) -> int:
    argv = [
        "customer-storage-egress-contract",
        "--contract",
        str(contract),
        "--public-key",
        str(public_key),
        "--expected-cidrs",
        str(cidrs),
    ]
    if policy is not None:
        argv.extend(("--network-policy", str(policy)))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(contract_module.socket, "getaddrinfo", resolver)
    return contract_module.main()


def test_direct_rollout_cli_rejects_expired_and_forged_contracts(tmp_path, monkeypatch):
    _, _, contract_path, public_key_path, cidrs_path, _ = cli_inputs(
        tmp_path,
        observed_at=datetime.now(UTC) - timedelta(days=2),
    )
    assert run_verify_cli(monkeypatch, contract_path, public_key_path, cidrs_path) == 2

    private_key, contract, contract_path, public_key_path, cidrs_path, _ = cli_inputs(tmp_path / "forged")
    del private_key
    contract["signature"] = base64.b64encode(b"0" * 64).decode()
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    assert run_verify_cli(monkeypatch, contract_path, public_key_path, cidrs_path) == 2


@pytest.mark.parametrize("linked_input", ["contract", "public_key", "cidrs", "policy"])
def test_direct_rollout_cli_rejects_symlinked_inputs(tmp_path, monkeypatch, linked_input):
    _, _, contract_path, public_key_path, cidrs_path, policy_path = cli_inputs(tmp_path)
    paths = {
        "contract": contract_path,
        "public_key": public_key_path,
        "cidrs": cidrs_path,
        "policy": policy_path,
    }
    target = paths[linked_input]
    real = target.with_suffix(target.suffix + ".real")
    target.rename(real)
    target.symlink_to(real)
    assert run_verify_cli(monkeypatch, contract_path, public_key_path, cidrs_path, policy_path) == 2


def test_direct_rollout_cli_rejects_reassigned_provider_address(tmp_path, monkeypatch):
    _, _, contract_path, public_key_path, cidrs_path, _ = cli_inputs(tmp_path)

    def reassigned(host: str, port: int, *, type: int):
        values = resolver(host, port, type=type)
        if host == "cpl.iam.api.nebius.cloud":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.99", 443))]
        return values

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "customer-storage-egress-contract",
            "--contract",
            str(contract_path),
            "--public-key",
            str(public_key_path),
            "--expected-cidrs",
            str(cidrs_path),
        ],
    )
    monkeypatch.setattr(contract_module.socket, "getaddrinfo", reassigned)
    assert contract_module.main() == 2

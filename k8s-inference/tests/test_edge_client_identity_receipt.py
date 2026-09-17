from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    ROOT / "stages/workloads/scripts/verify-edge-client-identity-receipt.py"
)
SPEC = importlib.util.spec_from_file_location("edge_client_identity_receipt", ADAPTER_PATH)
assert SPEC is not None and SPEC.loader is not None
EDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EDGE)

PUBLIC_KEY = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
KEY_ID = "sha256:" + hashlib.sha256(PUBLIC_KEY).hexdigest()
ISSUER_ID = "platform-security-edge-authority"
LB_ID = "loadbalancer-e00abc123"
LISTENER_REVISION = "revision-20260917-123456"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def expected_subject() -> dict[str, object]:
    return {
        "schema": EDGE.SUBJECT_SCHEMA,
        "provider": "nebius",
        "project_id": "project-e00abc123",
        "cluster_id": "mk8scluster-e00abc123",
        "allocation": {
            "id": "vpcallocation-e00abc123",
            "ipv4_address": "203.0.113.17",
        },
        "network": {
            "id": "vpcnetwork-e00abc123",
            "subnet_id": "vpcsubnet-e00abc123",
        },
        "security_group": {
            "id": "vpcsecuritygroup-e00abc123",
            "ingress_rule_id": "vpcsecurityrule-e00abc123",
            "source_cidrs": ["0.0.0.0/0"],
            "destination_ports": [80, 443, 10080, 10443, 31425, 32633],
        },
        "gateway": {
            "namespace": "fs2-system",
            "name": "public",
            "class_name": "fs2-serve-public",
            "listeners": [
                {"name": "acme-http", "protocol": "HTTP", "port": 80},
                {"name": "public-https", "protocol": "HTTPS", "port": 443},
            ],
        },
        "service_ports": {
            "http": {"listener_port": 80, "target_port": 10080, "node_port": 31425},
            "https": {"listener_port": 443, "target_port": 10443, "node_port": 32633},
        },
    }


def trust_store() -> dict[str, object]:
    return {
        "schema": EDGE.TRUST_SCHEMA,
        "issuers": [
            {
                "id": ISSUER_ID,
                "role": EDGE.ISSUER_ROLE,
                "key_id": KEY_ID,
                "public_key": b64url(PUBLIC_KEY),
            }
        ],
    }


def native_evidence_documents() -> dict[str, object]:
    documents: dict[str, object] = {
        name: {
            "schema": "fs2-serve.nebius.ai/native-edge-supporting-export/v1",
            "kind": name,
        }
        for name in EDGE.EVIDENCE_FILES
    }
    listeners = {
        "schema": "fs2-serve.nebius.ai/native-provider-listener-export/v1",
        "provider": "nebius",
        "collected_at": "2026-09-17T12:05:00Z",
        "provenance": {
            "collector_id": "platform-security-native-exporter",
            "endpoint": "api.nebius.cloud",
            "operation": "loadbalancer.listener.list",
            "request_id": "request-20260917-abcdef",
            "response_attestation_sha256": "a" * 64,
        },
        "request": {
            "load_balancer_id": LB_ID,
            "page_size": 100,
            "page_token": "",
        },
        "response": {
            "listeners": [
                {
                    "id": "loadbalancerlistener-http123",
                    "load_balancer_id": LB_ID,
                    "port": 80,
                    "protocol": "HTTP",
                    "source_connection_limit": {
                        "enabled": True,
                        "key": "SOURCE_IP",
                        "maximum": 64,
                        "overflow_action": "REJECT_SOURCE_ONLY",
                        "applies_before_backend": True,
                        "protocols": [
                            "HTTP1",
                            "HTTP2",
                            "TCP_INCOMPLETE_HANDSHAKE",
                        ],
                    },
                },
                {
                    "id": "loadbalancerlistener-https123",
                    "load_balancer_id": LB_ID,
                    "port": 443,
                    "protocol": "HTTPS",
                    "source_connection_limit": {
                        "enabled": True,
                        "key": "SOURCE_IP",
                        "maximum": 64,
                        "overflow_action": "REJECT_SOURCE_ONLY",
                        "applies_before_backend": True,
                        "protocols": [
                            "HTTP1",
                            "HTTP2",
                            "TCP_INCOMPLETE_HANDSHAKE",
                        ],
                    },
                },
            ],
            "next_page_token": "",
            "remaining_item_count": 0,
            "revision": LISTENER_REVISION,
        },
    }
    listener_raw = EDGE._canonical(listeners) + b"\n"
    documents["provider_listener_export_sha256"] = listeners
    documents["connection_isolation_probe_sha256"] = {
        "schema": "fs2-serve.nebius.ai/native-connection-isolation-probe/v1",
        "collected_at": "2026-09-17T12:10:00Z",
        "load_balancer_id": LB_ID,
        "listener_export_sha256": hashlib.sha256(listener_raw).hexdigest(),
        "listener_revision": LISTENER_REVISION,
        "protocols": ["HTTP1", "HTTP2", "TCP_INCOMPLETE_HANDSHAKE"],
        "saturating_source": {
            "admitted": 64,
            "attempted": 65,
            "backend_connections": 64,
            "observation_id": "b" * 64,
            "rejected": 1,
        },
        "independent_source": {
            "admitted": 1,
            "attempted": 1,
            "backend_connections": 1,
            "observation_id": "c" * 64,
            "rejected": 0,
        },
    }
    return documents


def native_reopened_evidence() -> dict[str, object]:
    documents = native_evidence_documents()
    digests = {
        name: hashlib.sha256(EDGE._canonical(document) + b"\n").hexdigest()
        for name, document in documents.items()
    }
    return {"digests": digests, "documents": documents}


def receipt() -> dict[str, object]:
    subject = expected_subject()
    payload = {
        "schema": EDGE.PAYLOAD_SCHEMA,
        "issuer": {
            "id": ISSUER_ID,
            "role": EDGE.ISSUER_ROLE,
            "key_id": KEY_ID,
        },
        "nonce": "1" * 64,
        "issued_at": "2026-09-17T12:00:00Z",
        "expires_at": "2026-09-17T13:00:00Z",
        "subject": subject,
        "provider_topology": {
            "load_balancer": {
                "id": LB_ID,
                "allocation_id": subject["allocation"]["id"],
                "public_ipv4_address": subject["allocation"]["ipv4_address"],
            },
            "listeners": [
                {
                    "id": "loadbalancerlistener-http123",
                    "gateway_listener_name": "acme-http",
                    "protocol": "HTTP",
                    "port": 80,
                },
                {
                    "id": "loadbalancerlistener-https123",
                    "gateway_listener_name": "public-https",
                    "protocol": "HTTPS",
                    "port": 443,
                },
            ],
            "backend": {
                "id": "loadbalancerbackend-e00abc123",
                "service_namespace": "envoy-gateway-system",
                "service_name": "envoy-fs2-system-public-12345678",
                "service_uid": "12345678-1234-1234-1234-123456789abc",
                "service_ports": subject["service_ports"],
            },
            "security_group": {
                "id": subject["security_group"]["id"],
                "ingress_rule_id": subject["security_group"]["ingress_rule_id"],
            },
            "routing": {
                "network_id": subject["network"]["id"],
                "subnet_id": subject["network"]["subnet_id"],
                "route_table_ids": ["vpcroute-e00abc123"],
            },
        },
        "observations": {
            "xff": {
                "header_action": "append",
                "appends_downstream_remote_address": True,
                "untrusted_prefix_ignored": True,
                "proxy_chain": [
                    {
                        "kind": "nebius-load-balancer",
                        "id": LB_ID,
                        "position_from_envoy": 1,
                    }
                ],
            },
            "direct_access": {
                "verdict": "excluded",
                "enforcement": "security-group-and-provider-routing",
                "security_group_id": subject["security_group"]["id"],
                "ingress_rule_id": subject["security_group"]["ingress_rule_id"],
                "public_entrypoint_ids": [LB_ID],
                "worker_public_ipv4_addresses": [],
                "service_cluster_ip_publicly_routable": False,
                "node_ports_publicly_routable": False,
                "target_ports_publicly_routable": False,
            },
            "connection_admission": {
                "enforcement_point": "provider-listener-before-envoy",
                "http1_and_http2_connections_covered": True,
                "listeners": [
                    {"id": "loadbalancerlistener-http123", "port": 80},
                    {"id": "loadbalancerlistener-https123", "port": 443},
                ],
                "max_concurrent_connections_per_source": 64,
                "overflow_action": "reject-source-only-before-backend",
                "provider_load_balancer_id": LB_ID,
                "slow_or_incomplete_connections_covered": True,
                "source_identity": "provider-observed-source-ip",
                "two_client_probe": {
                    "other_source_reached_backend": True,
                    "saturating_source_limited": True,
                },
            },
        },
        "evidence": native_reopened_evidence()["digests"],
    }
    return {
        "schema": EDGE.RECEIPT_SCHEMA,
        "algorithm": EDGE.ALGORITHM,
        "payload": payload,
        "payload_sha256": hashlib.sha256(EDGE._canonical(payload)).hexdigest(),
        "signature": b64url(b"\x00" * 64),
    }


def validation_time() -> datetime:
    return datetime(2026, 9, 17, 12, 30, tzinfo=timezone.utc)


def reopened_evidence(_value: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(native_reopened_evidence())


def test_signed_receipt_derives_identity_without_caller_verdicts(monkeypatch) -> None:
    verified: list[tuple[bytes, bytes, bytes]] = []
    monkeypatch.setattr(
        EDGE,
        "_verify_ed25519",
        lambda key, signature, message: verified.append((key, signature, message)),
    )

    candidate = receipt()
    result = EDGE.verify_receipt(
        candidate,
        trust_store(),
        expected_subject(),
        reopened_evidence(candidate),
        validation_time=validation_time(),
    )

    assert result == {
        "verified": "true",
        "trusted_hops": "1",
        "payload_sha256": candidate["payload_sha256"],
        "issuer_key_id": KEY_ID,
        "provider_load_balancer_id": LB_ID,
        "direct_access_excluded": "true",
        "per_source_connection_limit": "64",
    }
    assert verified and verified[0][0] == PUBLIC_KEY


def test_empty_production_trust_registry_cannot_authorize_a_receipt(monkeypatch) -> None:
    monkeypatch.setattr(
        EDGE,
        "_verify_ed25519",
        lambda *_args: pytest.fail("untrusted evidence must fail before crypto verification"),
    )
    empty = {"schema": EDGE.TRUST_SCHEMA, "issuers": []}
    with pytest.raises(EDGE.ReceiptError, match="source-trusted authority"):
        candidate = receipt()
        EDGE.verify_receipt(
            candidate,
            empty,
            expected_subject(),
            reopened_evidence(candidate),
            validation_time=validation_time(),
        )


def test_payload_tampering_is_rejected_before_signature_verification(monkeypatch) -> None:
    candidate = receipt()
    candidate["payload"]["observations"]["xff"]["header_action"] = "overwrite"
    monkeypatch.setattr(
        EDGE,
        "_verify_ed25519",
        lambda *_args: pytest.fail("digest mismatch must fail before crypto verification"),
    )
    with pytest.raises(EDGE.ReceiptError, match="reopened payload"):
        EDGE.verify_receipt(
            candidate,
            trust_store(),
            expected_subject(),
            reopened_evidence(candidate),
            validation_time=validation_time(),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["payload"]["subject"]["allocation"].update(
                {"id": "vpcallocation-foreign123"}
            ),
            "exact Terraform edge",
        ),
        (
            lambda value: value["payload"]["provider_topology"]["listeners"][0].update(
                {"port": 8080}
            ),
            "exact Gateway listeners",
        ),
        (
            lambda value: value["payload"]["observations"]["xff"].update(
                {"untrusted_prefix_ignored": False}
            ),
            "unforgeable client position",
        ),
        (
            lambda value: value["payload"]["observations"]["direct_access"].update(
                {"node_ports_publicly_routable": True}
            ),
            "exclude direct Envoy access",
        ),
    ],
)
def test_signed_but_wrong_edge_facts_are_rejected(monkeypatch, mutation, message) -> None:
    candidate = copy.deepcopy(receipt())
    mutation(candidate)
    candidate["payload_sha256"] = hashlib.sha256(
        EDGE._canonical(candidate["payload"])
    ).hexdigest()
    monkeypatch.setattr(EDGE, "_verify_ed25519", lambda *_args: None)
    with pytest.raises(EDGE.ReceiptError, match=message):
        EDGE.verify_receipt(
            candidate,
            trust_store(),
            expected_subject(),
            reopened_evidence(candidate),
            validation_time=validation_time(),
        )


def test_signed_digest_must_match_reopened_provider_evidence(monkeypatch) -> None:
    candidate = receipt()
    actual = reopened_evidence(candidate)
    actual["digests"]["routing_export_sha256"] = "9" * 64
    monkeypatch.setattr(EDGE, "_verify_ed25519", lambda *_args: None)
    with pytest.raises(EDGE.ReceiptError, match="reopened provider/LB evidence bytes"):
        EDGE.verify_receipt(
            candidate,
            trust_store(),
            expected_subject(),
            actual,
            validation_time=validation_time(),
        )


def test_fixed_evidence_directory_reopens_and_hashes_every_raw_input(tmp_path) -> None:
    directory = tmp_path / EDGE.EVIDENCE_DIRECTORY
    directory.mkdir(mode=0o700)
    documents = native_evidence_documents()
    expected_digests = {}
    for digest_name, filename in EDGE.EVIDENCE_FILES.items():
        raw = EDGE._canonical(documents[digest_name]) + b"\n"
        path = directory / filename
        path.write_bytes(raw)
        path.chmod(0o600)
        expected_digests[digest_name] = hashlib.sha256(raw).hexdigest()

    assert EDGE._reopen_evidence(tmp_path / EDGE.RECEIPT_FILENAME) == {
        "digests": expected_digests,
        "documents": documents,
    }


def test_signed_connection_cap_cannot_override_native_listener_response(monkeypatch) -> None:
    candidate = receipt()
    candidate["payload"]["observations"]["connection_admission"][
        "max_concurrent_connections_per_source"
    ] = 32
    candidate["payload_sha256"] = hashlib.sha256(
        EDGE._canonical(candidate["payload"])
    ).hexdigest()
    monkeypatch.setattr(EDGE, "_verify_ed25519", lambda *_args: None)
    with pytest.raises(EDGE.ReceiptError, match="native provider evidence"):
        EDGE.verify_receipt(
            candidate,
            trust_store(),
            expected_subject(),
            reopened_evidence(candidate),
            validation_time=validation_time(),
        )


def test_native_connection_probe_must_match_listener_revision(monkeypatch) -> None:
    candidate = receipt()
    evidence = reopened_evidence(candidate)
    evidence["documents"]["connection_isolation_probe_sha256"][
        "listener_revision"
    ] = "revision-foreign"
    evidence["digests"]["connection_isolation_probe_sha256"] = hashlib.sha256(
        EDGE._canonical(
            evidence["documents"]["connection_isolation_probe_sha256"]
        )
        + b"\n"
    ).hexdigest()
    monkeypatch.setattr(EDGE, "_verify_ed25519", lambda *_args: None)
    with pytest.raises(EDGE.ReceiptError, match="native listener revision"):
        EDGE.verify_receipt(
            candidate,
            trust_store(),
            expected_subject(),
            evidence,
            validation_time=validation_time(),
        )


def test_openssl_verifier_accepts_the_rfc8032_empty_message_vector() -> None:
    signature = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155f"
        "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    EDGE._verify_ed25519(PUBLIC_KEY, signature, b"")

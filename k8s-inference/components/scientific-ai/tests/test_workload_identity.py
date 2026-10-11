import io
import json
import urllib.request

import httpx
import jwt
import pytest

from scientific_ai.broker import BrokerObjects
from scientific_ai.workload_identity import PodVerifier, WorkloadIdentity


@pytest.fixture
def verifier(pod_tokens, monkeypatch):
    verifier = PodVerifier(
        WorkloadIdentity(
            audience="https://testserver/internal/storage",
            issuers={
                "test-a": {
                    "issuer": "https://issuer.example/test-a",
                    "jwks_url": "https://issuer.example/test-a/keys",
                }
            },
        )
    )
    monkeypatch.setattr(verifier.keys["test-a"], "fetch_data", lambda: pod_tokens[2])
    return verifier


def verify(verifier, token, **overrides):
    current = {
        "cluster": "test-a",
        "attempt_id": "pod-1",
        "execution": {
            "identity": {
                "ref": "scientific-ai-storage",
                "service_account": "identity-scientific-ai-storage",
                "audience": "https://testserver/internal/storage",
            }
        },
        **overrides,
    }
    verifier.require_current(
        token, current=current, namespace="tenant-system", identity_ref="scientific-ai-storage"
    )


def test_real_projected_signature(verifier, pod_tokens):
    verify(verifier, pod_tokens[1](pod_tokens[0]()))


def test_jwks_transport_identifies_application_and_keeps_verification(verifier, pod_tokens, monkeypatch):
    client = verifier.keys["test-a"]
    monkeypatch.setattr(client, "fetch_data", jwt.PyJWKClient.fetch_data.__get__(client))
    seen = []

    def open_jwks(request, **kwargs):
        assert request.full_url == "https://issuer.example/test-a/keys"
        assert request.get_header("User-agent") == "nebius-scientific-ai/1.0"
        assert "application/jwk-set+json" in request.get_header("Accept")
        assert kwargs["timeout"] == 5
        seen.append(request.full_url)
        return io.BytesIO(json.dumps(pod_tokens[2]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", open_jwks)
    verify(verifier, pod_tokens[1](pod_tokens[0]()))
    verify(verifier, pod_tokens[1](pod_tokens[0]()))
    assert len(seen) == 1  # the short JWKS cache still works


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://untrusted.example"),
        ("aud", "sts.amazonaws.com"),
        ("sub", "system:serviceaccount:tenant-other:identity-scientific-ai-storage"),
        ("exp", 1),
        ("nbf", 4102444800),
        ("iat", 4102444800),
        ("kubernetes.io", {}),
        ("kubernetes.io", "not-a-pod"),
    ],
)
def test_invalid_signed_claims_denied(verifier, pod_tokens, field, value):
    claims = pod_tokens[0]()
    claims[field] = value
    with pytest.raises(PermissionError):
        verify(verifier, pod_tokens[1](claims))


@pytest.mark.parametrize("field", ["exp", "iat", "nbf", "aud", "iss", "sub"])
def test_required_claims(verifier, pod_tokens, field):
    claims = pod_tokens[0]()
    del claims[field]
    with pytest.raises(PermissionError):
        verify(verifier, pod_tokens[1](claims))


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "tenant-other"),
        ("pod", {"name": "pod-1"}),
        ("pod", {"name": "old-pod", "uid": "old-uid"}),
        ("serviceaccount", {"name": "identity-other", "uid": "other-uid"}),
    ],
)
def test_bound_object_claims(verifier, pod_tokens, field, value):
    claims = pod_tokens[0]()
    claims["kubernetes.io"][field] = value
    with pytest.raises(PermissionError):
        verify(verifier, pod_tokens[1](claims))


def test_forged_none_or_symmetric_tokens_rejected(verifier, pod_tokens):
    for algorithm, key in [("none", ""), ("HS256", "forged-symmetric-secret-" + "x" * 32)]:
        token = jwt.encode(pod_tokens[0](), key, algorithm=algorithm, headers={"kid": "test-only"})
        with pytest.raises(PermissionError):
            verify(verifier, token)


def test_unknown_cluster_and_missing_identity_refused(verifier, pod_tokens):
    token = pod_tokens[1](pod_tokens[0]())
    for overrides in [{"cluster": "unknown"}, {"execution": {}}, {"attempt_id": "replacement"}]:
        with pytest.raises(PermissionError):
            verify(verifier, token, **overrides)


def test_key_service_failure_is_retryable(verifier, pod_tokens, monkeypatch):
    def unavailable(*args):
        raise jwt.PyJWKClientConnectionError("private URL is not propagated")

    monkeypatch.setattr(verifier.keys["test-a"], "fetch_data", unavailable)
    with pytest.raises(ConnectionError, match="temporarily unavailable"):
        verify(verifier, pod_tokens[1](pod_tokens[0]()))


def test_broker_rereads_rotated_token_without_restart(tmp_path):
    token_file = tmp_path / "token"
    seen = []

    def transport(request):
        seen.append(request.headers["X-Scientific-Workload-Token"])
        return httpx.Response(200, json={"url": "https://storage.example/object"})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        broker = BrokerObjects(
            http=client,
            url="https://testserver",
            token="job-grant",
            attempt_id="pod-1",
            identity_token_file=token_file,
        )
        for value in ["first-projected-token", "renewed-projected-token"]:
            token_file.write_text(value)
            broker._request("get", "blobs/" + "a" * 64)
        assert seen == ["first-projected-token", "renewed-projected-token"]
        token_file.unlink()
        with pytest.raises(FileNotFoundError):
            broker._request("get", "blobs/" + "a" * 64)
        assert len(seen) == 2

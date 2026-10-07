"""Ready-first artifact routing retains degraded-worker recovery during rollout."""

import httpx
import pytest
from pydantic import ValidationError

from fs2_serve.scientific_batch import companion
from fs2_serve.settings import Settings


def test_fallback_setting_is_optional_and_accepts_service_origin():
    assert Settings().scientific_batch_internal_fallback_api_url is None
    value = Settings(scientific_batch_internal_fallback_api_url="http://artifacts.system.svc:8080")
    assert value.scientific_batch_internal_fallback_api_url == "http://artifacts.system.svc:8080"


@pytest.mark.parametrize(
    "url",
    [
        "https://external.invalid",
        "http://artifacts.system.svc/path",
        "http://user:password@artifacts.system.svc",
        "http://artifacts.system.svc?query=1",
    ],
)
def test_fallback_setting_rejects_non_service_origins(url):
    with pytest.raises(ValidationError, match="fallback API URL"):
        Settings(scientific_batch_internal_fallback_api_url=url)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("failure", ["connect", "503", "dropped-response"])
def test_transient_internal_call_uses_fallback_within_existing_budget(monkeypatch, method, failure):
    seen = []
    monkeypatch.setattr(companion.time, "sleep", lambda _: None)

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            if failure == "connect":
                raise httpx.ConnectError("not ready", request=request)
            if failure == "dropped-response":
                raise httpx.RemoteProtocolError("response lost", request=request)
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = companion.WorkloadArtifactHttpClient(
            base_url="http://ready.system.svc:8080",
            fallback_base_url="http://fallback.system.svc:8080",
            capability="test-capability",
            client=transport,
        )
        url = client.base_url + "/internal/scientific-workloads/artifacts/example:download"
        if method == "GET":

            def read(response):
                response.read()
                return response.json()

            assert client._download_get(url, headers=client.headers, read=read) == {"ok": True}
        else:
            response = client._upload_request("POST", url, headers=client.headers, json_body={"id": "stable"})
            assert response.json() == {"ok": True}
        assert [r.url.host for r in seen] == ["ready.system.svc", "fallback.system.svc"]
        assert all(r.headers["authorization"] == "Bearer test-capability" for r in seen)
        assert len(seen) < companion._ARTIFACT_DOWNLOAD_MAX_ATTEMPTS


def test_fallback_does_not_rewrite_object_handles_or_other_paths():
    with httpx.Client() as transport:
        client = companion.WorkloadArtifactHttpClient(
            base_url="http://ready.system.svc:8080",
            fallback_base_url="http://fallback.system.svc:8080",
            capability="test-capability",
            client=transport,
        )
        for url in ("https://objects.example.invalid/signed-object", client.base_url + "/other"):
            assert client._attempt_url(url, 1) == url


def test_auth_rejection_does_not_try_another_service(monkeypatch):
    seen = []
    monkeypatch.setattr(companion.time, "sleep", lambda _: pytest.fail("must not retry"))

    def handle(request):
        seen.append(request)
        return httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = companion.WorkloadArtifactHttpClient(
            base_url="http://ready.system.svc:8080",
            fallback_base_url="http://fallback.system.svc:8080",
            capability="test-capability",
            client=transport,
        )
        with pytest.raises(httpx.HTTPStatusError):
            client._download_get(
                client.base_url + "/internal/scientific-workloads/a",
                headers=client.headers,
                read=lambda response: response,
            )
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("error_type", [
    httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout,
    httpx.WriteError, httpx.WriteTimeout, httpx.PoolTimeout,
])
def test_upload_transport_retry_is_bounded_and_replays_identical_body(monkeypatch, method, error_type):
    seen, sleeps = [], []
    monkeypatch.setattr(companion.time, "sleep", sleeps.append)

    def handle(request):
        seen.append(request)
        raise error_type("injected transport failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        client = companion.WorkloadArtifactHttpClient(
            base_url="http://ready.system.svc:8080", capability="test", client=transport,
        )
        kwargs = {"json_body": {"upload_ids": ["stable"]}} if method == "POST" else {"content": b"immutable"}
        with pytest.raises(error_type, match="injected transport failure"):
            client._upload_request(method, client.base_url + "/internal/scientific-workloads/uploads:finalize",
                                   headers=client.headers, **kwargs)
    assert len(seen) == companion._ARTIFACT_UPLOAD_MAX_ATTEMPTS
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert all(request.content == seen[0].content for request in seen)

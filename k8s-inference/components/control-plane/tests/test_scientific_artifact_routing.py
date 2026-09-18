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
@pytest.mark.parametrize("failure", ["connect", "503"])
def test_transient_internal_call_uses_fallback_within_existing_budget(monkeypatch, method, failure):
    seen = []
    monkeypatch.setattr(companion.time, "sleep", lambda _: None)

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            if failure == "connect":
                raise httpx.ConnectError("not ready", request=request)
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

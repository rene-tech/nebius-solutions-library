"""Long transfers renew exact immutable handles without extending retry budgets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from fs2_serve.scientific_batch import companion

ARTIFACT_ID = UUID("ac970a14-f987-46fd-827a-32acdccbbbac")
CONTENT = b"closed native checkpoint bytes"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
MEDIA_TYPE = "application/octet-stream"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Transfers:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.requests: list[httpx.Request] = []
        self.downloads = 0
        self.uploads: list[dict] = []
        self.object_calls = 0
        self.object_status = 200
        self.expire_on_transfer = False
        self.renewal_status = 200
        self.renewal_patch: dict = {}
        self.wrong_upload_id = False
        self.first_transfer_status: int | None = None

    def signed(self, method: str, number: int) -> dict:
        return {
            "method": method,
            "url": f"https://objects.test/{number}",
            "headers": {"X-Test-Handle": str(number)},
            "expires_at": (self.clock() + timedelta(minutes=10)).isoformat(),
        }

    def pointer(self) -> dict:
        self.downloads += 1
        artifact = {
            "artifact_id": str(ARTIFACT_ID),
            "sha256": DIGEST,
            "size_bytes": len(CONTENT),
            "media_type": MEDIA_TYPE,
        }
        if self.downloads > 1:
            artifact.update(self.renewal_patch)
        return {"artifact": artifact, "handle": self.signed("GET", self.downloads)}

    def begun(self, value: dict) -> dict:
        self.uploads.append(value)
        upload_id = value["upload_id"]
        if len(self.uploads) > 1 and self.wrong_upload_id:
            upload_id = str(ARTIFACT_ID)
        return {"upload_id": upload_id, "handle": self.signed("PUT", len(self.uploads))}

    def ref(self) -> dict:
        return {"artifact_id": str(ARTIFACT_ID), "sha256": DIGEST,
                "size_bytes": len(CONTENT), "media_type": MEDIA_TYPE}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "objects.test":
            self.object_calls += 1
            assert request.headers["X-Test-Handle"] == request.url.path.removeprefix("/")
            assert "authorization" not in request.headers
            if request.method == "PUT":
                assert request.read() == CONTENT
            if self.expire_on_transfer:
                self.clock.advance(601)
            status = self.object_status
            if self.first_transfer_status is not None and self.object_calls == 1:
                status = self.first_transfer_status
            return httpx.Response(status, stream=httpx.ByteStream(CONTENT))
        assert request.headers["authorization"] == "Bearer test-attempt-capability"
        if request.url.path.endswith("artifacts:download"):
            assert json.loads(request.content) == {"artifact_ids": [str(ARTIFACT_ID)]}
            return httpx.Response(200, json=[self.pointer()])
        if request.url.path.endswith(":download"):
            assert str(ARTIFACT_ID) in request.url.path
            if self.downloads and self.renewal_status != 200:
                return httpx.Response(self.renewal_status)
            return httpx.Response(200, json=self.pointer())
        if request.url.path.endswith("uploads:batch"):
            values = json.loads(request.content)["uploads"]
            return httpx.Response(200, json=[self.begun(value) for value in values])
        if request.url.path.endswith("uploads"):
            if self.uploads and self.renewal_status != 200:
                return httpx.Response(self.renewal_status)
            return httpx.Response(200, json=self.begun(json.loads(request.content)))
        if request.url.path.endswith("uploads:finalize"):
            assert json.loads(request.content) == {"upload_ids": [self.uploads[0]["upload_id"]]}
            return httpx.Response(200, json=[self.ref()])
        if request.url.path.endswith(":finalize"):
            assert self.uploads[0]["upload_id"] in request.url.path
            return httpx.Response(200, json=self.ref())
        raise AssertionError("unexpected artifact path")


@pytest.fixture
def transport(monkeypatch):
    clock = Clock()
    calls = Transfers(clock)
    # Only real retry backoff advances time; tests never sleep.
    monkeypatch.setattr(companion.time, "sleep", clock.advance)
    with httpx.Client(transport=httpx.MockTransport(calls)) as http:
        client = companion.WorkloadArtifactHttpClient(
            base_url="https://platform.test", capability="test-attempt-capability",
            client=http, clock=clock,
        )
        yield client, calls, clock


def download(client, path: Path, *, streamed: bool = True) -> bytes:
    arguments = {"expected_digest": "sha256:" + DIGEST, "expected_size_bytes": len(CONTENT),
                 "expected_media_type": MEDIA_TYPE}
    if streamed:
        client.download_file(ARTIFACT_ID, destination=path, **arguments)
        return path.read_bytes()
    return client.download(ARTIFACT_ID, **arguments)


def upload(client, path: Path, method: str) -> dict:
    arguments = {"identity": "same-native-output", "media_type": MEDIA_TYPE, "compression": None}
    if method == "bytes":
        return client.upload(content=CONTENT, **arguments)
    path.write_bytes(CONTENT)
    if method == "file":
        return client.upload_file(path=path, **arguments)
    return client.upload_files(paths=(path,), **arguments)[0]


def test_prepared_download_renews_after_wait_without_reauthorizing_other_files(transport, tmp_path):
    client, calls, clock = transport
    client.prepare_downloads((ARTIFACT_ID,))
    clock.advance(601)
    assert download(client, tmp_path / "restored.cpt") == CONTENT
    assert calls.downloads == 2 and calls.object_calls == 1
    assert [request.url.path for request in calls.requests] == [
        "/internal/scientific-workloads/artifacts:download",
        f"/internal/scientific-workloads/artifacts/{ARTIFACT_ID}:download", "/2",
    ]


def test_bulk_upload_renews_after_waiting_for_existing_eight_transfer_slots(transport, tmp_path):
    client, calls, clock = transport

    class DelayedSlot:
        def __enter__(self):
            clock.advance(601)

        def __exit__(self, *_):
            return False

    client._native_put_slots = DelayedSlot()
    assert upload(client, tmp_path / "closed.cpt", "bulk") == calls.ref()
    assert calls.object_calls == 1 and len(calls.uploads) == 2
    assert calls.uploads[0] == calls.uploads[1]
    assert calls.requests[-2].url.path == "/2"


@pytest.mark.parametrize("streamed", [False, True])
def test_download_expiry_during_request_renews_only_same_verified_identity(transport, tmp_path, streamed):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.first_transfer_status = 403
    assert download(client, tmp_path / "restored.cpt", streamed=streamed) == CONTENT
    assert calls.downloads == calls.object_calls == 2


@pytest.mark.parametrize("method", ["bytes", "file", "bulk"])
def test_put_expiry_during_request_reuses_exact_upload_reservation(transport, tmp_path, method):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.first_transfer_status = 403
    assert upload(client, tmp_path / "closed.cpt", method) == calls.ref()
    assert calls.object_calls == len(calls.uploads) == 2
    assert calls.uploads[0] == calls.uploads[1]


@pytest.mark.parametrize("method", ["get", "bytes", "file", "bulk"])
def test_valid_handle_403_is_not_retried_or_renewed(transport, tmp_path, method):
    client, calls, _ = transport
    calls.object_status = 403
    with pytest.raises(httpx.HTTPStatusError):
        if method == "get":
            download(client, tmp_path / "restored.cpt")
        else:
            upload(client, tmp_path / "closed.cpt", method)
    assert calls.object_calls == 1
    assert calls.downloads + len(calls.uploads) == 1


@pytest.mark.parametrize("method", ["get", "bytes", "file", "bulk"])
def test_expiry_never_extends_five_data_attempts(transport, tmp_path, method):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.object_status = 403
    with pytest.raises(httpx.HTTPStatusError):
        if method == "get":
            download(client, tmp_path / "restored.cpt")
        else:
            upload(client, tmp_path / "closed.cpt", method)
    assert calls.object_calls == 5
    assert calls.downloads + len(calls.uploads) == 5
    assert not any(request.url.path.endswith(":finalize") for request in calls.requests)


@pytest.mark.parametrize("status", [401, 403, 409])
@pytest.mark.parametrize("method", ["get", "bulk"])
def test_reauthorization_denial_remains_terminal(transport, tmp_path, status, method):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.first_transfer_status = 403
    calls.renewal_status = status
    with pytest.raises(httpx.HTTPStatusError) as caught:
        if method == "get":
            download(client, tmp_path / "restored.cpt")
        else:
            upload(client, tmp_path / "closed.cpt", method)
    assert caught.value.response.status_code == status and calls.object_calls == 1


@pytest.mark.parametrize("patch", [
    {"artifact_id": "331b11b2-432a-47cb-a733-2813d842e1bb"},
    {"sha256": "0" * 64}, {"size_bytes": 1}, {"media_type": "text/plain"},
])
def test_renewed_get_cannot_substitute_a_different_artifact(transport, tmp_path, patch):
    client, calls, clock = transport
    client.prepare_downloads((ARTIFACT_ID,))
    clock.advance(601)
    calls.renewal_patch = patch
    with pytest.raises(ValueError, match="pointer differs"):
        download(client, tmp_path / "restored.cpt")
    assert calls.object_calls == 0 and not (tmp_path / "restored.cpt").exists()


def test_renewed_put_cannot_substitute_a_different_reservation(transport, tmp_path):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.first_transfer_status = 403
    calls.wrong_upload_id = True
    with pytest.raises(ValueError, match="reservation differs"):
        upload(client, tmp_path / "closed.cpt", "bulk")
    assert calls.object_calls == 1


def test_retry_rechecks_expiry_even_after_transient_failure(transport, tmp_path):
    client, calls, _ = transport
    calls.expire_on_transfer = True
    calls.first_transfer_status = 503
    assert download(client, tmp_path / "restored.cpt") == CONTENT
    assert calls.downloads == calls.object_calls == 2


def test_near_expiry_prepared_handle_is_refreshed_before_first_get(transport, tmp_path):
    client, calls, clock = transport
    client.prepare_downloads((ARTIFACT_ID,))
    clock.advance(597)
    assert download(client, tmp_path / "restored.cpt") == CONTENT
    assert calls.downloads == 2 and calls.object_calls == 1

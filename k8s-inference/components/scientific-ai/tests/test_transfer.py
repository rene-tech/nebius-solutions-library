import hashlib
import io
import socket

import httpx
import pytest
from conftest import MemoryObjects

from scientific_ai.contracts import FileRecord
from scientific_ai.storage import Artifacts
from scientific_ai.transfer import retry_transfer


def test_dns_outage_is_retried_with_bounded_backoff(monkeypatch):
    delays, calls = [], []
    monkeypatch.setattr("scientific_ai.transfer.time.sleep", delays.append)

    def transient():
        calls.append(True)
        if len(calls) < 6:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary DNS outage")
        return "delivered"

    assert retry_transfer(transient) == "delivered"
    assert len(calls) == 6 and delays == [0.5, 1, 2, 4, 4]


def test_interrupted_read_restarts_only_its_partial_destination(tmp_path):
    class Partial(io.BytesIO):
        def read(self, n=-1):
            if self.tell():
                raise httpx.ReadTimeout("synthetic network outage")
            return super().read(3)

    class Storage(MemoryObjects):
        reads = 0

        def get(self, key):
            self.reads += 1
            return Partial(b"abcdef") if self.reads == 1 else io.BytesIO(b"abcdef")

    objects = Storage()
    record = FileRecord(path="data", sha256=hashlib.sha256(b"abcdef").hexdigest(), size_bytes=6)
    destination = tmp_path / "data"
    Artifacts(objects).download(record, destination)
    assert destination.read_bytes() == b"abcdef"
    assert objects.reads == 2
    assert not list(tmp_path.glob(".incoming-*"))


def test_lost_put_ack_replays_identical_bytes_not_partial_stream(tmp_path):
    class Storage(MemoryObjects):
        def put(self, key, source):
            super().put(key, source)
            if len(self.puts) == 1:
                raise httpx.ReadTimeout("lost acknowledgement")

    path = tmp_path / "data"
    path.write_bytes(b"abcdef")
    record = FileRecord(path="data", sha256=hashlib.sha256(b"abcdef").hexdigest(), size_bytes=6)
    objects = Storage()
    Artifacts(objects).upload(path, record)
    assert len(objects.puts) == 2
    assert objects.data["blobs/" + record.sha256] == b"abcdef"


def test_transport_retry_does_not_retry_denied_credentials(tmp_path):
    class Storage(MemoryObjects):
        calls = 0

        def get(self, key):
            self.calls += 1
            raise PermissionError("revoked grant")

    objects = Storage()
    with pytest.raises(PermissionError):
        Artifacts(objects).verify("blobs/" + "a" * 64, "a" * 64, 1, tmp_path / "owned")
    assert objects.calls == 1


def test_transport_failure_before_open_does_not_remove_an_existing_destination(tmp_path):
    class Storage(MemoryObjects):
        def get(self, key):
            raise httpx.ReadTimeout("unavailable")

    path = tmp_path / "existing"
    path.write_bytes(b"keep this")
    with pytest.raises(httpx.ReadTimeout):
        Artifacts(Storage()).verify("blobs/" + "a" * 64, "a" * 64, 1, path)
    assert path.read_bytes() == b"keep this"

import io
import json
from unittest.mock import patch

from botocore.exceptions import ClientError
import pytest

import prepare_demo as target


def manifest():
    return {
        "bucket": target.SOURCE_BUCKET,
        "state": {"operation_id": target.SOURCE_OPERATION, "generation": 71,
                  "job_id": "mas1-20e", "commands": [{"checkpoint_step": 13963440}]},
        "files": [{"path": f"history-{i}.log", "sha256": "a" * 64,
                   "key": target.SOURCE_PREFIX + "mas1-20e/objects/" + "a" * 64,
                   "size_bytes": 82674112 if i == 0 else 0} for i in range(305)],
    }


def validate(value):
    raw = json.dumps(value).encode()
    with patch.object(target, "SOURCE_SHA", target.sha(raw)):
        return target.validate_manifest(raw)


def test_exact_inventory():
    assert len(validate(manifest())["files"]) == 305


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", "a//b", "a\\b", "./x", ""])
def test_paths(path):
    with pytest.raises(ValueError):
        target.safe_path(path)


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(bucket=target.DEMO_BUCKET),
    lambda m: m["state"].update(operation_id="other-operation"),
    lambda m: m["state"].update(generation=72),
    lambda m: m["state"]["commands"][0].update(checkpoint_step=13963439),
    lambda m: m["files"][0].update(key="another-tenant/objects/" + "a" * 64),
    lambda m: m["files"][0].update(path="history-1.log"),
    lambda m: m["files"][0].update(sha256="z" * 64),
    lambda m: m["files"][0].update(size_bytes=-1),
    lambda m: m["files"].pop(),
])
def test_changed_source_rejected(mutation):
    value = manifest()
    mutation(value)
    with pytest.raises(ValueError):
        validate(value)


def test_manifest_digest_is_pinned():
    with pytest.raises(ValueError, match="source manifest changed"):
        target.validate_manifest(b"{}")


class ObjectStore:
    def __init__(self, existing=None):
        self.existing = existing
        self.writes = []

    def put_object(self, **kwargs):
        self.writes.append(kwargs)
        assert kwargs["Bucket"] == target.DEMO_BUCKET
        assert kwargs["IfNoneMatch"] == "*"
        if self.existing is not None:
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")
        self.existing = kwargs["Body"]

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == target.DEMO_BUCKET
        return {"ContentLength": len(self.existing), "Body": io.BytesIO(self.existing)}


def test_conditional_copy_verifies_bytes():
    store = ObjectStore()
    target.put_once(store, "private-run/example", b"input")
    assert len(store.writes) == 1


def test_repeat_copy_requires_identical_destination():
    store = ObjectStore(b"input")
    target.put_once(store, "private-run/example", b"input")
    assert store.existing == b"input"


def test_conflicting_destination_is_not_overwritten():
    store = ObjectStore(b"other")
    with pytest.raises(ValueError, match="checksum mismatch"):
        target.put_once(store, "private-run/example", b"input")
    assert store.existing == b"other"


def test_nonconditional_put_error_not_masked():
    store = ObjectStore()
    with patch.object(store, "put_object", side_effect=ClientError(
        {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject"
    )), pytest.raises(ClientError):
        target.put_once(store, "private-run/example", b"input")

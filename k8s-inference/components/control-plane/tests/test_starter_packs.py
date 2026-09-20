"""No customer object may be replaced, including racing conditional creates."""

import hashlib
import io
import json

import pytest
from botocore.exceptions import ClientError

from fs2_serve.starter_packs import SCHEMA, BucketPackInstaller, PackError, StarterPack


def error(status):
    return ClientError({"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}}, "test")


class S3:
    def __init__(self):
        self.objects, self.puts = {}, []
        self.fail_at, self.race = None, None

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's public keyword contract
        if Key not in self.objects:
            raise error(404)
        data = self.objects[Key]
        return {"ContentLength": len(data), "Body": io.BytesIO(data)}

    def put_object(self, Bucket, Key, Body, IfNoneMatch, ContentType=None):  # noqa: N803
        assert IfNoneMatch == "*"
        self.puts.append(Key)
        if Key == self.fail_at:
            raise error(503)
        if self.race:
            self.objects[Key] = self.race
        if Key in self.objects:
            raise error(412)
        self.objects[Key] = Body

    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Size": len(v)} for v in self.objects.values()], "IsTruncated": False}


def pack(tmp_path, **override):
    objects = []
    for name, content in [("README.md", b"demo index\n"), ("structure/protein.fasta", b">ubiquitin\nMQIFVKTL\n")]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        objects.append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "media_type": "text/plain",
                "recipe_version": "1",
                "validation_status": "offline-validated",
                "provenance": {
                    "source": "authored test fixture",
                    "license": "CC0-1.0",
                    "attribution": "test author",
                    "transformation": "none",
                },
            }
        )
    value = {"schema": SCHEMA, "version": "v1", "release_status": "qualified", "objects": objects, **override}
    (tmp_path / "manifest.json").write_text(json.dumps(value))
    return StarterPack.load(tmp_path)


def test_install_verifies_complete_set_and_manifest_last(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    result = BucketPackInstaller(s3, "owned", quota_bytes=5000000).install(candidate)
    assert result["state"] == "complete"
    assert result["object_count"] == 3
    assert result["total_bytes"] == sum(map(len, s3.objects.values()))
    assert s3.puts[-1] == "examples/v1/manifest.json"


def test_same_content_adopted_without_rewrite(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    installer = BucketPackInstaller(s3, "owned", quota_bytes=5000000)
    installer.install(candidate)
    s3.puts.clear()
    result = installer.install(candidate)
    # The immutable completion manifest is conditionally adopted too.
    assert s3.puts == ["examples/v1/manifest.json"]
    assert result["created_objects"] == 0
    assert result["adopted_objects"] == 2


@pytest.mark.parametrize(
    "key,code",
    [
        ("examples/v1/README.md", "existing_object_conflict"),
        ("examples/v1/manifest.json", "existing_manifest_conflict"),
    ],
)
def test_customer_edits_preserved_and_no_completion(tmp_path, key, code):
    candidate, s3 = pack(tmp_path), S3()
    s3.objects[key] = b"customer modification"
    with pytest.raises(PackError, match=code):
        BucketPackInstaller(s3, "owned", quota_bytes=5000000).install(candidate)
    assert s3.objects[key] == b"customer modification"
    assert s3.puts == []


def test_racing_customer_write_is_preserved(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    s3.race = b"concurrent customer write"
    with pytest.raises(PackError, match="existing_object_conflict"):
        BucketPackInstaller(s3, "owned", quota_bytes=5000000).install(candidate)
    assert set(s3.objects.values()) == {s3.race}
    assert "examples/v1/manifest.json" not in s3.objects


def test_partial_upload_resumes_create_only(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    s3.fail_at = "examples/v1/structure/protein.fasta"
    installer = BucketPackInstaller(s3, "owned", quota_bytes=5000000)
    with pytest.raises(ClientError):
        installer.install(candidate)
    assert "examples/v1/manifest.json" not in s3.objects
    assert s3.objects["examples/v1/README.md"] == b"demo index\n"
    s3.fail_at = None
    result = installer.install(candidate)
    assert result["adopted_objects"] == 1
    assert result["created_objects"] == 1


def test_no_write_if_quota_insufficient(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    s3.objects["customer/work-log.txt"] = b"protected history"
    with pytest.raises(PackError, match="insufficient_bucket_headroom"):
        BucketPackInstaller(s3, "owned", quota_bytes=candidate.total_bytes).install(candidate)
    assert s3.puts == []
    assert s3.objects["customer/work-log.txt"] == b"protected history"


def test_refuses_unqualified_release(tmp_path):
    with pytest.raises(PackError, match="pack_not_qualified"):
        pack(tmp_path, release_status="draft")
    assert StarterPack.load(tmp_path, allow_unqualified=True).version == "v1"


@pytest.mark.parametrize(
    "change,code",
    [
        ({"path": "../customer-data"}, "object_path_invalid"),
        ({"path": "/etc/passwd"}, "object_path_invalid"),
        ({"path": "manifest.json"}, "object_path_invalid"),
        ({"provenance": {}}, "object_provenance_missing"),
        ({"sha256": "0" * 64}, "local_object_checksum_mismatch"),
        ({"size_bytes": 32 * 1024 * 1024 + 1}, "object_size_out_of_bounds"),
    ],
)
def test_manifest_validation(tmp_path, change, code):
    candidate = pack(tmp_path)
    data = json.loads(candidate.manifest)
    data["objects"][0].update(change)
    (tmp_path / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(PackError, match=code):
        StarterPack.load(tmp_path)


def test_provider_error_never_retried_as_unconditional_put(tmp_path):
    candidate, s3 = pack(tmp_path), S3()
    s3.fail_at = "examples/v1/README.md"
    with pytest.raises(ClientError):
        BucketPackInstaller(s3, "owned", quota_bytes=5000000).install(candidate)
    assert s3.puts == [s3.fail_at]
    assert not s3.objects

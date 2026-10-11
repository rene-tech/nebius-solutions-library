import io
from datetime import UTC, datetime, timedelta

import boto3
import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from scientific_ai.identity import s3_client, web_identity_session
from scientific_ai.storage import S3Objects


def test_sdk_renews_twice_reads_rotated_token_and_never_uses_ambient_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "wrong-ambient-tenant")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wrong-ambient-secret")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::999999999999:role/wrong")
    token = tmp_path / "projected-token"
    token.write_text("token-1")
    sts = boto3.client("sts", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test")
    stub = Stubber(sts)
    role = "arn:aws:iam::123456789012:role/per-job-test"
    for number in range(1, 4):
        stub.add_response(
            "assume_role_with_web_identity",
            {
                "Credentials": {
                    "AccessKeyId": f"ASIA{number:016d}",
                    "SecretAccessKey": "test" * 10,
                    "SessionToken": f"temporary-{number}",
                    "Expiration": datetime.now(UTC) + timedelta(seconds=60),
                },
            },
            {"RoleArn": role, "RoleSessionName": "attempt-opaque", "WebIdentityToken": f"token-{number}"},
        )
    stub.add_client_error(
        "assume_role_with_web_identity", service_error_code="AccessDenied", http_status_code=403
    )
    original = botocore.session.Session.create_client

    def clients(self, service_name, *args, **kwargs):
        return sts if service_name == "sts" else original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(botocore.session.Session, "create_client", clients)
    session = web_identity_session(
        role_arn=role, token_file=token, session_name="attempt-opaque", region="eu-west-1"
    )
    assert session.get_credentials().method == "assume-role-with-web-identity"
    with stub:
        for number in range(1, 4):
            token.write_text(f"token-{number}")
            credentials = session.get_credentials().get_frozen_credentials()
            assert credentials.access_key == f"ASIA{number:016d}"
        with pytest.raises(ClientError):
            session.get_credentials().get_frozen_credentials()
    stub.assert_no_pending_responses()
    assert "wrong-ambient" not in credentials.access_key


def test_missing_token_does_not_fall_back_to_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    session = web_identity_session(
        role_arn="arn:aws:iam::123456789012:role/test",
        token_file=tmp_path / "missing",
        session_name="attempt",
        region="eu-west-1",
    )
    with pytest.raises(FileNotFoundError):
        session.get_credentials().get_frozen_credentials()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://insecure.invalid",
        "https://user:secret@s3.invalid",
        "https://s3.invalid?credential=secret",
        "file:///private",
    ],
)
def test_storage_endpoint_is_not_a_credential_transport(endpoint):
    with pytest.raises(ValueError):
        s3_client(None, endpoint_url=endpoint)


def test_real_boto3_s3_transfer_wiring_uses_only_scoped_prefix():
    client = boto3.client(
        "s3", region_name="eu-west-1", aws_access_key_id="test", aws_secret_access_key="test"
    )
    store = S3Objects(client, bucket="synthetic-test", prefix="approved/run-1")
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"opaque-etag"'},
            {
                "Bucket": "synthetic-test",
                "Key": "approved/run-1/blobs/object",
                "Body": ANY,
                "ChecksumAlgorithm": ANY,
            },
        )
        store.put("blobs/object", io.BytesIO(b"payload"))
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(io.BytesIO(b"payload"), 7)},
            {
                "Bucket": "synthetic-test",
                "Key": "approved/run-1/blobs/object",
            },
        )
        body = store.get("blobs/object")
        assert body.read() == b"payload"
        body.close()
        stub.assert_no_pending_responses()


def test_aws_client_factory_rejects_static_key_session():
    session = boto3.Session(aws_access_key_id="synthetic-static", aws_secret_access_key="synthetic-static")
    with pytest.raises(PermissionError):
        s3_client(session)

import json

import httpx
import pytest

from scientific_ai.serverless import ServerlessClient, ServerlessError, UnsupportedExecution, invoke_payload


def client(handler):
    return ServerlessClient(
        httpx.Client(base_url="https://fleet.invalid", transport=httpx.MockTransport(handler))
    )


def discovery():
    return {
        "io_modes": ["managed", "application"],
        "identities": ["customer-a"],
        "execution_profiles": ["confidential-job-v1"],
        "private_input_dir": "/var/run/serverless2/private",
        "identity_token_file": "/var/run/secrets/workload-identity/token",
    }


def test_confidential_binding_does_not_require_placeholder_aws_identity(setup):
    args, _, _ = setup
    payload = invoke_payload(
        args["profile"], profile_ref="confidential-job-v1", identity_ref=None, private_input_ref="job-1"
    )
    assert "workload_identity_ref" not in payload["execution"]
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"execution": discovery()})
        return httpx.Response(202, json={"id": "op-gromacs-1", "status": "QUEUED"})

    response = client(handler).submit("gromacs", payload, idempotency_key="stable-request-1")
    assert response["status"] == "QUEUED"
    assert [r.url.path for r in seen] == ["/v1/fleet", "/v1/models/gromacs:invoke"]
    assert json.loads(seen[-1].content) == payload


@pytest.mark.parametrize(
    "profile,identity", [("other-profile", None), ("confidential-job-v1", "other-tenant")]
)
def test_discovery_checks_tenant_profile_and_identity(profile, identity):
    c = client(lambda request: httpx.Response(200, json={"execution": discovery()}))
    with pytest.raises(UnsupportedExecution):
        c.require_capabilities(profile_ref=profile, identity_ref=identity)


def test_private_binding_transport_and_explicit_withdrawal():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ref": "job-1"}) if request.method == "PUT" else httpx.Response(204)

    c = client(handler)
    c.put_binding("job-1", {"manifest.json": '{"synthetic":"private-sentinel"}'})
    c.delete_binding("job-1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/v1/bindings/job-1"),
        ("DELETE", "/v1/bindings/job-1"),
    ]
    assert "private-sentinel" not in str(seen[0].url)


@pytest.mark.parametrize(
    "reference,data",
    [
        ("../../outside", {"valid": "x"}),
        ("job-1", {"../token": "x"}),
        ("job-1", {"valid": "x" * 65536}),
        ("job-1", {"valid": "é" * 32768}),
        ("job-1", {}),
    ],
)
def test_invalid_binding_never_reaches_serverless(reference, data):
    c = client(lambda request: pytest.fail("invalid binding was transmitted"))
    with pytest.raises(ValueError):
        c.put_binding(reference, data)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 429, 500, 503])
def test_error_does_not_echo_private_body_or_retry(status):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, json={"detail": "private-sentinel"}, headers={"Retry-After": "15"})

    with pytest.raises(ServerlessError) as error:
        client(handler).put_binding("job-1", {"manifest.json": "synthetic"})
    assert "private-sentinel" not in str(error.value)
    assert error.value.status_code == status and error.value.retry_after == "15"
    assert len(seen) == 1


def test_resume_uses_portable_checkpoint_and_actual_wire_shape():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"id": "op-gromacs-1-r1", "status": "QUEUED"})

    c = client(handler)
    c.resume("op-gromacs-1", checkpoint_ref="a" * 64, gpu_class="l40s")
    assert seen[0].url.path == "/v1/operations/op-gromacs-1:resume"
    assert json.loads(seen[0].content) == {"checkpoint_ref": "a" * 64, "gpu_class": "l40s"}


def test_result_unavailable_remains_409():
    c = client(lambda request: httpx.Response(409, json={"status": "RUNNING"}))
    with pytest.raises(ServerlessError) as error:
        c.result("op-gromacs-1")
    assert error.value.status_code == 409


@pytest.mark.parametrize("method", ["status", "result"])
def test_application_completion_without_durable_result_is_not_success(method):
    c = client(
        lambda request: httpx.Response(
            200,
            json={
                "status": "SUCCEEDED",
                "execution": {"io_mode": "application"},
                "result_ref": None,
            },
        )
    )
    with pytest.raises(ServerlessError) as error:
        getattr(c, method)("op-gromacs-1")
    assert error.value.status_code == 503

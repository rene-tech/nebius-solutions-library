import json

import httpx
import pytest
from test_gateway import gateway_fixture, submit_request  # noqa: F401

from scientific_ai.serverless import ServerlessClient, UnsupportedExecution


@pytest.mark.parametrize("transport", ["rest", "mcp"])
def test_delivery_retry_preserves_handle_and_never_submits_science(request, setup, transport):
    gateway, client, _, calls, operations, _ = request.getfixturevalue("gateway_fixture")
    gateway.settings = gateway.settings.model_copy(update={"delivery_retry_enabled": True})
    request = submit_request(client, setup)
    submitted = client.post(
        "/v1/models/gromacs:submit", json=request, headers={"Idempotency-Key": "delivery-test-original"}
    ).json()
    op = submitted["operation_id"]
    next(iter(operations.values())).update(status="FAILED")
    for _ in range(2):
        if transport == "rest":
            response = client.post(
                f"/v1/operations/{op}:export", headers={"Idempotency-Key": "delivery-test-retry"}
            )
            assert response.status_code == 202, response.text
            result = response.json()
        else:
            response = client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "retry_scientific_delivery",
                        "arguments": {
                            "operation_id": op,
                            "idempotency_key": "delivery-test-retry",
                        },
                    },
                },
            )
            assert response.status_code == 200, response.text
            envelope = response.json()["result"]
            result = envelope.get("structuredContent") or json.loads(envelope["content"][0]["text"])
        assert result["operation_id"] == op and result["status"] == "FINALIZING"
    assert len([p for _, p in calls if p.endswith(":invoke")]) == 1
    foreign = client.post(
        f"/v1/operations/{op}:export",
        headers={"Authorization": "Bearer other", "Idempotency-Key": "delivery-foreign"},
    )
    assert foreign.status_code == 404


def test_delivery_unavailable_without_platform_capability():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "execution": {
                    "io_modes": ["application"],
                    "execution_profiles": [],
                    "identities": [],
                    "private_input_dir": "/var/run/serverless2/private",
                }
            },
        )

    with httpx.Client(base_url="https://s2.example", transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(UnsupportedExecution, match="delivery retry"):
            ServerlessClient(http).export("op-internal", idempotency_key="export-test")
    assert calls == ["/v1/fleet"]

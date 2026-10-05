import json
from uuid import UUID

from capture_api_readiness import select_events


def test_capture_keeps_probes_and_only_target_resume_metadata():
    source = UUID("33b398b6-4311-4400-9a0b-67dfa2e596b5")
    event = {
        "method": "POST",
        "path": f"/v1/operations/{source}:resume",
        "status": 202,
        "duration_ms": 123,
        "request_id": "internal-request",
        "response_complete": True,
        "disconnected": False,
        "authorization": "must-not-be-retained",
        "body": "must-not-be-retained",
    }
    other = {**event, "path": "/v1/operations/other:resume", "request_id": "unrelated-customer"}
    lines = [
        '2026-10-05T17:07:00Z INFO: "GET /readyz HTTP/1.1" 503 Service Unavailable',
        '2026-10-05T17:07:10Z INFO: "GET /readyz HTTP/1.1" 200 OK',
        "2026-10-05T17:07:01Z INFO fs2_serve.access " + json.dumps(event),
        "2026-10-05T17:07:02Z INFO fs2_serve.access " + json.dumps(other),
        "2026-10-05T17:07:03Z INFO fs2_serve.access incomplete",
    ]
    result = select_events("\n".join(lines), source)
    assert [item["status"] for item in result["probes"]] == [503, 200]
    assert len(result["admissions"]) == 1
    assert result["admissions"][0]["duration_ms"] == 123
    rendered = json.dumps(result)
    assert "must-not-be-retained" not in rendered and "unrelated-customer" not in rendered


def test_missing_data_is_empty_not_a_success_measurement():
    assert select_events("", UUID(int=1)) == {"probes": [], "admissions": []}

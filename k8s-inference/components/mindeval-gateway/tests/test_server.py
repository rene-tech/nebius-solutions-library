import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fs2_mindeval.adapter import TokenFactoryAdapter
from fs2_mindeval.contracts import Identity
from fs2_mindeval.prompts import CRITERIA, PROFILES, PROVENANCE, profile_detail
from fs2_mindeval.scheduler import FairScheduler
from fs2_mindeval.server import create_app
from fs2_mindeval.store import Store

PATIENT = "Qwen/Qwen3-30B-A3B-Instruct-2507"
CLINICIAN = "openai/gpt-oss-120b"
JUDGE = "google/gemma-3-27b-it"
AUTH = {"authorization": "Bearer platform-team-one"}


def fixture_app(tmp_path, *, bad_judge=False):
    async def identity(auth):
        return Identity(
            tenant_id="shared-workshop",
            principal_id=auth,
            token_id="id",
            scopes={"inference.invoke"},
            models={"mindeval"} if "denied" not in auth else {"qwen3-8b"},
            max_concurrency=5,
        )

    def provider(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": model} for model in (PATIENT, CLINICIAN, JUDGE)]})
        body = json.loads(request.content)
        content = "A reply"
        if body["model"] == JUDGE:
            content = "not a judgment" if bad_judge else "\n".join(f"{criterion}: 3.75" for criterion in CRITERIA)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 10},
            },
        )

    adapter = TokenFactoryAdapter(
        "PROVIDER_SECRET", FairScheduler(), client=httpx.AsyncClient(transport=httpx.MockTransport(provider))
    )
    return create_app(
        adapter=adapter, store=Store(str(tmp_path / "store.sqlite")), identity_provider=identity, judge_model=JUDGE
    )


def register(client, **overrides):
    return client.post(
        "/v1/mindeval/runs/run-1/register",
        headers=AUTH,
        json={"profile_ids": ["profile-000"], "patient_model": PATIENT, "clinician_models": [CLINICIAN], **overrides},
    )


def test_exact_upstream_assets():
    import hashlib
    from fs2_mindeval.prompts import ASSETS, TEMPLATES

    assert len(PROFILES) == 50
    for key, value in TEMPLATES.items():
        assert hashlib.sha256(value.encode()).hexdigest() == PROVENANCE["template_sha256"][key]
    assert (
        hashlib.sha256((ASSETS / "profiles.jsonl").read_bytes()).hexdigest()
        == PROVENANCE["sha256"]["data/profiles.jsonl"]
    )
    assert profile_detail("profile-000")["profile"]["name"] == "Dennis"
    with pytest.raises(ValueError):
        profile_detail("profile-050")


def test_bearer_required_identity_spoofing_ignored_and_grants_enforced(tmp_path):
    with TestClient(fixture_app(tmp_path)) as client:
        assert client.get("/v1/mindeval/catalog").status_code == 401
        result = register(client)
        assert result.status_code == 200
        assert "PROVIDER_SECRET" not in result.text
        spoof = client.get(
            "/v1/mindeval/runs/run-1/events",
            headers={"authorization": "Bearer other-team", "x-fs2-principal": "platform-team-one"},
        )
        assert spoof.status_code == 404
        denied = client.post(
            "/v1/mindeval/runs/denied/register",
            headers={"authorization": "Bearer denied"},
            json={"profile_ids": ["profile-000"], "patient_model": PATIENT, "clinician_models": [CLINICIAN]},
        )
        assert denied.status_code == 403


def test_registration_immutable_max20_and_replays_survive_restart(tmp_path):
    with TestClient(fixture_app(tmp_path)) as client:
        original = register(client).json()
        assert register(client).json() == original
        assert register(client, profile_ids=["profile-001"]).status_code == 409
        assert register(client, profile_ids=[f"profile-{i:03d}" for i in range(21)]).status_code == 422
    with TestClient(fixture_app(tmp_path)) as client:
        assert register(client).json() == original


def test_role_registration_and_observable_judgments(tmp_path):
    with TestClient(fixture_app(tmp_path)) as client:
        register(client)
        request = {
            "run_id": "run-1",
            "profile_id": "profile-000",
            "role": "clinician",
            "model": PATIENT,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        assert client.post("/v1/mindeval/completions", headers=AUTH, json=request).status_code == 403
        request["model"] = CLINICIAN
        result = client.post("/v1/mindeval/completions", headers=AUTH, json=request)
        assert result.status_code == 200
        judgment = client.post(
            "/v1/mindeval/judgments",
            headers=AUTH,
            json={
                "run_id": "run-1",
                "profile_id": "profile-000",
                "clinician_model": CLINICIAN,
                "interaction": [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Welcome"}],
            },
        )
        assert judgment.status_code == 200
        assert judgment.json()["overall_score"] == 3.75
        events = client.get("/v1/mindeval/runs/run-1/events", headers=AUTH).json()["data"]
        assert len(events) == 2
        assert events[1]["role"] == "judge"
        assert "queue_ms" in events[1]


def test_failed_judgment_persists_visible_failure_without_scores(tmp_path):
    with TestClient(fixture_app(tmp_path, bad_judge=True)) as client:
        register(client)
        result = client.post(
            "/v1/mindeval/judgments",
            headers=AUTH,
            json={
                "run_id": "run-1",
                "profile_id": "profile-000",
                "clinician_model": CLINICIAN,
                "interaction": [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Welcome"}],
            },
        )
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "invalid_judgment"
        events = client.get("/v1/mindeval/runs/run-1/events", headers=AUTH).json()["data"]
        assert events[0]["status"] == "failed"
        assert "judgment" not in events[0]

import httpx
import pytest
import pytest_asyncio

from fs2_workshop.app import create_app
from fs2_workshop.models import Settings


@pytest_asyncio.fixture
async def api(store):
    settings = Settings(
        database_url="unused", auth_url="http://platform/internal/ext-authz", gateway_url="http://gateway"
    )
    async with httpx.AsyncClient() as upstream:
        app = create_app(settings, store=store, client=upstream, start_workers=False)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            yield client


def authentication(request):
    token = request.headers.get("authorization", "")
    if token == "Bearer invalid":
        return httpx.Response(401)
    return httpx.Response(
        200,
        headers={
            "x-fs2-tenant": "event",
            "x-fs2-principal": token.split()[-1],
            "x-fs2-token-id": "test",
            "x-fs2-scopes": '["inference.invoke"]',
            "x-fs2-models": '["mindeval"]',
            "x-fs2-max-concurrency": "5",
        },
    )


@pytest.fixture
def auth(respx_mock):
    respx_mock.get("http://platform/internal/ext-authz").mock(side_effect=authentication)
    return {"Authorization": "Bearer team1"}


async def test_catalog_login_and_static_ui(api, auth, respx_mock):
    respx_mock.get("http://gateway/v1/mindeval/catalog").respond(200, json={"data": [], "judge_model": "fixed"})
    respx_mock.get("http://gateway/v1/mindeval/profiles").respond(200, json={"data": []})
    page = await api.get("/workshop")
    assert page.status_code == 200 and 'id="create-form"' in page.text
    script = await api.get("/workshop/static/app.js")
    assert script.status_code == 200 and "localStorage" not in script.text
    response = await api.get("/v1/workshop/catalog", headers=auth)
    assert response.status_code == 200 and response.json()["limits"]["workers_per_team"] == 5


async def test_participant_run_controls_and_report(api, auth):
    body = {"profile_ids": ["profile-000"], "patient_model": "patient", "clinician_models": ["clinician"]}
    response = await api.post("/v1/workshop/runs", headers={**auth, "Idempotency-Key": "unique"}, json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["data"][0]["id"]
    path = f"/v1/workshop/runs/{run_id}"
    pause = await api.post(path + "/interventions", headers=auth, json={"action": "pause"})
    assert pause.json()["status"] == "paused"
    assert (await api.get(path, headers={"Authorization": "Bearer team2"})).status_code == 404
    assert (await api.get(path + "/report", headers={"Authorization": "Bearer team2"})).status_code == 404
    report = await api.get(path + "/report", headers=auth)
    assert report.status_code == 200 and "attachment" in report.headers["content-disposition"]
    assert "credential_ciphertext" not in report.text
    assert report.json()["events"][-1]["kind"] == "intervention.pause"
    for action in ("resume", "takeover"):
        result = await api.post(path + "/interventions", headers=auth, json={"action": action})
        assert result.status_code == 200
    said = await api.post(path + "/interventions", headers=auth, json={"action": "say", "text": "Human example"})
    assert said.json()["state"]["transcript"][-1]["human"]
    abort = await api.post(path + "/interventions", headers=auth, json={"action": "abort"})
    assert abort.json()["status"] == "aborted"
    assert (await api.post(path + "/interventions", headers=auth, json={"action": "resume"})).status_code == 409


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
async def test_missing_or_invalid_key_never_gets_runs(api, auth, headers):
    assert (await api.get("/v1/workshop/runs", headers=headers)).status_code == 401


async def test_idempotency_required_and_unsupported_arguments_rejected(api, auth):
    body = {"profile_ids": ["profile-000"], "patient_model": "patient", "clinician_models": ["clinician"]}
    assert (await api.post("/v1/workshop/runs", headers=auth, json=body)).status_code == 400
    body["invented_option"] = True
    assert (await api.post("/v1/workshop/runs", headers={**auth, "Idempotency-Key": "a"}, json=body)).status_code == 422


async def test_missing_auth_contract_is_explicit(api, respx_mock):
    respx_mock.get("http://platform/internal/ext-authz").respond(200, headers={"x-fs2-tenant": "event"})
    response = await api.get("/v1/workshop/runs", headers={"Authorization": "Bearer team1"})
    assert response.status_code == 503 and "contract" in response.text


async def test_ungranted_workflow_is_denied(api, respx_mock):
    response = authentication(httpx.Request("GET", "http://test", headers={"Authorization": "Bearer team1"}))
    response.headers["x-fs2-models"] = '["cosmos3-nano"]'
    respx_mock.get("http://platform/internal/ext-authz").mock(return_value=response)
    assert (await api.get("/v1/workshop/runs", headers={"Authorization": "Bearer team1"})).status_code == 403

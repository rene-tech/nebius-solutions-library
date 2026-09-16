import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "public_rehearsal", Path(__file__).parents[1] / "scripts/rehearse_workshop.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def make_run():
    return {
        "id": "example-run",
        "state": {
            "judgment": {"judgment": dict.fromkeys(runner.CRITERIA, 4.5), "model": "judge"},
            "benchmark_eligible": True,
            "transcript": [{"content": "example"} for _ in range(5)],
        },
    }


def test_completed_requires_exact_named_finite_axes():
    row = make_run()
    runner.validate_completed(row, "judge")
    row["state"]["judgment"]["judgment"].pop(next(iter(runner.CRITERIA)))
    with pytest.raises(runner.AcceptanceFailure, match="five-axis"):
        runner.validate_completed(row, "judge")


@pytest.mark.parametrize("score", [True, float("nan"), float("inf"), 0, 7, "3"])
def test_completed_rejects_invalid_scores(score):
    row = make_run()
    row["state"]["judgment"]["judgment"][next(iter(runner.CRITERIA))] = score
    with pytest.raises(runner.AcceptanceFailure, match="five-axis"):
        runner.validate_completed(row, "judge")


def test_rejects_changed_judge_or_nonbenchmark_run():
    row = make_run()
    with pytest.raises(runner.AcceptanceFailure, match="different judge"):
        runner.validate_completed(row, "another-judge")
    row["state"]["benchmark_eligible"] = False
    with pytest.raises(runner.AcceptanceFailure, match="not eligible"):
        runner.validate_completed(row, "judge")


def test_duplicate_credentials_fail(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(["duplicate"] * 10))
    with pytest.raises(runner.AcceptanceFailure, match="distinct"):
        runner.read_credentials(path)


async def test_preflight_and_secret_safe_report(tmp_path):
    teams = [{"label": f"team-{i}", "token": f"test-private-token-{i}"} for i in range(10)]
    args = SimpleNamespace(
        output=str(tmp_path),
        insecure=False,
        ca_file=None,
        base_url="https://example.test",
        run_label="mock",
        gateway_image="gateway@sha256:test",
        workshop_image="workshop@sha256:test",
    )
    rehearsal = runner.Rehearsal(args, teams, "test-denied-token")
    requests = []

    def respond(request):
        requests.append(request)
        authorization = request.headers.get("Authorization", "")
        if not authorization:
            return httpx.Response(401, json={"detail": "denied"})
        if authorization == "Bearer test-denied-token":
            assert request.url.path == "/v1/mindeval/runs/mock-profile-limit-denied/register"
            return httpx.Response(403, json={"detail": "grant required"})
        team = next(i for i, t in enumerate(teams) if authorization == f"Bearer {t['token']}")
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "identity": {"tenant_id": "same-tenant", "principal_id": f"principal-{team}"},
                    "limits": {"workers_per_team": 5},
                    "catalog": {
                        "judge_model": "judge",
                        "data": [
                            {"id": f"model-{i}", "clinician_eligible": True, "patient_eligible": i == 0}
                            for i in range(6)
                        ],
                    },
                    "profiles": {"data": [{"id": f"profile-{i:03}"} for i in range(50)]},
                },
            )
        body = json.loads(request.content)
        return httpx.Response(422 if len(body["profile_ids"]) == 21 else 200, json=body)

    await rehearsal.client.aclose()
    rehearsal.client = httpx.AsyncClient(base_url=args.base_url, transport=httpx.MockTransport(respond))
    try:
        await rehearsal.preflight()
        assert all(item["passed"] for item in rehearsal.summary["checks"])
        assert len(requests) == 15
        with pytest.raises(runner.AcceptanceFailure, match="credential detected"):
            rehearsal.save("must-not-exist.json", {"accidental_secret": teams[0]["token"]})
        assert not (tmp_path / "must-not-exist.json").exists()
    finally:
        await rehearsal.client.aclose()

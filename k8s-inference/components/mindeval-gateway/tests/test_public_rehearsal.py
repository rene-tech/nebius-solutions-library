import asyncio
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


def test_full_dialogue_checks_complete_round_count():
    row = make_run()
    with pytest.raises(runner.AcceptanceFailure, match="wrong length"):
        runner.validate_completed(row, "judge", turns=10)
    row["state"]["transcript"] = [{"content": "example"} for _ in range(21)]
    runner.validate_completed(row, "judge", turns=10)


def test_classifier_requires_actual_full_prefix_coverage():
    row = {"state": {"transcript": [{"role": "patient"}], "classification": {"status": "unavailable"}}}
    with pytest.raises(runner.AcceptanceFailure, match="unavailable"):
        runner.validate_classification(row)
    classification = {
        "status": "completed",
        "input_user_turns": 1,
        "evaluated_user_turns": 1,
        "assessments": [{"status": "completed", "error": None, "coverage": {"truncated": False}}],
    }
    row["state"]["classification"] = classification
    runner.validate_classification(row)
    classification["assessments"][0]["coverage"]["truncated"] = True
    with pytest.raises(runner.AcceptanceFailure, match="truncated"):
        runner.validate_classification(row)
    classification["evaluated_user_turns"] = 0
    with pytest.raises(runner.AcceptanceFailure, match="every patient prefix"):
        runner.validate_classification(row)


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


async def test_full_cohort_uses_all_six_clinicians_and_checks_21_messages(tmp_path):
    args = SimpleNamespace(
        output=str(tmp_path),
        insecure=False,
        ca_file=None,
        base_url="https://example.test",
        run_label="cohort",
        gateway_image="gateway",
        workshop_image="workshop",
        full_dialogue_all_clinicians=True,
        full_dialogue_turns=10,
        timeout_seconds=10,
        poll_seconds=0,
    )
    teams = [{"label": f"team-{i}", "token": f"private-{i}"} for i in range(10)]
    rehearsal = runner.Rehearsal(args, teams, "denied")
    rehearsal.profiles = [{"id": f"profile-{i:03}"} for i in range(50)]
    rehearsal.patient = "patient"
    rehearsal.clinicians = [f"clinician-{i}" for i in range(6)]
    rehearsal.catalog = {"judge_model": "judge"}
    rows = []
    for i in range(6):
        row = make_run()
        row.update(id=f"run-{i}", status="completed")
        row["state"]["transcript"] = [
            {"role": "patient" if turn % 2 == 0 else "clinician", "content": "fixture"} for turn in range(21)
        ]
        row["state"]["classification"] = {
            "status": "completed",
            "input_user_turns": 11,
            "evaluated_user_turns": 11,
            "assessments": [{"status": "completed", "coverage": {"truncated": False}} for _ in range(11)],
        }
        rows.append(row)

    async def request(method, path, **kwargs):
        if method == "POST":
            assert kwargs["body"]["clinician_models"] == rehearsal.clinicians
            assert kwargs["body"]["max_turns"] == 10
            return {"data": rows}, 202
        if path.endswith("/events"):
            return {"data": []}, 200
        row = next(row for row in rows if row["id"] in path)
        return {"run": row} if path.endswith("/report") else row, 200

    rehearsal.request = request
    try:
        await rehearsal.full_dialogue()
        assert rehearsal.summary["full_dialogue"]["passed"]
        assert len(rehearsal.summary["full_dialogue"]["run_ids"]) == 6
        assert len(json.loads((tmp_path / "full-dialogue.json").read_text())["runs"]) == 6
    finally:
        await rehearsal.client.aclose()


@pytest.fixture
async def report_reader(tmp_path):
    args = SimpleNamespace(
        output=str(tmp_path),
        insecure=False,
        ca_file=None,
        base_url="https://fixture.test",
        run_label="read-only",
        gateway_image="gateway",
        workshop_image="workshop",
    )
    teams = [{"label": f"team-{i}", "token": f"fixture-private-token-{i}"} for i in range(10)]
    rehearsal = runner.Rehearsal(args, teams, None)
    yield rehearsal
    await rehearsal.client.aclose()


async def test_parallel_reports_are_read_only_bounded_and_in_input_order(report_reader):
    active, peak, finished, methods = 0, 0, [], []

    async def respond(request):
        nonlocal active, peak
        methods.append(request.method)
        run_id = request.url.path.split("/")[4]
        index = int(run_id.removeprefix("run-"))
        assert request.headers["Authorization"] == f"Bearer fixture-private-token-{index % 10}"
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001 * (6 - index % 6))
            if request.url.path.endswith("/events"):
                finished.append(run_id)
                return httpx.Response(404 if index == 1 else 200, json={"data": []})
            row = make_run()
            row["id"] = run_id
            return httpx.Response(200, json={"run": row} if request.url.path.endswith("/report") else row)
        finally:
            active -= 1

    await report_reader.client.aclose()
    report_reader.client = httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(respond))
    jobs = [(i % 10, f"run-{i}") for i in range(12)]
    results = await report_reader.collect_run_reports(jobs)
    assert peak == 5 and active == 0
    assert len(methods) == 36 and set(methods) == {"GET"}
    assert [(item["team"], item["run_id"]) for item in results] == jobs
    assert finished != [run_id for _, run_id in jobs]
    assert results[1]["gateway_events"] is None
    assert report_reader.summary["tls_verification"] is True


async def test_parallel_reports_reject_another_runs_identity(report_reader):
    async def respond(request):
        return httpx.Response(
            200, json={"run": {"id": "wrong"}} if request.url.path.endswith("/report") else {"id": "mine"}
        )

    await report_reader.client.aclose()
    report_reader.client = httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(respond))
    with pytest.raises(runner.AcceptanceFailure, match="another run"):
        await report_reader.collect_run_reports([(0, "mine")])


async def test_parallel_reports_keep_credential_filtering(report_reader):
    async def respond(request):
        return httpx.Response(200, json={"id": "mine", "accidental": report_reader.teams[0]["token"]})

    await report_reader.client.aclose()
    report_reader.client = httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(respond))
    with pytest.raises(runner.AcceptanceFailure, match="exposed credential"):
        await report_reader.collect_run_reports([(0, "mine")])


async def test_parallel_reports_preserve_invalid_classifier_result(report_reader):
    row = make_run()
    row["state"]["transcript"] = [{"role": "patient"}]
    row["state"]["classification"] = {"status": "unavailable", "error": "fixture failure"}

    async def respond(request):
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"run": row} if request.url.path.endswith("/report") else row)

    await report_reader.client.aclose()
    report_reader.client = httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(respond))
    result = (await report_reader.collect_run_reports([(0, row["id"])]))[0]
    assert result["report"]["run"]["state"]["classification"] == row["state"]["classification"]
    with pytest.raises(runner.AcceptanceFailure, match="unavailable"):
        runner.validate_classification(result["row"])


async def test_parallel_report_cancellation_cleans_up_readers(report_reader):
    active = 0
    started = asyncio.Event()

    async def respond(request):
        nonlocal active
        active += 1
        if active == 5:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    await report_reader.client.aclose()
    report_reader.client = httpx.AsyncClient(base_url="https://fixture.test", transport=httpx.MockTransport(respond))
    task = asyncio.create_task(report_reader.collect_run_reports([(0, f"run-{i}") for i in range(12)]))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0

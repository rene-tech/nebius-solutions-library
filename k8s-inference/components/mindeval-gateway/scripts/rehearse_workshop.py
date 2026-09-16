#!/usr/bin/env python3
"""Exercise the public customer API with ordinary team credentials.

Run only after the coordinator declares the deployed images stable. Reports
contain no tokens; credentials are read from a protected local JSON file. This
script cannot attest Kubernetes image digests through a public API that does not
expose them, so image provenance is supplied and verified by the coordinator.
"""

import argparse
import asyncio
import json
import math
import ssl
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import httpx

CRITERIA = {
    "Clinical Accuracy & Competence",
    "Ethical & Professional Conduct",
    "Assessment & Response",
    "Therapeutic Relationship & Alliance",
    "AI-Specific Communication Quality",
}
TERMINAL = {"completed", "failed", "aborted"}


class AcceptanceFailure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def validate_completed(row, judge, turns=2):
    judgment = row["state"].get("judgment") or {}
    scores = judgment.get("judgment", {})
    check(
        set(scores) == CRITERIA
        and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 1 <= v <= 6
            for v in scores.values()
        ),
        f"invalid five-axis judgment: {row['id']}",
    )
    check(judgment["model"] == judge, "run used a different judge")
    check(row["state"].get("benchmark_eligible") is True, "unintervened canonical run is not eligible")
    check(len(row["state"]["transcript"]) == 1 + 2 * turns, "canonical transcript has wrong length")


def validate_classification(row):
    classification = row["state"].get("classification") or {}
    expected = sum(turn["role"] == "patient" for turn in row["state"]["transcript"])
    check(classification.get("status") == "completed", "classifier assessment is unavailable or incomplete")
    check(
        classification.get("input_user_turns") == expected
        and classification.get("evaluated_user_turns") == expected
        and len(classification.get("assessments", [])) == expected,
        "classifier did not assess every patient prefix",
    )
    check(
        all(
            item.get("status") == "completed"
            and not item.get("error")
            and item.get("coverage", {}).get("truncated") is False
            for item in classification["assessments"]
        ),
        "classifier prefix failed or was truncated",
    )


def read_credentials(path, denied_path=None):
    data = json.loads(Path(path).read_text())
    teams = data if isinstance(data, list) else data["teams"]
    normalized = []
    for i, item in enumerate(teams):
        if isinstance(item, str):
            item = {"token": item}
        token = item.get("token") or item.get("plaintext_token") or item.get("api_key")
        check(isinstance(token, str) and token, "credential entries need a token string")
        normalized.append({"label": item.get("label", f"team-{i + 1:02d}"), "token": token})
    denied = data.get("denied_token") if isinstance(data, dict) else None
    if denied_path:
        raw = Path(denied_path).read_text().strip()
        try:
            value = json.loads(raw)
            denied = value if isinstance(value, str) else value.get("token") or value.get("plaintext_token")
        except ValueError:
            denied = raw
    check(len(normalized) == 10, "exactly ten ordinary team credentials are required")
    check(len({item["token"] for item in normalized}) == 10, "team credentials must be distinct")
    return normalized, denied


class Rehearsal:
    def __init__(self, args, teams, denied):
        self.args, self.teams, self.denied = args, teams, denied
        self.output = Path(args.output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.secrets = [team["token"] for team in teams] + ([denied] if denied else [])
        self.verify = False if args.insecure else ssl.create_default_context(cafile=args.ca_file)
        self.client = httpx.AsyncClient(base_url=args.base_url.rstrip("/"), verify=self.verify, timeout=90)
        self.summary = {
            "schema": "fs2-mindeval-public-rehearsal/v1",
            "label": args.run_label,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "base_url": args.base_url,
            "image_provenance": {
                "gateway": args.gateway_image,
                "workshop": args.workshop_image,
                "verified_by_public_runner": False,
            },
            "checks": [],
            "repetitions": [],
            "passed": False,
        }

    def save(self, filename, data):
        text = json.dumps(data, ensure_ascii=False, indent=2)
        check(
            not any(secret and secret in text for secret in self.secrets),
            "credential detected in response/report; not saved",
        )
        (self.output / filename).write_text(text + "\n")

    def record(self, name, passed=True, **details):
        self.summary["checks"].append({"name": name, "passed": passed, **details})
        self.save("summary.json", self.summary)

    async def request(self, method, path, *, team=0, token=None, body=None, key=None, expected=(200,), client=None):
        headers = {"Authorization": f"Bearer {token or self.teams[team]['token']}"}
        if key:
            headers["Idempotency-Key"] = key
        response = await (client or self.client).request(method, path, headers=headers, json=body)
        try:
            data = response.json()
        except ValueError:
            data = {"non_json_response": response.text[:500]}
        rendered = json.dumps(data)
        check(not any(secret and secret in rendered for secret in self.secrets), "server exposed credential bytes")
        if response.status_code not in expected:
            raise AcceptanceFailure(
                json.dumps({"method": method, "path": path, "status": response.status_code, "response": data})
            )
        return data, response.status_code

    async def preflight(self):
        response = await self.client.get("/v1/workshop/catalog")
        check(response.status_code in (401, 403), "workshop catalog accepted no bearer credential")
        self.record("unauthenticated_denied", status=response.status_code)
        catalogs = await asyncio.gather(*(self.request("GET", "/v1/workshop/catalog", team=i) for i in range(10)))
        identities = [result[0]["identity"] for result in catalogs]
        check(
            len({(i["tenant_id"], i["principal_id"]) for i in identities}) == 10,
            "credentials do not represent ten distinct team principals",
        )
        check(
            len({i["tenant_id"] for i in identities}) == 1, "this rehearsal expects ten principals sharing one tenant"
        )
        self.record("ten_principals_same_tenant", identities=identities)
        self.catalog = catalogs[0][0]["catalog"]
        self.profiles = catalogs[0][0]["profiles"]["data"]
        self.clinicians = sorted(model["id"] for model in self.catalog["data"] if model.get("clinician_eligible"))
        patients = [model["id"] for model in self.catalog["data"] if model.get("patient_eligible")]
        check(len(self.clinicians) == 6, f"expected six eligible clinicians, got {len(self.clinicians)}")
        check(patients, "catalog has no eligible patient")
        self.patient = patients[0]
        for catalog, _ in catalogs:
            check(catalog["limits"]["workers_per_team"] == 5, "team key must permit five workers for load acceptance")
            check(catalog["catalog"]["judge_model"] == self.catalog["judge_model"], "judge differs between teams")
        self.save("catalog.json", catalogs[0][0])
        self.record(
            "model_grants_and_fixed_judge",
            clinicians=self.clinicians,
            patient=self.patient,
            judge=self.catalog["judge_model"],
        )
        registration = {
            "profile_ids": [p["id"] for p in self.profiles[:20]],
            "patient_model": self.patient,
            "clinician_models": self.clinicians,
        }
        path = f"/v1/mindeval/runs/{self.args.run_label}-profile-limit/register"
        twenty, _ = await self.request("POST", path, body=registration)
        check(len(twenty["profile_ids"]) == 20, "gateway did not accept20 profile registration")
        registration["profile_ids"] = [p["id"] for p in self.profiles[:21]]
        _, code = await self.request("POST", path, body=registration, expected=(422,))
        self.record("gateway_20_accepted_21_rejected", status=code, inference_calls=0)
        _, code = await self.request(
            "POST",
            "/v1/workshop/runs",
            body={**registration, "max_turns": 2},
            key=self.args.run_label + "-invalid21",
            expected=(422,),
        )
        self.record("workshop_21_profiles_rejected_without_jobs", status=code)
        if self.denied:
            registration["profile_ids"] = [self.profiles[0]["id"]]
            _, code = await self.request(
                "POST",
                f"/v1/mindeval/runs/{self.args.run_label}-profile-limit-denied/register",
                token=self.denied,
                body=registration,
                expected=(403,),
            )
            self.record("ordinary_pat_missing_model_grant_denied", status=code)
        else:
            self.record("ordinary_pat_missing_model_grant_denied", passed=False, reason="no denied_token provided")

    async def controls(self):
        saved = self.output / "controls.json"
        if saved.exists():
            prior = json.loads(saved.read_text())
            current, _ = await self.request("GET", f"/v1/workshop/runs/{prior['run_id']}")
            check(current["status"] == "aborted", "previous control run is no longer aborted")
            self.record("control_run_replay", run_id=prior["run_id"])
            return
        body = {
            "profile_ids": [self.profiles[0]["id"]],
            "patient_model": self.patient,
            "clinician_models": [self.patient],
            "mode": "canonical",
            "max_turns": 30,
            "max_completion_tokens": 1024,
        }
        created, _ = await self.request(
            "POST", "/v1/workshop/runs", body=body, key=self.args.run_label + "-controls", expected=(202,)
        )
        run_id = created["data"][0]["id"]
        path = f"/v1/workshop/runs/{run_id}"

        async def intervene(action, **kwargs):
            return (await self.request("POST", path + "/interventions", body={"action": action, **kwargs}))[0]

        paused = await intervene("pause")
        check(paused["status"] == "paused", "pause did not persist paused state")
        async with httpx.AsyncClient(base_url=self.args.base_url, verify=self.verify, timeout=90) as fresh:
            restored, _ = await self.request("GET", path, client=fresh)
        check(
            restored["status"] == "paused" and restored["state"]["transcript"] == paused["state"]["transcript"],
            "reconnect did not recover the paused transcript",
        )
        role = paused["state"]["next_role"]
        takeover = await intervene("takeover", role=role)
        check(takeover["status"] == "takeover", "takeover did not stop the requested role")
        spoken = await intervene("say", role=role, text="Let's take this one step at a time.", source="typed")
        check(spoken["state"]["transcript"][-1].get("human") is True, "human takeover turn was not recorded")
        await intervene("pause")
        nudged = await intervene(
            "nudge", role="clinician", text="Ask one question at a time and keep the response brief."
        )
        check(nudged["state"]["pending_nudges"], "nudge was not retained")
        await intervene("resume")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            current, _ = await self.request("GET", path)
            if len(current["state"]["transcript"]) > len(spoken["state"]["transcript"]):
                break
            check(
                current["status"] not in {"failed", "aborted"}, f"control run stopped: {current['state'].get('error')}"
            )
            await asyncio.sleep(2)
        else:
            raise AcceptanceFailure("control run did not advance after resume")
        aborted = await intervene("abort")
        check(aborted["status"] == "aborted", "abort did not persist terminal state")
        await asyncio.sleep(2)
        restored, _ = await self.request("GET", path)
        check(
            restored["status"] == "aborted" and restored["state"]["transcript"] == aborted["state"]["transcript"],
            "a late provider reply changed an aborted transcript",
        )
        check(
            restored["state"]["intervened"] and not restored["state"].get("benchmark_eligible"),
            "intervened run was reported benchmark-eligible",
        )
        events, _ = await self.request("GET", path + "/events")
        kinds = {e["kind"] for e in events["data"]}
        check(
            all("intervention." + name in kinds for name in ("pause", "takeover", "say", "nudge", "resume", "abort")),
            "intervention audit trail is incomplete",
        )
        report, _ = await self.request("GET", path + "/report")
        self.save("controls.json", {"run_id": run_id, "report": report, "passed": True})
        self.record("pause_takeover_human_turn_nudge_resume_abort_reconnect", run_id=run_id)

    async def repetition(self, number):
        started = time.monotonic()
        state = {"number": number, "run_ids": {}, "samples": [], "first_progress_seconds": {}, "passed": False}
        self.summary["repetitions"].append(state)

        async def submit(team):
            before, _ = await self.request("GET", "/v1/workshop/runs", team=team)
            before_ids = {row["id"] for row in before["data"]}
            profile = self.profiles[(team + 10 * (number - 1)) % len(self.profiles)]["id"]
            body = {
                "profile_ids": [profile],
                "patient_model": self.patient,
                "clinician_models": self.clinicians,
                "mode": "canonical",
                "max_turns": 2,
                "max_completion_tokens": 4096,
            }
            key = f"{self.args.run_label}-r{number}-team{team}"
            created, _ = await self.request("POST", "/v1/workshop/runs", team=team, body=body, key=key, expected=(202,))
            replayed, _ = await self.request(
                "POST", "/v1/workshop/runs", team=team, body=body, key=key, expected=(202,)
            )
            ids = sorted(run["id"] for run in created["data"])
            check(
                len(ids) == 6 and ids == sorted(run["id"] for run in replayed["data"]),
                "idempotent create added or changed jobs",
            )
            after, _ = await self.request("GET", "/v1/workshop/runs", team=team)
            check(
                {row["id"] for row in after["data"]} - before_ids <= set(ids),
                "idempotent create left additional hidden jobs in the owner's listing",
            )
            state["run_ids"][str(team)] = ids

        await asyncio.gather(*(submit(team) for team in range(10)))
        check(len({i for ids in state["run_ids"].values() for i in ids}) == 60, "teams did not receive60 distinct runs")
        self.record(
            f"repetition_{number}_60_jobs_idempotent",
            unique_runs=len({i for ids in state["run_ids"].values() for i in ids}),
        )
        for suffix in ("", "/events", "/report"):
            _, code = await self.request(
                "GET", f"/v1/workshop/runs/{state['run_ids']['0'][0]}{suffix}", team=1, expected=(404,)
            )
            self.record(f"repetition_{number}_cross_team_denied_{suffix or 'detail'}", status=code)
        active, final = True, {}
        deadline = time.monotonic() + self.args.timeout_seconds
        while active and time.monotonic() < deadline:
            active = False
            counts, running_by_team = Counter(), {}
            for team in range(10):
                listed, _ = await self.request("GET", "/v1/workshop/runs", team=team)
                rows = {row["id"]: row for row in listed["data"]}
                ids = state["run_ids"][str(team)]
                check(set(ids) <= rows.keys(), "a submitted run is missing from its owner's listing")
                selected = [rows[run_id] for run_id in ids]
                running_by_team[str(team)] = sum(row["status"] == "running" for row in selected)
                check(running_by_team[str(team)] <= 5, "team exceeded five concurrent running jobs")
                for row in selected:
                    counts[row["status"]] += 1
                    if row.get("version", 0) > 0:
                        state["first_progress_seconds"].setdefault(str(team), round(time.monotonic() - started, 3))
                    if row["status"] in TERMINAL:
                        final[row["id"]] = row
                    else:
                        active = True
            sample = {
                "seconds": round(time.monotonic() - started, 3),
                "statuses": dict(counts),
                "running_by_team": running_by_team,
            }
            state["samples"].append(sample)
            self.save("summary.json", self.summary)
            print(json.dumps({"repetition": number, **sample}), flush=True)
            if active:
                await asyncio.sleep(self.args.poll_seconds)
        check(not active, f"repetition{number} timed out with nonterminal runs; see samples")
        check(len(final) == 60, f"expected60 terminal runs, got{len(final)}")
        all_results, failures, telemetry, coverage = [], [], [], Counter()
        for team in range(10):
            for run_id in state["run_ids"][str(team)]:
                row, _ = await self.request("GET", f"/v1/workshop/runs/{run_id}", team=team)
                report, _ = await self.request("GET", f"/v1/workshop/runs/{run_id}/report", team=team)
                check(report["run"]["id"] == run_id, "report has another run's data")
                if row["status"] != "completed":
                    failures.append({"run_id": run_id, "status": row["status"], "error": row["state"].get("error")})
                else:
                    judgment = row["state"].get("judgment") or {}
                    try:
                        validate_completed(row, self.catalog["judge_model"])
                        validate_classification(row)
                    except (AcceptanceFailure, KeyError, TypeError) as exc:
                        failures.append({"run_id": run_id, "status": row["status"], "error": str(exc)})
                    for turn in row["state"]["transcript"]:
                        if turn.get("completion"):
                            telemetry.append(turn["completion"])
                    telemetry.append(judgment)
                classification = row["state"].get("classification")
                coverage["missing" if classification is None else classification.get("status", "observed")] += 1
                gateway_events, status = await self.request(
                    "GET", f"/v1/mindeval/runs/{run_id}/events", team=team, expected=(200, 404)
                )
                all_results.append(
                    {
                        "team": self.teams[team]["label"],
                        "report": report,
                        "gateway_events": gateway_events if status == 200 else None,
                    }
                )
        self.save(f"repetition-{number}-reports.json", all_results)
        state["failures"] = failures
        state["classifier_coverage"] = dict(coverage)
        state["usage"] = {
            key: sum(item.get("usage", {}).get(key, 0) or 0 for item in telemetry)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        state["inference"] = {
            "calls": len(telemetry),
            "mean_queue_ms": mean(item["telemetry"]["queue_ms"] for item in telemetry) if telemetry else None,
            "mean_latency_ms": mean(item["telemetry"]["latency_ms"] for item in telemetry) if telemetry else None,
            "retries": sum(item["telemetry"]["retries"] for item in telemetry),
        }
        state["elapsed_seconds"] = round(time.monotonic() - started, 3)
        check(not failures, f"repetition{number} has{len(failures)} failed/aborted runs; see reports")
        check(len(state["first_progress_seconds"]) == 10, "at least one team never made queue progress")
        check(not coverage.get("missing"), "classifier coverage is silently missing")
        state["passed"] = True
        self.record(
            f"repetition_{number}_60_completed_strict_judgments_fair_progress", classifier_coverage=dict(coverage)
        )

    async def run(self):
        try:
            await self.preflight()
            if self.args.repetitions:
                await self.controls()
            for repetition in range(1, self.args.repetitions + 1):
                await self.repetition(repetition)
            if self.args.full_dialogue_turns:
                await self.full_dialogue()
            self.summary["passed"] = all(check["passed"] for check in self.summary["checks"])
        except Exception as exc:
            message = str(exc)
            for secret in self.secrets:
                message = message.replace(secret, "[REDACTED]")
            self.summary["failure"] = {"type": type(exc).__name__, "message": message}
        finally:
            self.summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.save("summary.json", self.summary)
            await self.client.aclose()
        print(
            json.dumps(
                {"passed": self.summary["passed"], "output": str(self.output), "failure": self.summary.get("failure")}
            ),
            flush=True,
        )
        return self.summary["passed"]

    async def full_dialogue(self):
        started = time.monotonic()
        created, _ = await self.request(
            "POST",
            "/v1/workshop/runs",
            key=self.args.run_label + "-full-dialogue",
            expected=(202,),
            body={
                "profile_ids": [self.profiles[20]["id"]],
                "patient_model": self.patient,
                "clinician_models": self.clinicians if self.args.full_dialogue_all_clinicians else [self.patient],
                "mode": "canonical",
                "max_turns": self.args.full_dialogue_turns,
                "max_completion_tokens": 4096,
            },
        )
        run_ids = [run["id"] for run in created["data"]]
        check(len(run_ids) == (6 if self.args.full_dialogue_all_clinicians else 1), "full cohort has wrong job count")
        self.summary["full_dialogue"] = {"run_ids": run_ids, "turns": self.args.full_dialogue_turns}
        self.save("summary.json", self.summary)
        deadline = time.monotonic() + self.args.timeout_seconds
        while time.monotonic() < deadline:
            rows = [(await self.request("GET", f"/v1/workshop/runs/{run_id}"))[0] for run_id in run_ids]
            print(
                json.dumps(
                    {
                        "full_cohort": [
                            {"id": row["id"], "status": row["status"], "messages": len(row["state"]["transcript"])}
                            for row in rows
                        ]
                    }
                ),
                flush=True,
            )
            if all(row["status"] in TERMINAL for row in rows):
                break
            await asyncio.sleep(self.args.poll_seconds)
        reports, failures, telemetry = [], [], []
        for row in rows:
            run_id = row["id"]
            report, _ = await self.request("GET", f"/v1/workshop/runs/{run_id}/report")
            gateway_events, _ = await self.request("GET", f"/v1/mindeval/runs/{run_id}/events", expected=(200, 404))
            reports.append({"report": report, "gateway_events": gateway_events})
            try:
                check(row["status"] == "completed", f"full dialogue {row['status']}: {row['state'].get('error')}")
                validate_completed(row, self.catalog["judge_model"], self.args.full_dialogue_turns)
                validate_classification(row)
            except (AcceptanceFailure, KeyError, TypeError) as exc:
                failures.append({"run_id": run_id, "error": str(exc)})
            telemetry.extend(turn["completion"] for turn in row["state"]["transcript"] if turn.get("completion"))
            if row["state"].get("judgment"):
                telemetry.append(row["state"]["judgment"])
        self.save("full-dialogue.json", reports[0] if len(reports) == 1 else {"runs": reports})
        self.summary["full_dialogue"].update(
            {
                "passed": not failures,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "failures": failures,
                "inference_calls": len(telemetry),
                "usage": {
                    key: sum(item.get("usage", {}).get(key, 0) or 0 for item in telemetry)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                },
                "classifications": [row["state"].get("classification") for row in rows],
            }
        )
        check(not failures, f"full dialogue cohort has {len(failures)} failures; see full-dialogue.json")
        self.record("full_canonical_dialogue_complete", run_ids=run_ids, rounds=self.args.full_dialogue_turns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--keys-file", required=True)
    parser.add_argument("--denied-key-file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--workshop-image", required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument(
        "--full-dialogue-turns",
        type=int,
        default=0,
        help="Also run one full canonical dialogue; use --repetitions 0 for only this diagnostic",
    )
    parser.add_argument(
        "--full-dialogue-all-clinicians",
        action="store_true",
        help="Use all six eligible clinicians in the full dialogue cohort",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--ca-file")
    parser.add_argument(
        "--insecure", action="store_true", help="Explicitly accept the rehearsal endpoint's self-signed TLS certificate"
    )
    args = parser.parse_args()
    check(
        args.repetitions >= 0 and args.full_dialogue_turns >= 0 and args.repetitions + args.full_dialogue_turns > 0,
        "request at least one rehearsal or full dialogue",
    )
    teams, denied = read_credentials(args.keys_file, args.denied_key_file)
    raise SystemExit(0 if asyncio.run(Rehearsal(args, teams, denied).run()) else 1)


if __name__ == "__main__":
    main()

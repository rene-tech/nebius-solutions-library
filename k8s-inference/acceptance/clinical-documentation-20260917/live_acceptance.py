"""Full public research-recording acceptance; temporary ordinary key, revoked."""

import argparse
import base64
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "integrations/librechat/skills/clinical-documentation/scripts"
sys.path.insert(0, str(SCRIPTS))
from clinical_report import parser, run, save


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--assets", type=Path, required=True)
    cli.add_argument("--kubeconfig", required=True)
    cli.add_argument("--context", required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--origin", default="https://89.169.99.188")
    cli.add_argument("--report-model", default="qwen3-8b")
    cli.add_argument("--report-provider")
    cli.add_argument("--transcripts-from", type=Path, help="Reuse retained transcripts for a model comparison; no new ASR")
    cli.add_argument("--all-cases", action="store_true", help="Two complete English and three complete German recordings")
    args = cli.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = json.loads(subprocess.check_output([
        "kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context,
        "-n", "fs2-system", "get", "secret", "fs2-serve-admin", "-o", "json"]))
    bootstrap = base64.b64decode(source["data"]["token"]).decode().strip()
    receipt = {"started_at": datetime.now(UTC).isoformat(), "measurements": [],
               "clinical_validation": False, "key_revoked": False}
    key_id = None
    with httpx.Client(base_url=args.origin, headers={"origin": args.origin}, timeout=60, trust_env=False) as admin:
        try:
            response = admin.post("/admin/api/v1/session", headers={"authorization": "Bearer " + bootstrap})
            response.raise_for_status()
            response = admin.post("/admin/api/v1/keys", json={
                "name": "clinical-doc-acceptance-" + uuid4().hex[:10], "tenant_id": "rene", "principal_id": "rene",
                "models": ["qwen3-8b", "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b"],
                "scopes": ["catalog.read", "inference.invoke", "mcp.invoke", "operations.read",
                           "operations.result", "artifacts.write", "use.nonclinical"],
                "max_concurrency": 2, "expires_at": (datetime.now(UTC) + timedelta(hours=4)).isoformat()})
            response.raise_for_status()
            disclosed = response.json()["data"]
            key_id, key = disclosed["key"]["id"], disclosed["secret"]
            receipt["temporary_key_id"] = key_id
            cases = [("en-01", "en", "ready/en/day1_consultation01_conversation.wav"),
                     ("de-herzrasen", "de", "ready/de/hhu-herzrasen.wav")]
            if args.all_cases:
                cases += [("en-02", "en", "ready/en/day1_consultation02_conversation.wav"),
                          ("de-grippaler-infekt", "de", "ready/de/hhu-grippaler-infekt.wav"),
                          ("de-polyarthritis", "de", "ready/de/hhu-polyarthritis.wav")]

            def one(case):
                name, language, relative = case
                source = (["--transcript", str(args.transcripts_from / name / "transcript.txt")]
                          if args.transcripts_from else ["--audio", str(args.assets / relative)])
                command = source + ["--language", language,
                           "--base-url", args.origin, "--output", str(args.output / name),
                           "--report-model", args.report_model]
                if args.report_provider:
                    command += ["--report-provider", args.report_provider]
                options = parser().parse_args(command)
                start = time.monotonic()
                row = {"case": name, "language": language, "source": relative}
                try:
                    provider_key = None
                    if args.report_provider:
                        provider_key = (Path.home() / ".config/nebius-token-factory/api-key").read_text().strip()
                    doc = run(options, key=key, provider_key=provider_key)
                    row.update(status="completed_draft", facts=len(doc["facts"]),
                               excluded=len(doc["rejected"]), uncertainties=len(doc["uncertainties"]))
                    replay = run(options, key=key, provider_key=provider_key)
                    row["completed_replay_identical"] = replay == doc
                except (httpx.HTTPError, OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
                    row.update(status="incomplete", error=type(exc).__name__,
                               detail=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "see saved call receipt")
                row["wall_seconds"] = time.monotonic() - start
                print(json.dumps(row), flush=True)
                return row

            with ThreadPoolExecutor(max_workers=2) as pool:
                receipt["measurements"] = list(pool.map(one, cases))
        finally:
            if key_id:
                response = admin.delete("/admin/api/v1/keys/" + key_id)
                receipt["key_revoked"] = response.status_code == 200
            save(args.output / "acceptance.json", receipt)
    if not receipt["key_revoked"] or any(r["status"] != "completed_draft" for r in receipt["measurements"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

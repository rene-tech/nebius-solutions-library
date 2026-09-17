"""Full 20-profile, ten-round qualification of the two additional Porto clinicians.

This is provider contract/workflow evidence, not the separate LibreChat ten-team
acceptance. Patient, prompts and pilot judge are identical to the existing service.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from fs2_mindeval.adapter import TokenFactoryAdapter, parse_judgment
from fs2_mindeval.contracts import GatewayError
from fs2_mindeval.prompts import PROVENANCE, judge_messages, profile_detail
from fs2_mindeval.scheduler import FairScheduler

MODELS = ["nvidia/nemotron-3-super-120b-a12b", "zai-org/GLM-5.2"]
PATIENT = "Qwen/Qwen3-30B-A3B-Instruct-2507"
JUDGE = "google/gemma-3-27b-it"


def save(path, body):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


async def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--models", nargs="+", choices=MODELS, default=MODELS)
    args = cli.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scheduler = FairScheduler()
    key = (Path.home() / ".config/nebius-token-factory/api-key").read_text().strip()
    adapter = TokenFactoryAdapter(key, scheduler)
    save(args.output / "catalog.json", await adapter.discover())
    semaphore = asyncio.Semaphore(5)

    async def one(model, index):
        target = args.output / f"{model.rsplit('/', 1)[1]}-{index:03}.json"
        if target.exists():
            return json.loads(target.read_text())
        async with semaphore:
            profile_id = f"profile-{index:03}"
            profile = profile_detail(profile_id)
            row = {"model": model, "patient_model": PATIENT, "judge_model": JUDGE,
                   "profile_id": profile_id, "rounds": 10, "provenance": PROVENANCE,
                   "transcript": [{"role": "patient", "content": "Hello", "seed": True}],
                   "calls": [], "status": "running"}
            started = time.monotonic()
            try:
                for _round in range(10):
                    for role in ("clinician", "patient"):
                        messages = [{"role": "system", "content": profile[f"{role}_system_prompt"]}] + [
                            {"role": "assistant" if turn["role"] == role else "user", "content": turn["content"]}
                            for turn in row["transcript"]]
                        result = await adapter.complete(team="porto-expanded", model=model if role == "clinician" else PATIENT,
                                                        messages=messages, max_completion_tokens=4096, temperature=0.7)
                        row["transcript"].append({"role": role, "content": result["content"]})
                        row["calls"].append({"role": role, "usage": result["usage"], "telemetry": result["telemetry"],
                                             "finish_reason": result["finish_reason"]})
                        save(target, row)
                interaction = [{"role": "assistant" if turn["role"] == "clinician" else "user", "content": turn["content"]}
                               for turn in row["transcript"]]
                result = await adapter.complete(team="porto-expanded", model=JUDGE,
                                                messages=judge_messages(profile["profile"], interaction),
                                                max_completion_tokens=8192, temperature=0)
                row["judgment"] = parse_judgment(result["content"])
                row["calls"].append({"role": "judge", "usage": result["usage"], "telemetry": result["telemetry"]})
                row["status"] = "completed"
            except GatewayError as exc:
                row.update(status="failed", error=exc.code,
                           failure_telemetry={k: v for k, v in exc.telemetry.items() if k not in {"content", "reasoning"}})
            except Exception as exc:
                row.update(status="failed", error=type(exc).__name__)
            row["wall_seconds"] = round(time.monotonic() - started, 3)
            save(target, row)
            print(json.dumps({k: row[k] for k in ("model", "profile_id", "status", "wall_seconds")}), flush=True)
            return row

    try:
        rows = await asyncio.gather(*(one(model, i) for i in range(20) for model in args.models))
        summary = {"runs": len(rows), "completed": sum(r["status"] == "completed" for r in rows),
                   "failed": sum(r["status"] != "completed" for r in rows), "models": args.models,
                   "patient_model": PATIENT, "judge_model": JUDGE, "rounds": 10,
                   "clinical_validation": False, "librechat_acceptance": False}
        save(args.output / "summary.json", summary)
        print(json.dumps(summary), flush=True)
        if summary["failed"]:
            raise SystemExit(1)
    finally:
        await scheduler.close()
        await adapter.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

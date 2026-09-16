"""Real public-catalog contract tests and reproducible human-annotation calibration."""

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from .adapter import TokenFactoryAdapter, parse_judgment
from .contracts import GatewayError
from .prompts import ASSETS, CRITERIA, PROVENANCE, TEMPLATES, judge_messages, profile_detail
from .scheduler import FairScheduler

CLINICIANS = [
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen3-30B-A3B-Instruct-2507",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "openai/gpt-oss-120b",
    "zai-org/GLM-5.1",
    "deepseek-ai/DeepSeek-V4-Pro",
]
CANDIDATES = ["NousResearch/Hermes-4-405B", "google/gemma-3-27b-it"]
HUMAN_FIELDS = (
    ("criterion_1", "criterion_2"),
    ("criterion_3", "criterion_4"),
    ("criterion_6", "criterion_7"),
    ("criterion_8", "criterion_9"),
    ("criterion_10",),
)


def human_scores(row):
    # Appendix C has 2+2+2+2+1 sub-axes. Zero is outside its 1..6 scale and
    # therefore missing, never a fabricated score. criterion11 is undocumented.
    result = {}
    for criterion, fields in zip(CRITERIA, HUMAN_FIELDS):
        subaxes = []
        for field in fields:
            ratings = [
                score for score in row[f"{field}.responses"] if isinstance(score, (int, float)) and 1 <= score <= 6
            ]
            if not ratings:
                raise ValueError(f"no valid human scores for {row['id']} {field}")
            subaxes.append(mean(ratings))
        result[criterion] = mean(subaxes)
    return result


def metrics(rows):
    valid = [row for row in rows if row.get("judgment")]
    if not valid:
        return {"n": len(rows), "valid": 0, "mae": None, "bias": None}
    residuals = [row["judgment"][criterion] - row["human"][criterion] for row in valid for criterion in CRITERIA]
    by_axis = {
        criterion: {
            "mae": mean(abs(row["judgment"][criterion] - row["human"][criterion]) for row in valid),
            "bias": mean(row["judgment"][criterion] - row["human"][criterion] for row in valid),
        }
        for criterion in CRITERIA
    }
    pairs = []
    for i, left in enumerate(valid):
        for right in valid[i + 1 :]:
            if left["profile_fingerprint"] != right["profile_fingerprint"]:
                continue
            predicted = mean(left["judgment"].values()) - mean(right["judgment"].values())
            human = mean(left["human"].values()) - mean(right["human"].values())
            if human:
                pairs.append(0.5 if predicted == 0 else float(predicted * human > 0))
    return {
        "n": len(rows),
        "valid": len(valid),
        "mae": mean(map(abs, residuals)),
        "bias": mean(residuals),
        "rmse": math.sqrt(mean(value**2 for value in residuals)),
        "by_axis": by_axis,
        "within_profile_pairwise_accuracy": mean(pairs) if pairs else None,
        "pairs": len(pairs),
    }


async def run(args):
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    key = os.getenv("NEBIUS_TOKEN_FACTORY_API_KEY") or os.getenv("NEBIUS_API_KEY")
    if not key:
        key = (Path.home() / ".config/nebius-token-factory/api-key").read_text().strip()
    scheduler = FairScheduler()
    adapter = TokenFactoryAdapter(key, scheduler)
    catalog = await adapter.discover()
    if not (output / "catalog.json").exists():
        (output / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
    if args.mode == "contracts":
        profile = profile_detail("profile-000")
        records = []
        for model in list(dict.fromkeys([*CLINICIANS, *CANDIDATES])):
            messages = [
                {"role": "system", "content": profile["clinician_system_prompt"]},
                {"role": "user", "content": "Hello"},
            ]
            if model in {"Qwen/Qwen3-30B-A3B-Instruct-2507", "google/gemma-3-27b-it"}:
                messages = [
                    {"role": "system", "content": profile["patient_system_prompt"]},
                    {"role": "assistant", "content": "Hello"},
                    {"role": "user", "content": "Hi Dennis. How are you doing?"},
                ]
            row = {"model": model, "messages_sha256": hashlib.sha256(json.dumps(messages).encode()).hexdigest()}
            try:
                result = await adapter.complete(
                    team="contract", model=model, messages=messages, max_completion_tokens=8192, temperature=0
                )
                row.update(
                    status="passed",
                    content=result["content"],
                    finish_reason=result["finish_reason"],
                    usage=result["usage"],
                    telemetry=result["telemetry"],
                    reasoning_characters=len(result["reasoning"]),
                )
            except GatewayError as exc:
                row.update(
                    status="failed",
                    error=exc.code,
                    telemetry={k: v for k, v in exc.telemetry.items() if k not in {"content", "reasoning"}},
                )
            records.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "content"}), flush=True)
            (output / "contracts.json").write_text(
                json.dumps({"provenance": PROVENANCE, "data": records}, indent=2) + "\n"
            )
    else:
        annotations = [json.loads(line) for line in (ASSETS / "human_annotations.jsonl").read_text().splitlines()]
        # Entire profiles are grouped across models to prevent profile leakage
        # between selection and validation. Exclude named in-context examples.
        groups = defaultdict(list)
        excluded = []
        for row in annotations:
            name = row["member_attributes"]["name"]
            if f"- Name: {name}\n" in TEMPLATES["judge_v0_1"]:
                excluded.append(row["id"])
            else:
                groups[name].append(row)
        names = sorted(groups, key=lambda name: hashlib.sha256(f"mindeval-calibration-v1:{name}".encode()).hexdigest())
        selection_names = set(names[: len(names) // 2])
        rows = [row for name in names for row in groups[name]]
        output_file = output / "judgments.jsonl"
        existing = [json.loads(line) for line in output_file.read_text().splitlines()] if output_file.exists() else []
        fingerprints = {
            row["id"]: hashlib.sha256(json.dumps(row["member_attributes"], sort_keys=True).encode()).hexdigest()
            for row in annotations
        }
        for record in existing:
            record["profile_fingerprint"] = fingerprints[record["annotation_id"]]
        completed = {(row["candidate"], row["annotation_id"]) for row in existing}
        results = list(existing)
        semaphore = asyncio.Semaphore(8)

        async def evaluate(candidate, row):
            if (candidate, row["id"]) in completed:
                return
            async with semaphore:
                record = {
                    "candidate": candidate,
                    "annotation_id": row["id"],
                    "clinician_model": row["model"],
                    "profile_name": row["member_attributes"]["name"],
                    "profile_fingerprint": fingerprints[row["id"]],
                    "human": human_scores(row),
                    "split": "selection" if row["member_attributes"]["name"] in selection_names else "validation",
                }
                messages = judge_messages(row["member_attributes"], row["messages"])
                record["prompt_sha256"] = hashlib.sha256(json.dumps(messages).encode()).hexdigest()
                try:
                    result = await adapter.complete(
                        team=f"calibration-{candidate}",
                        model=candidate,
                        messages=messages,
                        max_completion_tokens=8192,
                        temperature=0,
                    )
                    record["content"] = result["content"]
                    record.update(
                        judgment=parse_judgment(result["content"]),
                        usage=result["usage"],
                        finish_reason=result["finish_reason"],
                        telemetry=result["telemetry"],
                        content=result["content"],
                    )
                except GatewayError as exc:
                    record.update(
                        error=exc.code,
                        telemetry={k: v for k, v in exc.telemetry.items() if k not in {"content", "reasoning"}},
                    )
                results.append(record)
                with output_file.open("a") as stream:
                    stream.write(json.dumps(record) + "\n")
                print(
                    json.dumps(
                        {
                            "candidate": candidate,
                            "annotation": row["id"],
                            "split": record["split"],
                            "status": record.get("error", "passed"),
                            "completed": len(results),
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(evaluate(candidate, row) for row in rows for candidate in CANDIDATES))
        output_file.write_text("".join(json.dumps(record) + "\n" for record in results))
        summary = {
            candidate: {
                split: metrics([row for row in results if row["candidate"] == candidate and row["split"] == split])
                for split in ("selection", "validation")
            }
            for candidate in CANDIDATES
        }
        eligible = [
            candidate
            for candidate in CANDIDATES
            if summary[candidate]["selection"]["valid"] == summary[candidate]["selection"]["n"]
        ]
        selected = min(eligible, key=lambda candidate: summary[candidate]["selection"]["mae"]) if eligible else None
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "provenance": PROVENANCE,
            "candidates": summary,
            "selected_model": selected,
            "selection_rule": "minimum selection-set MAE with 100% valid judgments",
            "profile_split": {"selection": sorted(selection_names), "validation": sorted(set(names) - selection_names)},
            "excluded_in_context_annotation_ids": excluded,
            "human_field_mapping": dict(zip(CRITERIA, HUMAN_FIELDS)),
            "bias_boundary": "Pilot on published synthetic conversations, not clinical validation. Family exclusion reduces self-preference but does not remove training-data or patient-family bias. No post-hoc score offsets. Human zero ratings excluded as out of scale; undocumented criterion11 excluded.",
        }
        (output / "calibration.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    await scheduler.close()
    await adapter.client.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["contracts", "calibrate"])
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()

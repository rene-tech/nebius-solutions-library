#!/usr/bin/env python3
"""Publish an immutable pack only after every advertised input/recipe has proof.

This is a release-artifact builder, not a runtime authorization control. It does
not run inference, edit old packs, or convert historical failures into passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import run_example as runner


async def coverage(root, reports):
    manifest = json.loads((root / "manifest.json").read_bytes())
    inputs = runner.Inputs(root, manifest, runner.offline_upload)
    evidence = {}
    for path in reports:
        report = json.loads(path.read_bytes())
        for record in report["results"]:
            if record["state"] == "passed":
                evidence[
                    (
                        record["case_id"],
                        record["model_id"],
                        record["recipe_sha256"],
                        record["input_sha256"],
                    )
                ] = record
    selected, missing, models = [], [], set()
    counts = Counter(case["category"] for case in manifest["cases"])
    minimums = {c["id"]: c.get("minimum_cases", 10) for c in manifest["categories"]}
    if (
        set(counts) != set(minimums)
        or any(not isinstance(n, int) or n < 1 for n in minimums.values())
        or any(counts[category] < n for category, n in minimums.items())
    ):
        raise ValueError("category_case_coverage_incomplete")
    for obj in manifest["objects"]:
        inputs.read(obj["path"])
        if not all(
            obj["provenance"].get(k)
            for k in ("source", "license", "attribution", "transformation")
        ):
            raise ValueError("provenance_missing")
    for case in manifest["cases"]:
        recipes = json.loads(inputs.read(case["recipes"]))["recipes"]
        if {r["model_id"] for r in recipes} != set(case["compatible_model_ids"]):
            raise ValueError("case_model_contract_mismatch")
        for recipe in recipes:
            models.add(recipe["model_id"])
            key = (
                case["id"],
                recipe["model_id"],
                runner.sha(runner.encoded(recipe)),
                runner.sha(
                    runner.encoded(await inputs.materialize(recipe["arguments"]))
                ),
            )
            record = evidence.get(key)
            if record:
                selected.append(record)
            else:
                missing.append({"case_id": key[0], "model_id": key[1]})
    if models != set(manifest["live_model_ids"]):
        raise ValueError("live_model_coverage_incomplete")
    return manifest, selected, missing


async def main(args):
    manifest, selected, missing = await coverage(args.pack, args.reports)
    print(
        json.dumps(
            {"qualified_recipes": len(selected), "missing_recipes": missing}, indent=2
        )
    )
    if args.check_only:
        return
    if missing:
        raise ValueError("cannot_publish_unqualified_recipes")
    if args.output.exists():
        raise ValueError("immutable_output_already_exists")
    shutil.copytree(args.pack, args.output)
    objects = {item["path"]: item for item in manifest["objects"]}

    def write(path, text, media="text/markdown"):
        content = text.encode() if isinstance(text, str) else runner.encoded(text)
        target = args.output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        item = objects.get(
            path,
            {
                "path": path,
                "media_type": media,
                "compatible_model_ids": [],
                "recipe_version": "1",
                "provenance": {
                    "source": "Scientific AI starter-data qualification",
                    "license": "Apache-2.0",
                    "attribution": "Nebius Scientific AI",
                    "transformation": "Payload-free measured recipe acceptance and onboarding documentation",
                },
            },
        )
        item.update(
            sha256=runner.sha(content),
            size_bytes=len(content),
            validation_status="release-validated",
        )
        objects[path] = item

    evidence = {
        "schema": "fs2-serve.nebius.ai/starter-data-qualification/v1",
        "at": datetime.now(UTC).isoformat(),
        "draft_manifest_sha256": runner.sha((args.pack / "manifest.json").read_bytes()),
        "source_commit": args.source_commit,
        "scope": "Exact starter input/recipe/output functionality. Not a clinical/scientific efficacy or whole-platform performance qualification.",
        "results": selected,
    }
    write("qualification.json", evidence, "application/json")
    lines = [
        "# Tested example coverage",
        "",
        "These measurements include gateway queueing and runtime startup where applicable. They are one observed run per recipe, not a latency guarantee or isolated GPU benchmark. An occupied cluster can take longer. Cold-start values are reported only when the platform measured them; missing is not zero.",
        "",
        "| Category / case | Model | Input formats | Observed accepted-to-terminal time |",
        "| --- | --- | --- | --- |",
    ]
    for case in manifest["cases"]:
        case["validation_status"] = "recipe-result-validated"
        receipts = [r for r in selected if r["case_id"] == case["id"]]
        timing = [
            "## Observed runs",
            "",
            "| Model | Accepted-to-terminal | Operation |",
            "| --- | --- | --- |",
        ]
        for record in receipts:
            seconds = record.get("server_elapsed_seconds")
            duration = f"{seconds:.1f}s" if seconds is not None else "not reported"
            formats = (
                ", ".join(sorted({objects[p]["media_type"] for p in case["assets"]}))
                or "text/JSON prompt"
            )
            lines.append(
                f"| {case['id']} | {record['model_id']} | {formats} | {duration} |"
            )
            timing.append(
                f"| {record['model_id']} | {duration} | `{record['operation_id']}` |"
            )
        path = case["id"] + "/README.md"
        original = (
            (args.output / path).read_text().split("Timing: not benchmarked yet;")[0]
        )
        write(
            path,
            original
            + "\n"
            + "\n".join(timing)
            + "\n\nTimes include waiting and are not a service-level promise. See the root coverage table and qualification.json for exact checks and lifecycle fields.\n",
        )
    write("COVERAGE.md", "\n".join(lines) + "\n")
    readme = (args.output / "README.md").read_text()
    write(
        "README.md",
        readme
        + "\n## Run an example\n\nUse Python 3.13 and a local virtual environment after downloading this entire prefix. Keep output directories outside the pack.\n\n```sh\npython3.13 -m venv .venv\n. .venv/bin/activate\npip install -r requirements.txt\nexport SCIENTIFIC_MODELS_MCP_URL='https://89.169.99.188/mcp'\n# Set SCIENTIFIC_MODELS_API_KEY through your normal secret mechanism.\npython run-example.py structure/1ubq --model openfold2 --output ../runs/ubiquitin\n```\n\nUse the public MCP URL for your deployment (the URL above is this release's qualification target). A local filesystem path is resolved and uploaded by the runner, not read by the remote model. Rerun the exact command to resume a known operation. Never delete its receipt merely to retry: it prevents duplicate work. Model grants still apply; examples do not grant model access.\n\n[Coverage and observed timing](COVERAGE.md) · [Checks and operation identities](qualification.json). The bundled runner verifies checksums and durable terminal status. The release's semantic checks validate result formats, not scientific truth; review model outputs before research use.\n",
    )
    manifest.update(
        objects=list(objects.values()),
        release_status="qualified",
        qualification={
            "state": "passed",
            "receipts": ["qualification.json"],
            "recipes": len(selected),
            "scope": evidence["scope"],
        },
    )
    (args.output / "manifest.json").write_bytes(runner.encoded(manifest))
    print(
        json.dumps(
            {
                "output": str(args.output),
                "manifest_sha256": runner.sha(
                    (args.output / "manifest.json").read_bytes()
                ),
                "objects": len(objects),
                "object_bytes": sum(o["size_bytes"] for o in objects.values()),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--reports", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not args.check_only and (not args.output or not args.source_commit):
        parser.error("publication needs --output and --source-commit")
    asyncio.run(main(args))

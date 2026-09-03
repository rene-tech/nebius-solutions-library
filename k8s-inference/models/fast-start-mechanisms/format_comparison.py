#!/usr/bin/env python3
"""Render the campaign comparison as Markdown, so the numbers are not retyped.

Reads the receipts a campaign wrote and prints the per-mechanism table plus the
runtime phase breakdown that explains it. Every number comes from a receipt;
nothing here computes a level, and the qualification line states plainly which
cohorts reach the 20-sample rule and which do not.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

MINIMUM_QUALIFYING_SAMPLES = 20
PHASES = (
    ("weight_load_seconds", "weight load"),
    ("compile_seconds", "torch.compile"),
    ("graph_capture_seconds", "graph capture"),
    ("engine_init_seconds", "engine init"),
)


def load(directory: Path) -> list[dict[str, Any]]:
    receipts = []
    for path in sorted(directory.glob("attempt-*.json")):
        receipts.append(json.loads(path.read_text(encoding="utf-8")))
    return receipts


def _p95(cohort: list[dict[str, Any]]) -> float | None:
    """Nearest-rank p95 where a failure ranks after every duration.

    This mirrors fast_start.nearest_rank exactly: if the rank lands on a failed
    attempt the percentile is unavailable rather than optimistic.
    """

    if not cohort:
        return None
    durations = sorted(item["model_start_seconds"] for item in cohort if item["succeeded"])
    rank = max(1, math.ceil(0.95 * len(cohort)))
    return durations[rank - 1] if rank <= len(durations) else None


def _seconds(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.2f}"


def _bytes(value: int | None) -> str:
    if not value:
        return "none"
    return f"{value / 1024**3:.1f} GiB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    arguments = parser.parse_args(argv)
    receipts = load(arguments.receipts)
    if not receipts:
        raise SystemExit(f"no attempt receipts under {arguments.receipts}")

    by_arm: dict[str, list[dict[str, Any]]] = {}
    for receipt in receipts:
        by_arm.setdefault(receipt["arm"], []).append(receipt)

    control = by_arm.get("conventional", [])
    control_p95 = _p95(control)

    print("| Mechanism | n | fail | p50 s | p95 s | vs conventional | Reserved while idle |")
    print("| --- | ---: | ---: | ---: | ---: | --- | --- |")
    for arm in sorted(by_arm):
        cohort = by_arm[arm]
        values = sorted(item["model_start_seconds"] for item in cohort if item["succeeded"])
        failed = sum(1 for item in cohort if not item["succeeded"])
        p95 = _p95(cohort)
        p50 = statistics.median(values) if values else None
        if arm == "conventional" or control_p95 is None or p95 is None:
            comparison = "control" if arm == "conventional" else "unavailable"
        elif p95 > control_p95:
            comparison = f"{p95 - control_p95:.1f} s slower"
        else:
            comparison = f"{control_p95 - p95:.1f} s faster, {control_p95 / p95:.1f}x"
        held = []
        if cohort[0]["reserved_accelerators"]:
            held.append(f"{cohort[0]['reserved_accelerators']} accelerator")
        if cohort[0]["reserved_host_memory_bytes"]:
            held.append(f"{_bytes(cohort[0]['reserved_host_memory_bytes'])} host RAM")
        print(
            f"| `{arm}` | {len(cohort)} | {failed} | {_seconds(p50)} | {_seconds(p95)} "
            f"| {comparison} | {', '.join(held) or 'nothing'} |"
        )

    print()
    print("Runtime phases, median over each cohort's successful attempts:")
    print()
    print("| Mechanism | " + " | ".join(label for _key, label in PHASES) + " |")
    print("| --- | " + " | ".join("---:" for _ in PHASES) + " |")
    for arm in sorted(by_arm):
        cells = []
        for key, _label in PHASES:
            samples = [
                item["runtime_phases"][key]
                for item in by_arm[arm]
                if item["succeeded"] and key in item["runtime_phases"]
            ]
            cells.append(f"{statistics.median(samples):.2f}" if samples else "n/a")
        print(f"| `{arm}` | " + " | ".join(cells) + " |")

    print()
    for arm in sorted(by_arm):
        cohort = by_arm[arm]
        failed = sum(1 for item in cohort if not item["succeeded"])
        enough = len(cohort) >= MINIMUM_QUALIFYING_SAMPLES and failed == 0
        state = "reaches" if enough else "does not reach"
        print(f"- `{arm}`: {len(cohort)} samples, {failed} failed, {state} the 20-sample failure-free rule.")
    print()
    print(
        "Reaching the rule is necessary, not sufficient, and this script never grants a level. "
        "fs2_serve.fast_start.evaluate_fast_start decides one, from evidence bound to the exact "
        "deployment tuple."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

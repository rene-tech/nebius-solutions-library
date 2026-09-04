#!/usr/bin/env python3
"""Aggregate the trial receipts into one campaign summary and level assignment.

A level is only assigned from a cohort in which every trial completed and passed
its semantic gate.  The cohort is reported twice: once over every trial, and
once over the trials whose volume setup was not serialised behind a recursive
fsGroup ownership pass over the shared claim -- the trial's own pass, or a
concurrent trial's pass on the same node and claim.  That pass is a migration
cost paid once per claim rather than a property of the steady-state start path,
so both numbers are published and neither replaces the other.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RECEIPTS = HERE / "receipts"
SUMMARY = HERE / "CAMPAIGN_SUMMARY.json"

# A first step this many times slower than the cohort's fastest means the trial
# compiled rather than reused a cached executable.
COLD_FIRST_STEP_RATIO = 4

PHASES = (
    "capacity_wait_seconds",
    "sandbox_and_volume_setup_seconds",
    "runtime_init_and_artifact_load_seconds",
    "compute_to_first_result_seconds",
    "time_to_first_semantic_result_seconds",
    "model_start_seconds",
    "schedule_to_semantic_complete_seconds",
    "teardown_seconds",
    "end_to_end_seconds",
)


def matrix() -> dict[str, Any]:
    return json.loads((HERE / "campaign_matrix.json").read_text(encoding="utf-8"))


def statistics_for(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 3),
        "mean": round(statistics.fmean(ordered), 3),
        "max": round(ordered[-1], 3),
        # With three trials the 95th percentile is the worst observation; the
        # contract qualifies on p95, so this stays the conservative choice as the
        # cohort grows.
        "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3),
    }


def level_for(seconds: float, thresholds: dict[str, int]) -> str:
    for level in ("L4", "L3", "L2", "L1"):
        if seconds <= thresholds[level]:
            return level
    return "Off"


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return not (a_end <= b_start or a_start >= b_end)


def ownership_contended(receipt: dict[str, Any], receipts: list[dict[str, Any]]) -> list[str]:
    """Name the trials whose recursive ownership walk stalled this trial's mount.

    The kubelet serialises volume setup per node and claim, so one Pod running
    the default fsGroupChangePolicy of Always holds up every other Pod mounting
    the same claim on that node, even a Pod that itself asked for OnRootMismatch.
    Attribution therefore cannot stop at a trial's own events.
    """
    boundaries = receipt["measured"]["boundaries"]
    start, end = boundaries.get("pod_scheduled_at"), boundaries.get("container_started_at")
    if not (start and end):
        return []
    node = receipt["node"].get("node_name_sha256")
    claims = {
        volume["claim"]
        for volume in matrix()["models"][receipt["model_id"]]["volumes"].values()
        if volume.get("claim")
    }
    contenders = []
    for other in receipts:
        if other["trial_id"] == receipt["trial_id"]:
            continue
        if other["node"].get("node_name_sha256") != node:
            continue
        if not other["measured"]["volume_ownership"]["recursive_fsgroup_pass_observed"]:
            continue
        other_claims = {
            volume["claim"]
            for volume in matrix()["models"][other["model_id"]]["volumes"].values()
            if volume.get("claim")
        }
        if not (claims & other_claims):
            continue
        other_bounds = other["measured"]["boundaries"]
        if other_bounds.get("pod_scheduled_at") and other_bounds.get("container_started_at") and overlaps(
            start, end, other_bounds["pod_scheduled_at"], other_bounds["container_started_at"]
        ):
            contenders.append(other["trial_id"])
    return sorted(contenders)


def summarize() -> dict[str, Any]:
    root = matrix()
    thresholds = root["level_contract"]["thresholds_seconds"]
    receipts = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(RECEIPTS.glob("*.json"))]
    for receipt in receipts:
        receipt["_contended_by"] = ownership_contended(receipt, receipts)

    cohorts: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for receipt in receipts:
        cohorts.setdefault(receipt["model_id"], {}).setdefault(receipt["variant"], []).append(receipt)

    models: dict[str, Any] = {}
    for model, variants in sorted(cohorts.items()):
        spec = root["models"][model]
        model_entry: dict[str, Any] = {
            "image": {"reference": spec["image"], "digest": spec["image_digest"], "tag": spec["image_tag"]},
            "namespace": spec.get("namespace", root["cluster"]["namespace"]),
            "kueue_admitted": spec.get("use_kueue", True),
            "artifact_bytes_read_floor": spec["artifact_bytes_read_floor"],
            "variants": {},
        }
        for variant, trials in sorted(variants.items()):
            complete = [t for t in trials if t["job_state"] == "complete"]
            passed = [t for t in complete if t.get("semantic_validation", {}).get("status") == "passed"]
            steady = [
                t for t in passed
                if not t["measured"]["volume_ownership"]["recursive_fsgroup_pass_observed"]
                and not t["_contended_by"]
            ]

            phases = {
                name: statistics_for([
                    t["measured"]["phases_seconds"][name]
                    for t in passed
                    if t["measured"]["phases_seconds"].get(name) is not None
                ])
                for name in PHASES
                if any(t["measured"]["phases_seconds"].get(name) is not None for t in passed)
            }

            entry: dict[str, Any] = {
                "label": spec["variants"][variant]["label"],
                "trials_submitted": len(trials),
                "trials_complete": len(complete),
                "trials_semantically_passed": len(passed),
                "meets_three_trial_requirement": len(passed) >= 3,
                "phases_seconds": phases,
                "nodes_used": sorted({t["node"]["node_name_sha256"] for t in passed}),
                "gpu_device_kind": sorted({
                    t["measured"]["gpu_device_kind_reported_by_runtime"]
                    for t in passed
                    if t["measured"]["gpu_device_kind_reported_by_runtime"]
                }),
                "nvidia_driver_version": sorted({t["node"]["nvidia_driver_version"] for t in passed}),
                "image_state": sorted({t["measured"]["image"]["state"] for t in passed}),
                "trial_ids": [t["trial_id"] for t in passed],
                "volume_ownership_contention": {
                    t["trial_id"]: t["_contended_by"] for t in passed if t["_contended_by"]
                },
            }
            if passed:
                entry["qualified_level_all_trials"] = level_for(phases["model_start_seconds"]["p95"], thresholds)

            # Split one-time compilation from steady state using the runtime's
            # own first-step timing. A trial that had to compile spends orders of
            # magnitude longer on its first step than one that reused a cached
            # executable, so the split is read from the measurement rather than
            # from which trial happened to run first.
            firsts = {
                t["trial_id"]: t["measured"]["throughput"]["runtime_reported_step_seconds_first"]
                for t in passed
                if (t["measured"].get("throughput") or {}).get("runtime_reported_step_seconds_first") is not None
            }
            if len(firsts) == len(passed) and len(firsts) > 1 and max(firsts.values()) >= COLD_FIRST_STEP_RATIO * min(firsts.values()):
                floor = min(firsts.values())
                cold = sorted(k for k, v in firsts.items() if v >= COLD_FIRST_STEP_RATIO * floor)
                warm = sorted(k for k in firsts if k not in cold)
                by_id = {t["trial_id"]: t for t in passed}
                entry["compilation"] = {
                    "rule": (
                        f"a trial whose first optimizer step is at least {COLD_FIRST_STEP_RATIO}x the cohort's "
                        "fastest first step had to compile; the rest reused cached executables"
                    ),
                    "first_step_seconds": {k: round(v, 2) for k, v in sorted(firsts.items())},
                    "one_time_compilation": {
                        "trials": cold,
                        "model_start_seconds": statistics_for(
                            [by_id[k]["measured"]["phases_seconds"]["model_start_seconds"] for k in cold]
                        ),
                        "qualified_level": level_for(
                            max(by_id[k]["measured"]["phases_seconds"]["model_start_seconds"] for k in cold),
                            thresholds,
                        ),
                    },
                    "steady_state": {
                        "trials": warm,
                        "model_start_seconds": statistics_for(
                            [by_id[k]["measured"]["phases_seconds"]["model_start_seconds"] for k in warm]
                        ),
                        "qualified_level": level_for(
                            max(by_id[k]["measured"]["phases_seconds"]["model_start_seconds"] for k in warm),
                            thresholds,
                        ),
                    },
                }
            if steady and len(steady) != len(passed):
                steady_stats = statistics_for([t["measured"]["phases_seconds"]["model_start_seconds"] for t in steady])
                entry["steady_state"] = {
                    "trials": [t["trial_id"] for t in steady],
                    "excluded": [t["trial_id"] for t in passed if t not in steady],
                    "exclusion_reason": (
                        "volume setup in these trials was serialised behind a recursive fsGroup ownership "
                        "pass over the shared claim, either the trial's own pass or a concurrent trial's pass "
                        "on the same node and claim. Under fsGroupChangePolicy OnRootMismatch that cost is "
                        "paid once per claim rather than on every start, and it disappears entirely once no "
                        "Pod on the node still uses the default Always policy."
                    ),
                    "excluded_detail": [
                        {
                            "trial_id": t["trial_id"],
                            "own_recursive_pass": t["measured"]["volume_ownership"]["recursive_fsgroup_pass_observed"],
                            "stalled_by_concurrent_trials": t["_contended_by"],
                            "sandbox_and_volume_setup_seconds": t["measured"]["phases_seconds"]["sandbox_and_volume_setup_seconds"],
                        }
                        for t in passed if t not in steady
                    ],
                    "model_start_seconds": steady_stats,
                    "qualified_level": level_for(steady_stats["p95"], thresholds),
                }
            model_entry["variants"][variant] = entry
        models[model] = model_entry

    return {
        "schema": "fs2.nebius.ai/fast-start-live-campaign-summary/v1",
        "campaign_id": root["campaign_id"],
        "owner_task": root["owner_task"],
        "cluster": root["cluster"],
        "placement": root["placement"],
        "level_contract": root["level_contract"],
        "mechanism_evidence": root["mechanism_evidence"],
        "models": models,
        "totals": {
            "trials": len(receipts),
            "complete": sum(1 for r in receipts if r["job_state"] == "complete"),
            "semantically_passed": sum(
                1 for r in receipts if r.get("semantic_validation", {}).get("status") == "passed"
            ),
        },
    }


def main() -> int:
    summary = summarize()
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for model, entry in summary["models"].items():
        for variant, values in entry["variants"].items():
            steady = values.get("steady_state", {})
            print(
                f"{model}/{variant}: {values['trials_semantically_passed']}/{values['trials_submitted']} passed, "
                f"p95 model start {values['phases_seconds']['model_start_seconds']['p95']}s "
                f"-> {values.get('qualified_level_all_trials')}"
                + (f" (steady state {steady['model_start_seconds']['p95']}s -> {steady['qualified_level']})" if steady else "")
            )
    print(json.dumps(summary["totals"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

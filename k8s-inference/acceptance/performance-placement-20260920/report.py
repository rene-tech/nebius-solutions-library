"""Read durable campaign APIs and export an explicit, non-readiness report."""

import argparse
import json
import os
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from inventory import admin_client


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render(campaigns, observed_at):
    rows = ["# Scientific AI benchmark collection", "", f"Observed: {observed_at}", "",
            "Exploratory, uncontrolled-cache measurements on the shared cluster. "
            "Not a cold-start, hardware-winner or customer-readiness claim. "
            "Times below cover the named trial, which can contain multiple requests. "
            "Missing/replayed client timings are not zero.", ""]
    for campaign in campaigns:
        counts = Counter(trial["status"] for trial in campaign["trials"])
        rows.extend([f"## {cell(campaign['name'])}", "", f"Campaign `{campaign['id']}`; "
                     f"specification SHA-256 `{campaign['spec_sha256']}`.", "",
                     "; ".join(f"{count} {status}" for status, count in sorted(counts.items())) + ".", "",
                     "| Model / workload | Passed | Failed | Active / queued | Excluded | Measured end-to-end seconds (n; min / median / max) |",
                     "|---|---:|---:|---:|---:|---|"])
        groups = {}
        for trial in campaign["trials"]:
            groups.setdefault((trial["model_id"], trial["workload_class"]), []).append(trial)
        for (model, workload), trials in sorted(groups.items()):
            outcomes = Counter(trial["status"] for trial in trials)
            measurements = [trial["result"]["elapsed_seconds"] for trial in trials
                            if trial["status"] == "succeeded" and trial.get("result")
                            and trial["result"].get("elapsed_seconds") is not None]
            timing = (f"{len(measurements)}; {min(measurements):.2f} / {median(measurements):.2f} / {max(measurements):.2f}"
                      if measurements else "—")
            active = outcomes["queued"] + outcomes["running"]
            failed = sum(outcomes[s] for s in ("failed", "expired", "cancelled", "preempted"))
            excluded = outcomes["unsupported"] + outcomes["capacity-unavailable"]
            rows.append(f"| {cell(model)} / {cell(workload)} | {outcomes['succeeded']} | {failed} | {active} | {excluded} | {timing} |")
        failures = [(trial["model_id"], trial["id"], (trial.get("result") or {}).get("error_code"))
                    for trial in campaign["trials"] if trial["status"] == "failed"]
        if failures:
            rows.extend(["", "Retained failed trials (including original runner failures):", ""])
            rows.extend(f"- `{model}`: `{trial}` — `{error}`" for model, trial, error in failures)
        rows.append("")
    rows.extend(["## Interpretation", "",
                 "- The initial campaign retains its original exclusions. Extension campaigns add those adapters; "
                 "do not sum these exclusions as permanently unsupported models.",
                 "- Corrected campaigns do not erase failed original trials. Compare their exact fixture/runtime specifications.",
                 "- GPU placement requires separately controlled equivalent-input cohorts on multiple actual hardware environments.",
                 "- Full scientific phase receipts retain measured/estimated evidence labels; estimated GPU accounting "
                 "is not promoted to measured registry fields.", ""])
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--origin", default="https://89.169.99.188")
    parser.add_argument("--campaign-id", action="append", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    campaigns = []
    with closing(admin_client(args.kubeconfig, args.context, args.origin)) as client:
        for identity in args.campaign_id:
            response = client.get("/admin/api/v1/performance/campaigns/" + identity)
            response.raise_for_status()
            campaigns.append(response.json()["data"])
        client.delete("/admin/api/v1/session")
    observed_at = datetime.now(UTC).isoformat()
    (args.directory / "campaigns.json").write_text(json.dumps({"observed_at": observed_at, "campaigns": campaigns}, indent=2))
    (args.directory / "report.md").write_text(render(campaigns, observed_at))
    for campaign in campaigns:
        print(json.dumps({"campaign_id": campaign["id"], "name": campaign["name"],
                          "counts": Counter(t["status"] for t in campaign["trials"])}))


if __name__ == "__main__":
    main()

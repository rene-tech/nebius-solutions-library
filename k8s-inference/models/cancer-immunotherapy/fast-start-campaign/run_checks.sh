#!/usr/bin/env bash
# Offline checks for the fast-start live campaign. No cluster access required.
set -euo pipefail
cd "$(dirname "$0")"

echo "== python syntax =="
python3 -m py_compile faststart.py summarize.py validate_trial.py gpu_metrics.py tests/test_faststart.py

echo "== json well-formedness =="
for file in campaign_matrix.json af3-fold-input.json CAMPAIGN_SUMMARY.json receipts/*.json; do
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$file"
done

echo "== summary is current with the receipts =="
python3 - <<'PY'
import hashlib, json, pathlib, subprocess, sys
before = pathlib.Path("CAMPAIGN_SUMMARY.json").read_bytes()
subprocess.run([sys.executable, "summarize.py"], check=True, stdout=subprocess.DEVNULL)
after = pathlib.Path("CAMPAIGN_SUMMARY.json").read_bytes()
if hashlib.sha256(before).hexdigest() != hashlib.sha256(after).hexdigest():
    raise SystemExit("CAMPAIGN_SUMMARY.json is stale; re-run summarize.py and commit the result")
print("summary matches the receipts")
PY

echo "== every receipt carries a semantic verdict and an evidence-backed level =="
python3 - <<'PY'
import json, pathlib
matrix = json.loads(pathlib.Path("campaign_matrix.json").read_text())
thresholds = matrix["level_contract"]["thresholds_seconds"]
receipts = sorted(pathlib.Path("receipts").glob("*.json"))
if not receipts:
    raise SystemExit("no trial receipts")
for path in receipts:
    receipt = json.loads(path.read_text())
    name = receipt["trial_id"]
    assert receipt["job_state"] == "complete", f"{name} did not complete"
    assert receipt["semantic_validation"]["status"] == "passed", f"{name} failed its semantic gate"
    assert "@sha256:" in receipt["image"]["reference"], f"{name} is not digest-pinned"
    assert receipt["node"]["accelerator_class"] == "nvidia-h100-sxm5-80gb", f"{name} did not run on H100"
    assert receipt["node"]["snapshot_eligible"] == "false", f"{name} node claims snapshot eligibility"
    assert receipt["node"]["local_nvme_eligible"] == "false", f"{name} node claims local NVMe"
    measured = receipt["measured"]
    start = measured["phases_seconds"]["model_start_seconds"]
    if start is None:
        assert measured["assigned_level"] == "unavailable", f"{name} assigned a level without a measurement"
    else:
        expected = "Off"
        for level in ("L4", "L3", "L2", "L1"):
            if start <= thresholds[level]:
                expected = level
                break
        assert measured["assigned_level"] == expected, f"{name} level does not follow the contract"
    for label, value in measured["phases_seconds"].items():
        assert value is None or value >= 0.0, f"{name} publishes a negative {label}"
print(f"{len(receipts)} receipts validated")
PY

echo "== no GPU snapshot claim without restore proof =="
python3 - <<'PY'
import json, pathlib
matrix = json.loads(pathlib.Path("campaign_matrix.json").read_text())
snapshot = matrix["mechanism_evidence"]["gpu_process_snapshot"]
assert snapshot["state"] == "unsupported" and snapshot["claim"] == "none", snapshot
for path in pathlib.Path("receipts").glob("*.json"):
    receipt = json.loads(path.read_text())
    assert receipt["mechanism_evidence"]["gpu_process_snapshot"]["claim"] == "none", path.name
print("no snapshot level is claimed anywhere")
PY

echo "== unit tests =="
python3 -m unittest discover -s tests

echo "ALL CHECKS PASSED"

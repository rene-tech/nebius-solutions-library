"""Exact candidate HTTP experiment. Exhaustion is not successful generation."""

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rdkit import Chem, DataStructs
from rdkit.Chem import QED, rdFingerprintGenerator


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    args = p.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    test_cases = [
        (x["name"], x["request"], x["purpose"])
        for x in json.loads(args.cases.read_text())
    ]
    fingerprint = rdFingerprintGenerator.GetMorganGenerator(radius=2)
    summaries = []
    prior = {}
    with httpx.Client(base_url=args.base_url, timeout=180, trust_env=False) as client:
        health = client.get("/v1/health/ready")
        health.raise_for_status()
        assert health.json()["compute_capability"] == "9.0"
        (args.output / "health.json").write_text(
            json.dumps(health.json(), indent=2) + "\n"
        )
        for cohort in (1, 2):
            for name, req, purpose in test_cases:
                started = datetime.now(timezone.utc).isoformat()
                tick = time.monotonic()
                response = client.post("/generate", json=req)
                elapsed = time.monotonic() - tick
                value = response.json()
                checks = {}
                counts = None
                if response.status_code == 200:
                    items = value["generated"]
                    counts = value["metrics"]
                    source = Chem.MolFromSmiles(req["smi"])
                    source_canonical = Chem.MolToSmiles(source)
                    canonical = []
                    improved = 0
                    for item in items:
                        molecule = Chem.MolFromSmiles(item["sample"])
                        assert molecule is not None
                        smi = Chem.MolToSmiles(molecule)
                        canonical.append(smi)
                        assert smi != source_canonical and item["model_decoded"] is True
                        assert abs(QED.qed(molecule) - item["score"]) < 1e-9
                        sim = DataStructs.TanimotoSimilarity(
                            fingerprint.GetFingerprint(source),
                            fingerprint.GetFingerprint(molecule),
                        )
                        assert abs(sim - item["similarity"]) < 1e-9 and sim >= req.get(
                            "min_similarity", 0.3
                        )
                        improved += int(
                            item["score"] < QED.qed(source)
                            if req.get("minimize", False)
                            else item["score"] > QED.qed(source)
                        )
                    assert (
                        len(items) == len(set(canonical)) == req.get("num_molecules", 1)
                    )
                    scores = [x["score"] for x in items]
                    assert scores == sorted(
                        scores, reverse=not req.get("minimize", False)
                    )
                    checks = dict(
                        returned=len(items),
                        distinct=len(set(canonical)),
                        all_changed=True,
                        independent_qed_similarity=True,
                        source_qed=QED.qed(source),
                        improved_count=improved,
                        min_qed=min(scores),
                        max_qed=max(scores),
                        min_similarity=min(x["similarity"] for x in items),
                    )
                elif response.status_code == 422:
                    detail = value["detail"]
                    code = (
                        detail.get("code")
                        if isinstance(detail, dict)
                        else "REQUEST_VALIDATION"
                    )
                    assert code in {
                        "GENERATION_EXHAUSTED",
                        "INVALID_MOLECULE",
                        "REQUEST_VALIDATION",
                    }
                    assert "generated" not in value
                    counts = detail.get("counts") if isinstance(detail, dict) else None
                    checks = {"code": code}
                    if counts:
                        assert counts["distinct_feasible_molecules"] < req.get(
                            "num_molecules", 1
                        )
                else:
                    raise AssertionError(
                        f"{name}: unexpected HTTP {response.status_code}"
                    )
                if counts:
                    keys = [
                        "invalid_decodes",
                        "below_similarity",
                        "unchanged_decodes",
                        "duplicate_decodes",
                        "distinct_feasible_molecules",
                    ]
                    assert counts["attempted_model_decodes"] == req.get(
                        "particles", 2
                    ) * req.get("iterations", 1)
                    assert counts["optimizer_steps"] == req.get("iterations", 1)
                    assert (
                        sum(counts[k] for k in keys)
                        == counts["attempted_model_decodes"]
                    )
                    checks["counts"] = {
                        k: counts[k]
                        for k in keys + ["attempted_model_decodes", "optimizer_steps"]
                    }
                stable = {"status": response.status_code, "body": value}
                if cohort == 1:
                    prior[name] = stable
                else:
                    assert stable == prior[name], (
                        f"{name}: replay changed without user seed control"
                    )
                row = dict(
                    cohort=cohort,
                    case=name,
                    purpose=purpose,
                    started_at=started,
                    elapsed_seconds=elapsed,
                    http_status=response.status_code,
                    generation_succeeded=response.status_code == 200,
                    truthful_output_checks_passed=True,
                    checks=checks,
                )
                receipt = dict(row, request=req, response=value)
                path = args.output / f"cohort-{cohort}-{name}.json"
                path.write_text(json.dumps(receipt, indent=2) + "\n")
                row["receipt_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                summaries.append(row)
                print(json.dumps(row), flush=True)
                (args.output / "partial-summary.json").write_text(
                    json.dumps(summaries, indent=2) + "\n"
                )
        (args.output / "metrics.txt").write_text(client.get("/metrics").text)
    summary = dict(
        scope="isolated unchanged candidate H100 HTTP; no production/public qualification",
        cases_per_cohort=len(test_cases),
        cohorts=2,
        results=summaries,
        rng="deterministic canonical-input-derived seed; no numeric seed API",
        constraints="finite-budget exhaustion, not proof of chemical infeasibility",
    )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    main()

"""Exact-image computational qualification. This is not hosted acceptance.

Run inside each pinned image, mounting this directory read-only at /qualification.
The output is public-safe: fixtures are public structures, not customer data.
"""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import resource
import time
from pathlib import Path

from fixtures import MOLECULES, semantic_requests


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_prediction(model_id, result, count):
    assert len(result) == count
    for row in result:
        assert len(row["predictions"]) == (52 if model_id == "admet-ai" else 3)
        for endpoint in row["predictions"].values():
            if model_id == "admet-ai":
                assert isinstance(endpoint, (int, float)) and math.isfinite(endpoint)
            else:
                assert 0 <= endpoint["positive_class_score"] <= 1
                assert endpoint["predicted_class"] in (0, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=["admet-ai", "ctoxpred2"])
    args = parser.parse_args()
    started = time.perf_counter()
    if args.model == "admet-ai":
        from toxicology.admet import ADMETRuntime
        runtime = ADMETRuntime()
        assets = sorted((runtime.root / "resources/models").rglob("*.pt"))
        if not assets:
            assets = sorted((runtime.root / "resources/models").rglob("*.ckpt"))
        asset_root = runtime.root
    else:
        from toxicology.ctox import CToxRuntime
        runtime = CToxRuntime()
        asset_root = runtime.root
        assets = sorted(path for path in (runtime.root / "models").rglob("*") if path.suffix in {".joblib", ".sav", ".model"})
    loaded = time.perf_counter() - started
    inventory = [{"path": str(path.relative_to(asset_root)), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in assets]
    assert inventory, "weight inventory must not be empty"
    measurements = []
    receipts = []
    for index, payload in enumerate(semantic_requests()):
        structures = [row["smiles"] for row in payload["molecules"]]
        started = time.perf_counter()
        result = runtime.predict(structures)
        elapsed = time.perf_counter() - started
        validate_prediction(args.model, result, len(structures))
        receipts.append({"id": f"toxicity-panel-{index}", "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(), "result_sha256": hashlib.sha256(canonical(result)).hexdigest(), "predictions": result, "seconds": elapsed})
    assert receipts[0]["result_sha256"] != receipts[1]["result_sha256"]
    # Compare the adapter's complete outputs with independently invoked
    # upstream inference, not a hand-written expected safety classification.
    structures = [row["smiles"] for row in MOLECULES]
    result = runtime.predict(structures)
    if args.model == "admet-ai":
        from admet_ai import ADMETModel
        reference = ADMETModel(drugbank_path=None, num_workers=0).predict(structures)
        differences = [abs(row["predictions"][name] - float(reference.iloc[i][name])) for i, row in enumerate(result) for name in row["predictions"]]
    else:
        import joblib
        import numpy as np
        from mordred import Calculator, descriptors
        from rdkit import Chem
        from utils import compute_fingerprint_features
        # Same formula as the upstream notebooks/nutils.py SSL path. Setting
        # nproc=1 changes descriptor scheduling, not the feature definition.
        desc = Calculator(descriptors, ignore_3D=True).pandas([Chem.MolFromSmiles(s) for s in structures], nproc=1, quiet=True)
        transform = joblib.load(runtime.root / "models/decriptors_preprocessing/global_preprocessing_pipeline.sav")
        features = np.concatenate((compute_fingerprint_features(structures), transform.transform(desc)), axis=1)
        differences = []
        for name, filename in {"hERG": "_ssl_herg_model", "Nav1.5": "_ssl_nav_model", "Cav1.2": "_ssl_cav_model"}.items():
            reference = joblib.load(runtime.root / "models/random_forest" / name / f"{filename}.joblib")
            reference.n_jobs = 2
            scores = reference.predict_proba(features)
            for i, row in enumerate(result):
                actual = row["predictions"][name]
                assert actual["predicted_class"] == int(scores[i].argmax())
                differences.append(abs(actual["positive_class_score"] - float(scores[i, 1])))
        neural = runtime.predict(structures, method="dl-sl", seed=17)
        neural_replay = runtime.predict(structures, method="dl-sl", seed=17)
        validate_prediction(args.model, neural, len(structures))
        assert canonical(neural) == canonical(neural_replay)
        measurements.append({"case": "dl-sl-fixed-seed-replay", "molecules": len(structures), "passed": True})
    assert max(differences) <= 1e-6
    for size in (1, 32, 1000):
        batch = [MOLECULES[i % len(MOLECULES)]["smiles"] for i in range(size)]
        started = time.perf_counter()
        output = []
        for offset in range(0, size, 32):
            output.extend(runtime.predict(batch[offset:offset + 32]))
        elapsed = time.perf_counter() - started
        validate_prediction(args.model, output, size)
        measurements.append({"case": "repeated-public-fixture-throughput", "molecules": size, "seconds": elapsed, "molecules_per_second": size / elapsed, "unique_structures": min(size, len(MOLECULES))})
    receipt = {
        "schema": "fs2-toxicology-runtime-qualification/v1", "model_id": args.model, "passed": True,
        "hosted_qualified": False, "scientific_accuracy_claimed": False,
        "environment": {"python": platform.python_version(), "machine": platform.machine(), "cpu_threads": os.getenv("TOX_CPU_THREADS"), "packages": {dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()}},
        "metadata": runtime.metadata(), "load_seconds": loaded,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "upstream_parity_max_absolute_difference": max(differences),
        "semantic_requests": receipts, "benchmarks": measurements, "weight_files": inventory,
        "limits": ["Direct local CPU inference, not public REST/MCP or autoscaling acceptance.", "Repeated-fixture timing is a throughput test, not validation of predictive accuracy or chemical applicability."],
    }
    print("FS2_RECEIPT=" + json.dumps(receipt, sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()

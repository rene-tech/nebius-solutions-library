"""Render one bounded isolated HTTP regression Pod from retained r6 inputs."""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path

import yaml
from prepare_candidate import PIN, ROOT, UID_ENV


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not PIN.fullmatch(args.image) or args.output.exists():
        raise ValueError("new output directory and immutable image required")
    raw = (args.reference / "cases.json").read_bytes()
    cases = json.loads(raw)
    receipt = json.loads((args.reference / "results/receipt.json").read_bytes())
    if not receipt["passed"] or len(cases) != 12 or receipt["cases_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("frozen reference cohort identity mismatch")
    references = {}
    for case in cases:
        run = next(row for row in receipt["runs"] if row["case_id"] == case["case_id"] and row["repetition"] == 1)
        source = args.reference / "results" / run["result_file"]
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != run["result_sha256"]:
            raise ValueError("reference result changed")
        if (
            run["input_sha256"]
            != hashlib.sha256(json.dumps(case["arguments"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        ):
            raise ValueError("reference request differs")
        references[case["case_id"]] = json.loads(content)
    refraw = json.dumps(references, sort_keys=True, separators=(",", ":")).encode()
    old = list(yaml.safe_load_all((args.reference / "manifest.yaml").read_text()))
    pod = copy.deepcopy(next(item for item in old if item["kind"] == "Pod"))
    labels = {"app.kubernetes.io/name": "fs2-diffdock-http-qualification", "fs2.nebius.ai/qualification": args.name}
    pod["metadata"] = {"name": args.name, "namespace": "fs2-models", "labels": labels}
    pod["spec"]["containers"][0].update(
        image=args.image,
        command=[
            "python3",
            "/qualification/qualify_http.py",
            "--cases",
            "/qualification/cases.json.gz",
            "--references",
            "/qualification/references.json.gz",
            "--output",
            "/tmp/http-qualification",
        ],
        env=[copy.deepcopy(UID_ENV)],
    )
    next(v for v in pod["spec"]["volumes"] if v["name"] == "qualification")["configMap"]["name"] = args.name
    config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "immutable": True,
        "metadata": {"name": args.name, "namespace": "fs2-models", "labels": labels},
        "data": {
            "qualify_http.py": Path(__file__).with_name("qualify_http.py").read_text(),
            "seed_qualify.py": (ROOT / "models/structure/runtime/diffdock-seeded/qualify.py").read_text(),
        },
        "binaryData": {
            "cases.json.gz": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
            "references.json.gz": base64.b64encode(gzip.compress(refraw, mtime=0)).decode(),
        },
    }
    if len(json.dumps(config).encode()) >= 1024 * 1024:
        raise ValueError("fixture ConfigMap exceeds existing Kubernetes limit")
    os.umask(0o077)
    args.output.mkdir(parents=True, mode=0o700)
    (args.output / "manifest.yaml").write_text(yaml.safe_dump_all([config, pod], sort_keys=False))
    (args.output / "cases.json").write_bytes(raw)
    preparation = {
        "applied": False,
        "requests": 12,
        "image": args.image,
        "reference_results_sha256": hashlib.sha256(refraw).hexdigest(),
        "cases_sha256": hashlib.sha256(raw).hexdigest(),
        "reference_receipt_sha256": hashlib.sha256((args.reference / "results/receipt.json").read_bytes()).hexdigest(),
        "qualifier_sha256": hashlib.sha256(config["data"]["qualify_http.py"].encode()).hexdigest(),
        "resources": pod["spec"]["containers"][0]["resources"],
        "node_selector": pod["spec"].get("nodeSelector"),
        "active_deadline_seconds": pod["spec"].get("activeDeadlineSeconds"),
    }
    (args.output / "preparation.json").write_text(json.dumps(preparation, indent=2) + "\n")
    print(json.dumps(preparation))


if __name__ == "__main__":
    main()

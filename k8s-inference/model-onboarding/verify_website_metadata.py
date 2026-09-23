#!/usr/bin/env python3
"""Check new App IDs against the metadata actually deployed on their public website.

No cloud credentials or third-party dependencies are needed. This check belongs
before model publication, not in the serving request path. Availability remains
owned by the inference catalog; website metadata never grants inference access.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
INVENTORIES = (
    "k8s-inference/catalog/runtime/models",
    "k8s-inference/catalog/runtime/native",
    "k8s-inference/catalog/runtime/contracts/scientific-workload-profiles.json",
)
DOMAINS = {
    "structure", "protein-design", "protein-language", "genomics", "small-molecule",
    "single-cell", "imaging", "sequence-search", "age-prediction",
    "generative-media", "physical-ai-robotics", "speech", "general-ai", "molecular-dynamics",
}


def ids_in_document(document: dict) -> set[str]:
    record = document.get("record", document)
    ids = set()
    if isinstance(record.get("model"), dict) and record["model"].get("id"):
        ids.add(record["model"]["id"].strip().lower())
    for profile in document.get("profiles", []):
        ids.add(profile["model_id"].strip().lower())
    return ids


def ids_at_ref(ref: str, root: Path = ROOT) -> set[str]:
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=root, text=True,
    ).strip()
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", commit, "--", *INVENTORIES],
        cwd=root, text=True,
    ).splitlines()
    ids = set()
    for path in paths:
        if path.endswith(".json"):
            raw = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=root)
            ids.update(ids_in_document(json.loads(raw)))
    if not ids:
        raise ValueError(f"No model inventory found at {ref}; refusing an empty check")
    return ids


def https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    url = urlparse(value)
    return url.scheme == "https" and bool(url.hostname) and not url.username and not url.password


def metadata_issues(model_ids: set[str], payload: dict) -> list[str]:
    if payload.get("schema") != "scientific-ai/catalog-metadata/v1":
        raise ValueError("Website metadata endpoint has an unsupported or missing schema")
    if not isinstance(payload.get("models"), list) or not payload["models"]:
        raise ValueError("Website metadata endpoint has no models")
    lookup = {}
    for row in payload["models"]:
        for key in [row["id"], *row.get("aliases", [])]:
            key = key.strip().lower()
            if key in lookup:
                raise ValueError(f"Duplicate website model ID or alias: {key}")
            lookup[key] = row
    issues = []
    for model_id in sorted(model_ids):
        row = lookup.get(model_id.strip().lower())
        if row is None:
            issues.append(f"{model_id}: missing deployed website metadata")
            continue
        if row.get("domain") not in DOMAINS:
            issues.append(f"{model_id}: missing or invalid category")
        if not https_url(row.get("homepage")):
            issues.append(f"{model_id}: missing public HTTPS information link")
        if "attribution" not in row:
            issues.append(f"{model_id}: missing explicit attribution decision")
        credit = row.get("attribution")
        if credit is not None:
            pair = (credit.get("label"), credit.get("relationship"))
            if pair not in {("NVIDIA", "publisher"), ("NVIDIA BioNeMo", "ecosystem")} or not https_url(credit.get("source")):
                issues.append(f"{model_id}: invalid NVIDIA attribution")
        homepage = urlparse(row.get("homepage", ""))
        if homepage.hostname == "huggingface.co" and homepage.path.startswith("/nvidia/") and not credit:
            issues.append(f"{model_id}: NVIDIA model card has no NVIDIA credit")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--website-url", required=True)
    parser.add_argument("--model-id", action="append", default=[])
    parser.add_argument("--base-ref", help="Check App IDs introduced since this Git commit")
    parser.add_argument("--head-ref", default="HEAD")
    args = parser.parse_args(argv)
    try:
        if not https_url(args.website_url):
            raise ValueError("--website-url must be a public HTTPS origin without credentials")
        origin = urlparse(args.website_url)
        if origin.path not in {"", "/"} or origin.query or origin.fragment:
            raise ValueError("--website-url must be an origin, not a path or query")
        if not args.base_ref and not args.model_id:
            raise ValueError("Supply --base-ref or at least one --model-id")
        model_ids = {value.strip().lower() for value in args.model_id if value.strip()}
        if args.base_ref:
            model_ids.update(ids_at_ref(args.head_ref) - ids_at_ref(args.base_ref))
        # Also detect an older candidate that became live, a manual registration,
        # or a website rollback. An unchanged model inventory is not evidence that
        # the currently published website is complete.
        with urlopen(args.website_url.rstrip("/") + "/api/models", timeout=20) as response:
            live = json.load(response)
        if live.get("status") != "ok" or live.get("source") != "live" or live.get("dropped") != 0 or not live.get("models"):
            raise ValueError("Public catalog must be fresh, live, nonempty and have no dropped models")
        model_ids.update(row["id"].strip().lower() for row in live["models"])
        with urlopen(args.website_url.rstrip("/") + "/api/catalog-metadata.json", timeout=20) as response:
            payload = json.load(response)
        issues = metadata_issues(model_ids, payload)
        if issues:
            raise ValueError("\n".join(issues) + "\nPublish the website metadata first, then retry model publication.")
        print(f"Verified deployed website category, information link and attribution for {len(model_ids)} model(s): {', '.join(sorted(model_ids))}")
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Website model-onboarding check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

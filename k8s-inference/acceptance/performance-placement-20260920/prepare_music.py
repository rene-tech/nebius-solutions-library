"""Freeze the newly published ACE-Step fixture without submitting inference."""

import argparse
import json
import os
from pathlib import Path

import httpx

from extra import MUSIC
from prepare_extra import upload
from runner import canonical, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=False)
    model = "ace-step-1-5"
    with httpx.Client(base_url="https://89.169.99.188", timeout=180, trust_env=False,
                      headers={"Authorization": "Bearer " + args.token_file.read_text().strip()}) as client:
        response = client.get("/v1/models")
        response.raise_for_status()
        if model not in {entry["id"] for entry in response.json()["data"]}:
            raise RuntimeError("model_not_published_to_benchmark_identity")
        raw = canonical({"model_id": model, "operation": "generate-music",
                         "requests": [{"payload": payload, "oracle": {}} for payload in MUSIC.REQUESTS],
                         "attribution": "Two committed synthetic instrumental prompts; no customer data"})
        artifact = upload(client, model, raw, "application/json")
    cases = [{"case_id": model, "model_id": model, "workload_class": "two-twelve-second-instrumental-clips",
              "adapter": "artifact-native-v1", "fixture_sha256": sha(raw),
              "fixture_ref": "artifact://" + artifact["artifact_id"], "repetitions": 3,
              "cache_condition": "uncontrolled"}]
    (args.directory / "cases.json").write_bytes(canonical(cases))
    print(json.dumps({"model": model, "fixture_prepared": True}))


if __name__ == "__main__":
    main()

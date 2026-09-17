#!/usr/bin/env python3
"""Bounded read-only speech discovery, own-storage and MindEval preservation."""

import argparse
import json
import os
from pathlib import Path

import httpx

from collect_live import check, collect, digest, kube, now, private_json, require_release, write_private
from run_live import validate_canary

SPEECH_MODELS = {
    "diar-streaming-sortformer-4spk-v2-1", "magpie-tts-multilingual-357m",
    "nemotron-speech-en-0-6b", "nemotron-speech-multilingual-0-6b",
    "parakeet-realtime-eou-120m-v1",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--expected-cp-image", required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    snapshot = collect(args.kubeconfig, args.context)
    require_release(snapshot, args.expected_cp_image)
    token = validate_canary(private_json(args.key_file), snapshot)
    report = {"started_at": now(), "scope": "read-only-sibling-preservation-no-inference-no-provisioning",
              "control_plane_image": args.expected_cp_image, "outcome": "failed"}
    try:
        with httpx.Client(base_url=snapshot["public_endpoint"], timeout=20, trust_env=False,
                          follow_redirects=False, headers={"Authorization": "Bearer " + token}) as http:
            response = http.get("/v1/models")
            check(response.status_code == 200, "model_catalog_failed")
            catalog = response.json()
            report["catalog_sha256"] = digest(catalog)
            report["speech_catalog"] = [{"id": row["id"], "capabilities": row.get("capabilities", []),
                                         "operations": row.get("operations", [])}
                for row in catalog["data"] if row["id"] in SPEECH_MODELS]
            check({row["id"] for row in report["speech_catalog"]} == SPEECH_MODELS,
                  "advertised_speech_model_missing")
            response = http.get("/v1/storage")
            check(response.status_code == 200, "own_storage_read_failed")
            storage = response.json()
            report["storage"] = {key: storage.get(key) for key in ("state", "mode", "quota_bytes")}
            check(storage.get("state") == "disabled" and storage.get("mode") == "disabled",
                  "stockholm_storage_not_disabled")
            check(not storage.get("bucket_name") and not storage.get("access_key_id")
                  and not storage.get("secret_access_key"), "excluded_tenant_storage_identity_exposed")
            response = http.get("/v1/mindeval/catalog")
            report["mindeval_catalog_http_status"] = response.status_code
            check(response.status_code == 200, "mindeval_public_catalog_failed")
            report["mindeval_catalog_sha256"] = digest(response.json())
        script = '''
import json, urllib.request
with urllib.request.urlopen('http://fs2-mindeval-gateway.fs2-system.svc.cluster.local:8080/healthz',timeout=10) as response:
    print(json.dumps({'status':response.status, 'health':json.load(response)}))
'''
        report["outcome"] = "read_checks_passed"
        try:
            report["mindeval_internal_health"] = kube(args.kubeconfig, args.context, "-n", "fs2-system",
                "exec", "-i", "deploy/fs2-serve-control-plane", "-c", "control-plane", "--", "python", "-", script=script)
            check(report["mindeval_internal_health"]["health"].get("catalog_ready") is True,
                  "mindeval_catalog_not_ready")
        except Exception as error:
            report["optional_internal_probe_failure"] = type(error).__name__
            report["outcome"] = "public_read_checks_passed_internal_probe_unavailable"
    except Exception as error:
        report["failure_type"] = type(error).__name__
    report["completed_at"] = now()
    write_private(args.output, report, (token,))
    print(json.dumps({"outcome": report["outcome"], "output": str(args.output)}))
    return 0 if report["outcome"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

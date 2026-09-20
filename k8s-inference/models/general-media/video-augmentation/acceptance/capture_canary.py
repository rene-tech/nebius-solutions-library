"""Export allowlisted, credential-free facts from the isolated September canary."""

import argparse
import hashlib
import json
from pathlib import Path


def capture(root: Path) -> dict:
    def read(name):
        return json.loads((root / name).read_text())

    cases = []
    for name in (
        "gpu-prerequisite-r1",
        "gpu-prerequisite-r2",
        "gpu-prerequisite-r3-small",
        "gpu-prerequisite-r4-guidance3",
        "gpu-prerequisite-r5-guidance3",
    ):
        directory = root / name
        if not directory.is_dir():
            raise ValueError(f"missing retained case: {name}")
        case = {"id": name, "result_present": (directory / "result.json").is_file()}
        if case["result_present"]:
            result = read(f"{name}/result.json")
            case["counts"] = result["counts"]
            case["items"] = [
                {
                    key: item.get(key)
                    for key in (
                        "status",
                        "input",
                        "output",
                        "checks",
                        "recipe_sha256",
                        "source_sha256",
                        "error_code",
                        "error_detail",
                    )
                }
                for item in result["items"]
            ]
        cases.append(case)
    audit = read("final-audit.json")
    uploaded = read("live-workspace-upload.json")
    return {
        "schema": "fs2-serve.nebius.ai/video-canary-evidence/v1",
        "recorded_at": audit["recorded_at"],
        "customer_ready": False,
        "hosted_parent_activated": False,
        "live_gpu_generation_tested": True,
        "worker_runtime_tested": True,
        "motion_validation_passed": False,
        "weather_validation_passed": False,
        "transport_scope": "Exact local worker with loopback protocol translation to the ordinary canary key's public native Cosmos API; not hosted parent/delegation or chat generation acceptance.",
        "worker_source_commit": "f9812bcfea8b97c02fea5886431cd58eea78cde6",
        "runtime_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/fs2-video-augmentation@sha256:3500d15d8a04d74966377350e8a9cdf9546b3280dc63c1474b1b64251b8b488c",
        "workbench_source_commit": "94fed505a83de28609b4797bf050ceb305d1a16f",
        "workbench_image": "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:3e5df6c2ec9318128b7f9cfde1b236cee8b0273befe84198860fb34b5de50f35",
        "workbench_endpoint_id": "aiendpoint-e00m58m8ym56ky5f4m",
        "workbench_upload": {
            key: uploaded[key]
            for key in (
                "browser_uploaded_object",
                "bucket_name",
                "size_bytes",
                "sha256",
                "s3_bytes_match_browser_source",
                "unauthenticated_video_api_status",
                "generated_video_tested",
            )
        },
        "audit": audit,
        "oci_attestations": read("oci-attestation-verification.json"),
        "cases": cases,
        "protected_receipt_sha256": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (
                "gpu-prerequisite-r2/transport.private.json",
                "gpu-prerequisite-r3-small/result.json",
                "gpu-prerequisite-r5-guidance3/result.json",
                "live-workspace-upload.json",
                "oci-attestation-verification.json",
                "final-audit.json",
                "review-r3/source-16.png",
                "review-r3/output-16.png",
                "review-r3/guidance3-output-16.png",
            )
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x") as handle:
        json.dump(capture(args.private_root), handle, indent=2, sort_keys=True)
        handle.write("\n")

"""Freeze public robotics inputs as benchmark-tenant artifacts, no inference."""

import argparse
import json
import os
from pathlib import Path

import httpx

from prepare_extra import upload
from runner import ROOT, canonical, sha
from extra import MEDIA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("token-file", "directory", "video", "dataset-archive"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True)
    cases = []
    with httpx.Client(base_url="https://89.169.99.188", timeout=180, trust_env=False,
                      headers={"Authorization": "Bearer " + args.token_file.read_text().strip()}) as client:
        def save(model, fixture, adapter, workload):
            raw = canonical(fixture)
            artifact = upload(client, model, raw, "application/json")
            cases.append({"case_id": model, "model_id": model, "workload_class": workload,
                          "adapter": adapter, "fixture_sha256": sha(raw),
                          "fixture_ref": "artifact://" + artifact["artifact_id"], "repetitions": 3,
                          "cache_condition": "uncontrolled"})
            (args.directory / "cases.json").write_bytes(canonical(cases))
            print(json.dumps({"model": model, "fixture_prepared": True}), flush=True)

        model = "cosmos-transfer2-5-2b"
        raw = args.video.read_bytes()
        assert sha(raw) == "3ccebbe9e650d659990629c0dbfee6b04cee24fdd0a54935b898ed7d40f6f1d0"
        artifact = upload(client, model, raw, "video/mp4")
        stream = MEDIA.probe_mp4(raw)["streams"][0]
        record = {"payload": {"video": artifact, "prompt": "Heavy overcast sky, diffuse soft lighting. Preserve camera motion, all objects and the road layout.",
                              "seed": 42, "num_steps": 35, "guidance": 7, "control_weight": 1.0,
                              "output_delivery": "artifact"},
                  "oracle": {"geometry_frames_rate": [stream["width"], stream["height"], int(stream["nb_frames"]), stream["avg_frame_rate"]]}}
        save(model, {"model_id": model, "operation": "transfer-video", "requests": [record],
                     "attribution": "NVIDIA paidf-augmentation bc5719362492a1e3b40bd7d33b43c46dd89efad5 public intersection fixture; explicitly prepared 640x480/93-frame/30fps input, no server-side trimming"},
             "artifact-native-v1", "93-frame-640x480-video-transfer")

        model = "cosmos3-lerobot-augmentation"
        raw = args.dataset_archive.read_bytes()
        bundle = upload(client, model, raw, "application/x-tar", compression="zstd")
        manifest = {"schema": "fs2-serve.nebius.ai/scientific-artifact-manifest/v1", "manifest_id": "benchmark-lerobot-20260920",
                    "entries": [{"name": "lerobot-dataset", "semantic_type": "lerobot-v3-bundle/v1", "artifact": bundle}]}
        manifest_ref = upload(client, model, canonical(manifest), "application/vnd.fs2.scientific-manifest+json")
        parameters = json.loads((ROOT / "acceptance/lerobot-customer-20260917/lighting.json").read_bytes())
        parameters["source"] = {"kind": "uploaded-bundle", **bundle}
        request = {"schema": "fs2-serve.nebius.ai/scientific-run-request/v1", "operation": "augment-lerobot-dataset",
                   "service_class": "customer-batch", "input_manifest": manifest_ref, "parameters": parameters}
        save(model, {"model_id": model, "request": request,
                     "attribution": "Public NVIDIA Cosmos3 Agibot example transformed into two-episode/two-camera LeRobot 0.6.1 fixture; not patient/customer data"},
             "artifact-lerobot-v1", "lerobot-v3-single-camera-lighting-variant")


if __name__ == "__main__":
    main()

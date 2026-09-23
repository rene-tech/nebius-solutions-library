"""CPU-only, credential-free regeneration of a frozen four-engine delivery.

Run only after the package owner declares the bundle complete. The container
sees the read-only delivered bundle and one fresh output directory, not the
original host source/data trees or the reference analysis used after execution.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time


IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/lc@sha256:81a2f3b54a98299933d487ccca4e9eb3fbea3130257a5a818a83940127429a4d"
ENGINES = ("gromacs", "namd", "amber", "lammps")
METADATA = {"inputs", "provenance", "lifecycle_timings", "pressure_observation_provenance"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_record(path):
    path = Path(path)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)}


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def equal_science(actual, expected, differences, location="root"):
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and actual.keys() == expected.keys(), "scientific fields differ: " + location)
        for key in expected:
            if key == "next_segment_file":  # Relocated bundle path, not science.
                continue
            equal_science(actual[key], expected[key], differences, location + "." + key)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(actual) == len(expected), "scientific list differs: " + location)
        for i, (left, right) in enumerate(zip(actual, expected)):
            equal_science(left, right, differences, location + f"[{i}]")
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        require(isinstance(actual, (int, float)) and math.isfinite(actual) and math.isfinite(expected), "nonfinite numeric result: " + location)
        require(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10), "scientific value differs: " + location)
        differences["numeric_values_compared"] += 1
        delta = abs(actual - expected)
        differences["maximum_absolute_difference"] = max(differences["maximum_absolute_difference"], delta)
        differences["non_bit_identical_numeric_values"] += int(actual != expected)
    else:
        require(actual == expected, "scientific value differs: " + location)


def compare_csv(actual, expected, differences):
    def rows(path):
        with Path(path).open(newline="") as stream:
            result = list(csv.reader(stream))
        for row in result[1:]:
            for i, value in enumerate(row):
                try:
                    row[i] = float(value)
                except ValueError:
                    pass
        return result
    equal_science(rows(actual), rows(expected), differences, Path(actual).name)


def verify_package(bundle):
    receipt = read(bundle / "analysis-inputs/packaging-receipt.json")
    require(receipt["path_base"] == "spec-directory", "analysis spec is not portable")
    for item in receipt["files"]:
        path = (bundle / item["path"]).resolve(strict=True)
        require(path.is_relative_to(bundle), "packaged file resolves outside bundle")
        require(path.stat().st_size == item["bytes"] and digest(path) == item["sha256"], "packaged dependency changed: " + item["path"])
    require(digest(bundle / "analysis-inputs/spec.json") == receipt["portable_spec_sha256"], "portable spec changed")
    return file_record(bundle / "analysis-inputs/packaging-receipt.json")


def verify_outputs(output, reference):
    analysis = read(output / "analysis/receipt.json")
    render = read(output / "videos/receipt.json")
    require(analysis["status"] == "analysis-passed" and not analysis["missing_engines"], "four-engine analysis incomplete")
    require(render["status"] == "real-trajectories-rendered-and-encoding-validated" and not render["missing_engines"], "four-engine video rendering incomplete")
    require(set(render["videos"]) == {*ENGINES, "four-engine-2x2"}, "missing clip/grid")
    require(render["settings"]["frame_interval_ps"] == 1 and render["settings"]["playback_ps_per_second"] == 40, "changed playback sampling")
    differences = {"numeric_values_compared": 0, "maximum_absolute_difference": 0., "non_bit_identical_numeric_values": 0}
    runs = []
    for engine in ENGINES:
        actual = read(output / "analysis" / engine / "summary.json")
        expected = read(reference / engine / "summary.json")
        require(actual["common_frame_count"] == 1000 and actual["production_steps"] == 500000 and
                actual["production_duration_ps"] == 1000 and actual["first_common_time_ps"] == 1 and actual["last_time_ps"] == 1000,
                "incomplete native production trajectory: " + engine)
        equal_science({k: v for k, v in actual.items() if k not in METADATA},
                      {k: v for k, v in expected.items() if k not in METADATA}, differences, engine)
        for filename in ("frames.csv", "thermodynamics.csv"):
            compare_csv(output / "analysis" / engine / filename, reference / engine / filename, differences)
        runs.append({"engine": engine, "frames": actual["frame_count"], "common_frames": actual["common_frame_count"],
                     "temperature_mean_K": actual["temperature_K"]["mean"], "pressure_mean_bar": actual["pressure_bar"]["mean"],
                     "density_mean_g_cm3": actual["density_from_native_cells_g_cm3"]["mean"],
                     "native_ns_per_day": actual["performance"]["native_ns_per_day"]})
    compare_csv(output / "analysis/comparison.csv", reference / "comparison.csv", differences)
    # Independently decode every rendered clip, including the actual 2x2 grid.
    videos = {}
    for name in (*ENGINES, "four-engine-2x2"):
        path = output / "videos" / (name + ".mp4")
        command = ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                   "stream=codec_name,width,height,r_frame_rate,nb_read_frames,duration,pix_fmt", "-of", "json", str(path)]
        probe = json.loads(subprocess.check_output(command, text=True))["streams"][0]
        size = 1440 if name == "four-engine-2x2" else 720
        require(int(probe["nb_read_frames"]) == 1000 and probe["width"] == probe["height"] == size and
                probe["r_frame_rate"] == "40/1" and math.isclose(float(probe["duration"]), 25, abs_tol=.001), "video synchronization/coverage differs")
        videos[name] = {**probe, **file_record(path)}
    return {"runs": runs, "scientific_value_comparison": differences, "video_probes": videos,
            "source_analysis_receipt": file_record(reference / "receipt.json"),
            "regenerated_analysis_receipt": file_record(output / "analysis/receipt.json"),
            "regenerated_render_receipt": file_record(output / "videos/receipt.json")}


def run(bundle, reference, output):
    bundle, reference, output = bundle.resolve(), reference.resolve(), output.resolve()
    require(not output.exists(), "preserve previous evidence; output must be new")
    require(not output.is_relative_to(bundle), "output must not modify the delivery bundle")
    before = verify_package(bundle)
    require(read(reference / "receipt.json")["status"] == "analysis-passed", "reference is not complete")
    output.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    record = {"schema": "fs2-serve.nebius.ai/portable-md-analysis-qualification/v1", "status": "incomplete",
              "recorded_at": datetime.now(timezone.utc).isoformat(), "image": IMAGE, "customer_ready": False,
              "scope": "CPU-only portable regeneration of the four existing native trajectories and five real-coordinate videos; no simulation, API call or browser acceptance",
              "packaging_receipt": before, "helper_source": file_record(Path(__file__).resolve()),
              "network": "none", "gpus_requested": 0, "api_credentials_supplied": False,
              "reference_tree_mounted": False, "original_source_or_data_trees_mounted": False}
    container = None
    try:
        command = ["docker", "create", "--network", "none", "--runtime", "runc", "--cpus", "4", "--memory", "8g",
                   "--user", f"{os.getuid()}:{os.getgid()}",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
                   "--workdir", "/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "MPLCONFIGDIR=/tmp/matplotlib",
                   "--mount", f"type=bind,source={bundle},target=/delivery,readonly",
                   "--mount", f"type=bind,source={output},target=/validation", "--entrypoint", "/opt/md-analysis/bin/python", IMAGE,
                   "/delivery/analysis-inputs/regenerate.py", "--analysis-output", "/validation/analysis", "--video-output", "/validation/videos"]
        record["command"] = command
        container = subprocess.check_output(command, text=True).strip()
        config = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        host = config["HostConfig"]
        require(host["NetworkMode"] == "none" and host["Runtime"] == "runc" and not host.get("DeviceRequests") and not host["Devices"], "container isolation differs")
        require({m["Destination"] for m in config["Mounts"] if m["Type"] == "bind"} == {"/delivery", "/validation"}, "unexpected host mount")
        require(not next(m for m in config["Mounts"] if m["Destination"] == "/delivery")["RW"], "delivery mount is writable")
        record["container"] = {"id": container, "image_id": config["Image"], "user": config["Config"]["User"], "network_mode": host["NetworkMode"],
                               "runtime": host["Runtime"], "read_only_rootfs": host["ReadonlyRootfs"],
                               "mounts": config["Mounts"], "device_requests": host.get("DeviceRequests"), "devices": host["Devices"]}
        with (output / "regenerate.log").open("w") as log:
            result = subprocess.run(["docker", "start", "--attach", container], stdout=log, stderr=subprocess.STDOUT, timeout=1800)
        record["exit_code"] = result.returncode
        require(result.returncode == 0, "portable regeneration failed; see retained regenerate.log")
        record.update(verify_outputs(output, reference))
        require(verify_package(bundle) == before, "delivery package changed during validation")
        record["generated_files"] = [file_record(path) for path in sorted(output.rglob("*")) if path.is_file()]
        record["status"] = "passed"
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if container:
            removed = subprocess.run(["docker", "rm", container], capture_output=True, text=True)
            if removed.returncode:
                # A timeout of the attached client can leave its owned CPU
                # container running. Never stop anything except the exact ID
                # returned by this invocation's docker create.
                subprocess.run(["docker", "stop", "--time", "5", container], capture_output=True, text=True)
                removed = subprocess.run(["docker", "rm", container], capture_output=True, text=True)
            record["task_container_removed"] = removed.returncode == 0
            if removed.returncode:
                record.update(status="failed", error="could not remove the task-owned CPU container")
        record["elapsed_seconds"] = time.monotonic() - started
        write(output / "qualification.json", record)
    require(record["status"] == "passed", record.get("error", "qualification incomplete"))
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("bundle", "reference-analysis", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run(args.bundle, args.reference_analysis, args.output)
    print(json.dumps({"status": result["status"], "runs": result["runs"], "elapsed_seconds": result["elapsed_seconds"]}))

"""One ordered 12-case HTTP replay against immutable r6 first-pass outputs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

sys.path[:0] = ["/opt/fs2/runtime", "/opt/fs2/model/upstream", "/qualification"]
from common import server  # noqa: E402 - exact candidate image paths
from seed_qualify import coordinates, saved  # noqa: E402 - pinned mounted helper


def compare(result, reference):
    blocks, confidence = result["ligand_positions"], result["position_confidence"]
    if len(blocks) != 4 or len(confidence) != 4 or len(reference["poses"]) != 4:
        raise ValueError("four-pose contract changed")
    if any("3D" not in block.splitlines()[1] for block in blocks):
        raise ValueError("generated SDF lacks 3D metadata")
    delta = max(
        float(np.max(np.abs(coordinates(block) - coordinates(prior["sdf"]))))
        for block, prior in zip(blocks, reference["poses"], strict=True)
    )
    scores = max(abs(value - prior["confidence"]) for value, prior in zip(confidence, reference["poses"], strict=True))
    return {
        "maximum_coordinate_difference_angstrom": delta,
        "maximum_confidence_difference": scores,
        "numerical_repeatability_pass": delta <= 0.01 and scores <= 0.001,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases_raw = gzip.decompress(args.cases.read_bytes())
    references_raw = gzip.decompress(args.references.read_bytes())
    cases, references = json.loads(cases_raw), json.loads(references_raw)
    if len(cases) != 12 or set(references) != {case["case_id"] for case in cases}:
        raise ValueError("exact twelve-case reference matrix required")
    pod_uid = str(uuid.UUID(os.environ["FS2_RUNTIME_POD_UID"]))
    args.output.mkdir(parents=True, exist_ok=False)
    server.STATE.load()
    if server.STATE.load_state != "ready":
        raise RuntimeError("candidate failed to load")
    httpd = server.BoundedHTTPServer(("127.0.0.1", 8000), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    runs = []
    try:
        for case in cases:
            operation = str(uuid.uuid4())
            request = json.dumps(case["arguments"], sort_keys=True, separators=(",", ":")).encode()
            connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=120)
            started = time.monotonic()
            try:
                connection.request(
                    "POST",
                    "/molecular-docking/diffdock/generate",
                    body=request,
                    headers={
                        "Content-Type": "application/json",
                        "X-FS2-Operation-Id": operation,
                        "X-Request-Id": operation + ":1",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
                observed = {
                    key: response.getheader(key)
                    for key in ("X-FS2-Runtime-Pod-Uid", "X-FS2-Runtime-Operation-Id", "X-FS2-Runtime-Attempt")
                }
                if response.status != 200:
                    raise RuntimeError("HTTP candidate failed: " + str(response.status))
                expected = {
                    "X-FS2-Runtime-Pod-Uid": pod_uid,
                    "X-FS2-Runtime-Operation-Id": operation,
                    "X-FS2-Runtime-Attempt": "1",
                }
                if observed != expected:
                    raise RuntimeError("response identity differs from actual Pod/operation/attempt")
                result = json.loads(raw)
            finally:
                connection.close()
            name = case["case_id"] + "-repeat1.json"
            output_sha = saved(args.output / name, result)
            row = {
                "case_id": case["case_id"],
                "input_sha256": hashlib.sha256(request).hexdigest(),
                "http_status": 200,
                "identity_headers": observed,
                "seconds": time.monotonic() - started,
                "result_file": name,
                "result_sha256": output_sha,
                **compare(result, references[case["case_id"]]),
            }
            runs.append(row)
            print(
                json.dumps({"event": "result_artifact", "filename": name, "document": result}, allow_nan=False),
                flush=True,
            )
            print(json.dumps({"event": "inference_completed", **row}, allow_nan=False), flush=True)
        receipt = {
            "schema": "fs2-diffdock-wrapper-http-regression/v1",
            "requests": len(runs),
            "runs": runs,
            "passed": len(runs) == 12 and all(row["numerical_repeatability_pass"] for row in runs),
            "model_load_seconds": server.STATE.load_seconds,
            "pod_uid": pod_uid,
            "gpu": subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,uuid,driver_version", "--format=csv,noheader"], text=True
            ).strip(),
            "cases_sha256": hashlib.sha256(cases_raw).hexdigest(),
            "references_sha256": hashlib.sha256(references_raw).hexdigest(),
            "wrapper_sha256": hashlib.sha256(Path(server.__file__).read_bytes()).hexdigest(),
            "adapter_identity": server.STATE.adapter.identity,
            "coordinate_tolerance_angstrom": 0.01,
            "confidence_tolerance": 0.001,
            "scope": (
                "One ordered pass through actual wrapper, compared with r6 first pass; "
                "not all-input scientific accuracy or public attribution."
            ),
        }
        saved(args.output / "receipt.json", receipt)
        print(json.dumps({"event": "qualification_completed", **receipt}, allow_nan=False), flush=True)
        return 0 if receipt["passed"] else 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run two distinct same-backend semantic requests and retain bounded evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pdb(residues: int = 32) -> str:
    lines: list[str] = []
    serial = 1
    for residue in range(1, residues + 1):
        angle = math.radians((residue - 1) * 100.0)
        center = (5.0 * math.cos(angle), 5.0 * math.sin(angle), 1.5 * residue)
        atoms = (
            ("N", center[0] - 0.6, center[1] - 0.4, center[2] - 0.5, "N"),
            ("CA", center[0], center[1], center[2], "C"),
            ("C", center[0] + 0.7, center[1] + 0.3, center[2] + 0.5, "C"),
            ("O", center[0] + 1.2, center[1] + 0.5, center[2] + 1.2, "O"),
        )
        for name, x, y, z, element in atoms:
            lines.append(
                f"ATOM  {serial:5d} {name:^4s} ALA A{residue:4d}    "
                f"{x:8.3f}{y:8.3f}{z:8.3f}  1.00 20.00          {element:>2s}"
            )
            serial += 1
    lines.extend(["TER", "END", ""])
    return "\n".join(lines)


def _requests(model: str) -> list[dict[str, Any]]:
    if model == "proteinmpnn":
        protein = _pdb()
        return [
            {"input_pdb": protein, "input_pdb_chains": ["A"], "random_seed": seed, "num_seq_per_target": 1}
            for seed in (1701, 2303)
        ]
    if model == "diffdock":
        assets = Path(__file__).resolve().parent / "assets"
        requests = [
            json.loads((assets / f"diffdock-b300-request-{index}.json").read_text(encoding="utf-8"))
            for index in (1, 2)
        ]
        protein = (assets / "diffdock-b300-protein.pdb").read_text(encoding="ascii")
        for request in requests:
            request["protein"] = protein
        return requests
    raise ValueError(model)


def _call(url: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 900) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "X-Request-Id": f"smoke-{time.time_ns()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw), {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = {"error": {"code": "non_json_response", "body_sha256": _sha(raw)}}
        return exc.code, value, {key.lower(): value for key, value in exc.headers.items()}


def _wait_ready(base_url: str, timeout_seconds: int) -> tuple[dict[str, Any], dict[str, str]]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            status, value, _headers = _call(base_url + "/readyz", timeout=5)
            last = value
            if status == 200 and value.get("status") == "ready":
                status, identity, headers = _call(base_url + "/identity", timeout=10)
                if status != 200:
                    raise RuntimeError("identity endpoint failed after readiness")
                return identity, headers
        except (OSError, ValueError):
            pass
        time.sleep(5)
    raise TimeoutError(f"runtime did not become ready: {last}")


def _validate(model: str, response: dict[str, Any]) -> dict[str, Any]:
    output = response.get("output")
    if not isinstance(output, dict):
        raise ValueError("response output is absent")
    if model == "proteinmpnn":
        sequences = output.get("sequences")
        if not isinstance(sequences, list) or len(sequences) != 1:
            raise ValueError("one ProteinMPNN sequence is required")
        sequence = sequences[0].get("sequence", "").replace("/", "")
        if len(sequence) != output.get("native_length") or set(sequence) - set("ACDEFGHIKLMNPQRSTVWY"):
            raise ValueError("ProteinMPNN sequence failed alphabet/length validation")
        for name in ("score", "global_score"):
            if not math.isfinite(float(sequences[0][name])):
                raise ValueError("ProteinMPNN score is non-finite")
        return {"sequence_sha256": _sha(sequence.encode()), "length": len(sequence)}
    if model == "diffdock":
        poses = output.get("poses")
        if not isinstance(poses, list) or len(poses) != 1:
            raise ValueError("one DiffDock pose is required")
        pose = poses[0]
        sdf = pose.get("sdf", "")
        if "V2000" not in sdf or "M  END" not in sdf or not math.isfinite(float(pose["confidence"])):
            raise ValueError("DiffDock SDF/confidence semantic validation failed")
        atom_lines = [line for line in sdf.splitlines() if len(line) >= 34 and line[31:34].strip()]
        return {"sdf_sha256": _sha(sdf.encode()), "confidence": pose["confidence"], "atom_lines": len(atom_lines)}
    raise ValueError(model)


def run(model: str, base_url: str, evidence_dir: Path, ready_timeout: int) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC).isoformat()
    result: dict[str, Any] = {
        "schema": "fs2-serve.nebius.ai/open-runtime-smoke/v1",
        "model": model,
        "started_at": started,
        "base_url": base_url,
        "attempts": [],
        "failures": 0,
    }
    try:
        identity, identity_headers = _wait_ready(base_url, ready_timeout)
        backend = identity.get("backend_id")
        if not backend or identity.get("routing_state") != "disabled":
            raise ValueError("identity does not prove a disabled route and stable backend")
        result["identity"] = identity
        result["identity_backend_header"] = identity_headers.get("x-backend-id")
        requests = _requests(model)
        request_hashes = {_sha(json.dumps(value, sort_keys=True).encode()) for value in requests}
        if len(request_hashes) != 2:
            raise ValueError("semantic requests are not distinct")
        for index, payload in enumerate(requests, 1):
            attempt: dict[str, Any] = {
                "attempt": index,
                "request_sha256": _sha(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()),
                "success": False,
            }
            call_started = time.monotonic()
            try:
                status, response, headers = _call(base_url + "/v1/infer", "POST", payload)
                attempt["http_status"] = status
                attempt["response_sha256"] = _sha(json.dumps(response, sort_keys=True, separators=(",", ":")).encode())
                attempt["elapsed_seconds"] = time.monotonic() - call_started
                if status != 200:
                    raise ValueError(f"inference returned HTTP {status}: {response.get('error', {}).get('code')}")
                if response.get("backend_id") != backend or headers.get("x-backend-id") != backend:
                    raise ValueError("request did not use the admitted backend")
                attempt["semantic"] = _validate(model, response)
                attempt["model_seconds"] = response.get("timings", {}).get("model_seconds")
                attempt["success"] = True
            except Exception as exc:  # retain every failed denominator without raw payload
                attempt["failure_type"] = type(exc).__name__
                attempt["failure"] = str(exc)[:240]
                result["failures"] += 1
            result["attempts"].append(attempt)
    except Exception as exc:
        result["setup_failure_type"] = type(exc).__name__
        result["setup_failure"] = str(exc)[:240]
        result["failures"] += 1
    result["completed_at"] = datetime.now(UTC).isoformat()
    result["qualified"] = len(result["attempts"]) == 2 and result["failures"] == 0
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    (evidence_dir / "smoke.json").write_text(output, encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("proteinmpnn", "diffdock"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--ready-timeout", type=int, default=2700)
    args = parser.parse_args()
    result = run(args.model, args.base_url.rstrip("/"), args.evidence_dir, args.ready_timeout)
    print(json.dumps({"model": args.model, "qualified": result["qualified"], "failures": result["failures"]}))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

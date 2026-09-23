"""Run typed native AMBER stages; checkpoint only a fully stopped workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fs2_gromacs.files import atomic_json, digest_file, extract_inputs, inventory

from . import ENGINE_ID, PMEMD_SOURCE_SHA256, RESULT_SCHEMA
from .contracts import canonical, normalize
from .validation import validate_pmemd, validate_tool_log

STATE_SCHEMA = "fs2-serve.nebius.ai/amber-checkpoint/v1"
PMEMD_BINARIES = {"cpu": "pmemd", "cuda-spfp": "pmemd.cuda_SPFP", "cuda-dpfp": "pmemd.cuda_DPFP"}


def utc():
    return datetime.now(timezone.utc).isoformat()


def native_argv(step, binary):
    if step["kind"] == "pmemd":
        prefix = step["output_prefix"]
        command = [binary, "-O", "-i", step["input"], "-p", step["topology"], "-c", step["coordinates"]]
        for flag, suffix in (("-o", ".mdout"), ("-r", ".rst7"), ("-x", ".nc"), ("-v", ".mdvel"), ("-e", ".mden"), ("-inf", ".mdinfo")):
            command += [flag, prefix + suffix]
        if "reference" in step:
            command += ["-ref", step["reference"]]
        return command
    if step["kind"] == "tleap":
        return [binary, "-f", step["input"]]
    command = [binary, "-i", step["input"]]
    if "topology" in step:
        command += ["-p", step["topology"]]
    if "coordinates" in step:
        command += ["-c", step["coordinates"]]
    return command


class Workflow:
    def __init__(self, request, *, job_id, operation_id, workspace, binaries=None, checkpoint_mode="local"):
        self.request = normalize(request)
        self.job = next(job for job in self.request["jobs"] if job["id"] == job_id)
        self.operation_id = operation_id
        self.root = Path(workspace).resolve()
        self.data, self.meta = self.root / "data", self.root / ".fs2"
        self.binaries = binaries or {}
        self.checkpoint_mode = checkpoint_mode
        self.recipe = hashlib.sha256(canonical({"request": self.request, "job": job_id, "image": ENGINE_ID})).hexdigest()
        self.started = time.monotonic()
        self.deadline = self.started + self.request["max_wall_seconds"]
        self.stopped, self.child = False, None
        self.state = {"schema": STATE_SCHEMA, "operation_id": operation_id, "job_id": job_id, "recipe_sha256": self.recipe, "generation": 0, "completed_steps": [], "commands": [], "elapsed_seconds": 0}
        self.version, self.gpu = "initialization incomplete", None

    def binary(self, step):
        key = step["backend"] if step["kind"] == "pmemd" else step["kind"]
        if key in self.binaries:
            return self.binaries[key]
        if step["kind"] == "pmemd":
            return "/opt/amber26/bin/" + PMEMD_BINARIES[key]
        return str(Path(os.environ.get("AMBERTOOLS_HOME", "/opt/ambertools")) / "bin" / key)

    def environment(self, step=None):
        env = {**os.environ, "OMP_NUM_THREADS": str(self.request["threads"]), "OPENBLAS_NUM_THREADS": str(self.request["threads"])}
        if step and step["kind"] != "pmemd":
            home = os.environ.get("AMBERTOOLS_HOME", "/opt/ambertools")
            env.update(AMBERHOME=home, PATH=home + "/bin:/usr/bin:/bin", LD_LIBRARY_PATH=home + "/lib")
        return env

    def stop(self, signum, frame):
        self.stopped = True
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _wait_json(self, path, predicate, *, seconds=600):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stopped:
                raise InterruptedError("interrupted; restore the preceding committed stage")
            if (self.meta / "transport-error.json").is_file():
                raise RuntimeError("durable checkpoint transport failed; see operation logs")
            if path.is_file():
                value = json.loads(path.read_text())
                if predicate(value):
                    return value
            time.sleep(0.25)
        raise TimeoutError("durable checkpoint handoff timed out")

    def initialize(self):
        self.meta.mkdir(parents=True, exist_ok=True)
        if self.checkpoint_mode == "companion":
            self._wait_json(self.meta / "restore-complete.json", lambda value: value.get("status") == "ready")
        if (previous := self.meta / "amber-state.json").is_file():
            state = json.loads(previous.read_text())
            if (state.get("schema"), state.get("recipe_sha256"), state.get("operation_id"), state.get("job_id")) != (STATE_SCHEMA, self.recipe, self.operation_id, self.job["id"]):
                raise ValueError("checkpoint belongs to another operation, job or engine recipe")
            completed = state.get("completed_steps", [])
            if completed != [step["id"] for step in self.job["steps"]][:len(completed)]:
                raise ValueError("checkpoint completed stages must be a prefix of the frozen workflow")
            self.state = state
            self.deadline -= float(state["elapsed_seconds"])
        if not self.data.exists():
            extract_inputs(self.root / "input.tar.gz", self.data, max_bytes=self.request["max_output_bytes"])
        if not self.binaries:
            if any(step["kind"] == "pmemd" and step["backend"] != "cpu" for step in self.job["steps"]):
                self.gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,uuid,driver_version,compute_cap", "--format=csv,noheader"], text=True, timeout=15).strip()
                rows = self.gpu.splitlines()
                if len(rows) != 1 or rows[0].rsplit(",", 1)[-1].strip() not in {"8.9", "9.0"}:
                    raise ValueError("PMEMD shape requires exactly one H100 SM90 or L40S SM89 GPU")
            self.version = subprocess.check_output(["/opt/amber26/bin/pmemd", "--version"], env=self.environment(), text=True, stderr=subprocess.STDOUT, timeout=30).strip()
        else:
            self.version = "test-only native binary injection"
        atomic_json(self.meta / "engine.json", {"engine_id": ENGINE_ID, "pmemd_source_sha256": PMEMD_SOURCE_SHA256, "version": self.version, "gpu": self.gpu})
        if self.state["generation"] == 0:
            self.state["input_files"] = inventory(self.data, max_bytes=self.request["max_output_bytes"])
            self.checkpoint()

    def checkpoint(self):
        if self.child is not None:
            raise RuntimeError("cannot commit while a native process is running")
        self.state["elapsed_seconds"] += time.monotonic() - self.started
        self.started = time.monotonic()
        files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
        if any(item["size_bytes"] > 5 * 1024**3 for item in files):
            raise ValueError("individual file exceeds the qualified 5 GiB transport limit")
        self.state["generation"] += 1
        atomic_json(self.meta / "amber-state.json", self.state)
        atomic_json(self.meta / "checkpoint-ready.json", {"schema": "fs2-serve.nebius.ai/amber-checkpoint-ready/v1", "state": self.state, "files": files})
        if self.checkpoint_mode == "companion":
            generation = self.state["generation"]
            self._wait_json(self.meta / "checkpoint-ack.json", lambda value: value.get("generation") == generation and value.get("status") == "committed")

    def execute(self, command, *, step, cwd, log):
        remaining = self.deadline - time.monotonic()
        if self.stopped or remaining <= 0:
            raise InterruptedError("execution budget exhausted or workflow interrupted")
        start = time.monotonic()
        with log.open("xb") as output:
            self.child = subprocess.Popen(command, cwd=cwd, env=self.environment(step), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                self.child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self.stop(signal.SIGTERM, None)
                try:
                    self.child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait()
            code = self.child.returncode
            self.child = None
        return code, time.monotonic() - start

    def run_step(self, step):
        cwd = self.data / step["directory"]
        if not cwd.is_dir() or cwd.is_symlink():
            raise ValueError("native step directory must exist in the input workspace")
        inputs = {}
        for key in ("input", "topology", "coordinates", "reference"):
            if key not in step:
                continue
            path = cwd / step[key]
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise ValueError(f"missing or empty native {key}: {step[key]}")
            inputs[key] = {"path": str(path.relative_to(self.data)), "sha256": digest_file(path)}
        command = native_argv(step, self.binary(step))
        log = cwd / f"fs2-{step['id']}.stdout.log"
        if step["kind"] == "pmemd":
            (cwd / step["output_prefix"]).parent.mkdir(parents=True, exist_ok=True)
        code, elapsed = self.execute(command, step=step, cwd=cwd, log=log)
        receipt = {"step_id": step["id"], "kind": step["kind"], "backend": step.get("backend"), "argv": command, "inputs": inputs, "log": str(log.relative_to(self.data)), "exit_code": code, "wall_seconds": elapsed, "finished_at": utc()}
        self.state["commands"].append(receipt)
        if self.stopped:
            raise InterruptedError("native process interrupted; preceding committed stage retained")
        if code:
            raise RuntimeError(f"native {step['kind']} stage {step['id']} exited {code}; see {log.name}")
        for name in step["expected_outputs"]:
            path = cwd / name
            if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
                raise ValueError(f"missing or empty expected native output: {name}")
        receipt["validation"] = validate_pmemd(cwd, step) if step["kind"] == "pmemd" else validate_tool_log(log, step["kind"])
        self.state["completed_steps"].append(step["id"])
        self.checkpoint()

    def run(self):
        status, error = "succeeded", None
        try:
            self.initialize()
            for step in self.job["steps"]:
                if step["id"] not in self.state["completed_steps"]:
                    self.run_step(step)
        except Exception as exc:
            status = "interrupted" if self.stopped or isinstance(exc, InterruptedError) else "failed"
            error = str(exc)
        try:
            files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
            inventory_error = None
        except Exception as exc:
            files, inventory_error = [], str(exc)
            status, error = "failed", error or inventory_error
        result = {"schema": RESULT_SCHEMA, "operation_id": self.operation_id, "job_id": self.job["id"], "status": status, "error": error, "recipe_sha256": self.recipe, "engine_id": ENGINE_ID, "pmemd_source_sha256": PMEMD_SOURCE_SHA256, "engine": self.version, "gpu": self.gpu, "completed_steps": self.state["completed_steps"], "commands": self.state["commands"], "native_checkpoint_generation": self.state["generation"], "gpu_snapshot_used": False, "retry_semantics": "active stage rerun from preceding committed full workspace; no automatic partial-restart or exact stochastic-state claim", "finished_at": utc(), "files": files, "inventory_error": inventory_error}
        atomic_json(self.root / "result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--checkpoint-mode", choices=("local", "companion"), default="companion")
    args = parser.parse_args()
    worker = Workflow(json.loads(args.request.read_text()), job_id=args.job_id, operation_id=args.operation_id, workspace=args.workspace, checkpoint_mode=args.checkpoint_mode)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    result = worker.run()
    print(json.dumps({key: result[key] for key in ("operation_id", "job_id", "status", "error")}))
    raise SystemExit(0 if result["status"] == "succeeded" else 143 if result["status"] == "interrupted" else 1)


if __name__ == "__main__":
    main()

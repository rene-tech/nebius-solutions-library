"""Execute native LAMMPS stages and publish only closed coherent workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fs2_gromacs.files import atomic_json, digest_file, extract_inputs, inventory

from . import ENGINE_ID, RESULT_SCHEMA
from .contracts import canonical, normalize

STATE_SCHEMA = "fs2-serve.nebius.ai/lammps-checkpoint/v1"


class Interrupted(RuntimeError):
    """An incomplete job, never scientific success."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def restart_step(text):
    """Parse native restart2info output (qualified against the pinned image)."""
    for pattern in (r"(?im)^\s*Timestep\s*[=:]\s*(\d+)\s*$", r"(?im)^\s*timestep\s+(\d+)\s*$"):
        if match := re.search(pattern, text):
            return int(match.group(1))
    raise ValueError("native restart metadata has no independently readable timestep")


class Workflow:
    def __init__(self, request, *, job_id, operation_id, workspace, binary=None, checkpoint_mode="local"):
        self.request = normalize(request)
        self.job = next(j for j in self.request["jobs"] if j["id"] == job_id)
        self.operation_id = operation_id
        self.root = Path(workspace).resolve()
        self.data, self.meta = self.root / "data", self.root / ".fs2"
        self.binary = binary
        self.build = None
        self.checkpoint_mode = checkpoint_mode
        self.engine_id = os.environ.get("FS2_LAMMPS_ENGINE_ID", ENGINE_ID)
        self.recipe = hashlib.sha256(canonical({"request": self.request, "job": job_id, "image": self.engine_id})).hexdigest()
        self.started = time.monotonic()
        self.deadline = self.started + self.request["max_wall_seconds"]
        self.stopped = False
        self.child = None
        self.state = {"schema": STATE_SCHEMA, "operation_id": operation_id, "job_id": job_id, "recipe_sha256": self.recipe, "generation": 0, "completed_steps": [], "commands": [], "active_step": None, "elapsed_seconds": 0}
        self.version = "unavailable: initialization did not complete"

    def engine(self, backend):
        return self.binary or os.environ.get("FS2_LAMMPS_BINARY", "/usr/local/lammps/" + (self.build or "sm90") + "/bin/lmp")

    def environment(self):
        env = {**os.environ, "OMP_NUM_THREADS": str(self.request["threads"])}
        if self.build:
            env["LD_LIBRARY_PATH"] = "/usr/local/lammps/" + self.build + "/lib:" + env.get("LD_LIBRARY_PATH", "")
        return env

    def stop(self, signum, frame):
        self.stopped = True
        if self.child is not None and self.child.poll() is None:
            # LAMMPS does not promise signal-safe final restart writes. Retain
            # the previously committed generation, never publish partial files.
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _wait_json(self, path, predicate, *, seconds=600):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stopped:
                raise Interrupted("interrupted; recover the last committed remote generation")
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
            self._wait_json(self.meta / "restore-complete.json", lambda v: v.get("status") == "ready")
        if (previous := self.meta / "lammps-state.json").is_file():
            state = json.loads(previous.read_text())
            if (state.get("schema"), state.get("recipe_sha256"), state.get("operation_id"), state.get("job_id")) != (STATE_SCHEMA, self.recipe, self.operation_id, self.job["id"]):
                raise ValueError("checkpoint belongs to another operation, job or engine recipe")
            self.state = state
            self.deadline -= float(state["elapsed_seconds"])
        if not self.data.exists():
            extract_inputs(self.root / "input.tar.gz", self.data, max_bytes=self.request["max_output_bytes"])
        if not self.binary:
            if any(s["backend"] != "cpu" for s in self.job["steps"]):
                capability = subprocess.check_output(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], text=True, timeout=15).strip().splitlines()
                if len(capability) != 1 or capability[0] not in {"8.9", "9.0"}:
                    raise ValueError("this execution shape requires one qualified L40S SM89 or H100 SM90 GPU")
                self.build = {"8.9": "sm86", "9.0": "sm90"}[capability[0]]
            else:
                self.build = "sm90"
        versions = {}
        for backend in sorted({s["backend"] for s in self.job["steps"]}):
            versions[backend] = subprocess.check_output([self.engine(backend), "-h"], env=self.environment(), stderr=subprocess.STDOUT, text=True, timeout=30)
        self.version = versions
        atomic_json(self.meta / "engine.json", {"engine_id": self.engine_id, "build": self.build, "versions": versions})
        if self.state["generation"] == 0:
            self.state["input_files"] = inventory(self.data, max_bytes=self.request["max_output_bytes"])
            self.checkpoint()

    def checkpoint(self):
        if self.child is not None:
            raise RuntimeError("cannot commit while the native process is running")
        self.state["elapsed_seconds"] += time.monotonic() - self.started
        self.started = time.monotonic()
        files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
        if any(f["size_bytes"] > 5 * 1024**3 for f in files):
            raise ValueError("individual file exceeds the qualified 5 GiB transport limit")
        self.state["generation"] += 1
        atomic_json(self.meta / "lammps-state.json", self.state)
        atomic_json(self.meta / "checkpoint-ready.json", {"schema": "fs2-serve.nebius.ai/lammps-checkpoint-ready/v1", "state": self.state, "files": files})
        if self.checkpoint_mode == "companion":
            generation = self.state["generation"]
            self._wait_json(self.meta / "checkpoint-ack.json", lambda v: v.get("generation") == generation and v.get("status") == "committed")

    def execute(self, command, *, cwd, log):
        remaining = self.deadline - time.monotonic()
        if self.stopped or remaining <= 0:
            raise Interrupted("execution budget exhausted or workflow interrupted")
        start = time.monotonic()
        with log.open("xb") as output:
            self.child = subprocess.Popen(command, cwd=cwd, env=self.environment(), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
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

    def read_restart_step(self, path, backend):
        text = subprocess.check_output([self.engine(backend), "-log", "none", "-restart2info", str(path)], env=self.environment(), cwd=self.meta, stderr=subprocess.STDOUT, text=True, timeout=60)
        return restart_step(text)

    def run_step(self, step):
        cwd = self.data / step["directory"]
        if not cwd.is_dir() or cwd.is_symlink():
            raise ValueError("native step directory must exist in the input workspace")
        c = step.get("continuation")
        prior = self.state["active_step"]
        segment = prior["segment"] if prior and prior["id"] == step["id"] else 0
        prior_progress = prior.get("progress", -1) if segment else -1
        script = step["input"] if segment == 0 else c["input"]
        while True:
            if not (cwd / script).is_file():
                raise ValueError("native input or complete continuation script is missing")
            if segment and (not c or not (cwd / c["restart_file"]).is_file()):
                raise ValueError("committed native restart is missing; refusing a fresh start")
            segment += 1
            command = [self.engine(step["backend"])]
            if step["backend"] == "kokkos-cuda":
                command += ["-k", "on", "g", "1", "t", str(self.request["threads"]), "-sf", "kk"]
            variables = {**step["variables"], "fs2_segment_seconds": str(min(self.request["segment_seconds"], max(1, int(self.deadline - time.monotonic()) - 15))), "fs2_segment": str(segment), "fs2_restart": "1" if segment > 1 else "0"}
            for name, value in variables.items():
                command += ["-var", name, value]
            log_name = f"fs2-{step['id']}-segment-{segment:06d}.log"
            # Logs are append-only segment artifacts; native log.lammps is off
            # unless the scientific script intentionally opens its own log.
            command += ["-log", "none", "-in", script]
            progress_path = cwd / c["progress_file"] if c else None
            if progress_path is not None and progress_path.exists():
                progress_path.unlink()  # marker must be produced by this invocation
            code, elapsed = self.execute(command, cwd=cwd, log=cwd / log_name)
            receipt = {"step_id": step["id"], "segment": segment, "backend": step["backend"], "argv": command, "input_sha256": digest_file(cwd / script), "log": str((cwd / log_name).relative_to(self.data)), "exit_code": code, "wall_seconds": elapsed, "finished_at": utc()}
            self.state["commands"].append(receipt)
            if self.stopped:
                raise Interrupted("native process interrupted; last committed checkpoint retained")
            if code:
                raise RuntimeError(f"native LAMMPS stage {step['id']} exited {code}; see {log_name}")
            complete = True
            if c:
                if not progress_path.is_file() or progress_path.stat().st_size > 128:
                    raise ValueError("native stage did not write a bounded progress marker")
                raw = progress_path.read_text().strip()
                if not re.fullmatch(r"[0-9]+", raw):
                    raise ValueError("native progress marker must contain one integer timestep")
                progress = int(raw)
                native_step = self.read_restart_step(cwd / c["restart_file"], step["backend"])
                if progress != native_step or progress <= prior_progress or progress > c["target_step"]:
                    raise ValueError("native restart and progress disagree, fail to advance or exceed target")
                receipt["native_restart_step"] = native_step
                complete = progress == c["target_step"]
                self.state["active_step"] = {"id": step["id"], "segment": segment, "progress": progress, "restart_file": c["restart_file"], "continuation_input": c["input"]}
                prior_progress = progress
            if complete:
                for name in step["expected_outputs"]:
                    if not (cwd / name).is_file() or (cwd / name).stat().st_size == 0:
                        raise ValueError(f"missing or empty expected native output: {name}")
                self.state["completed_steps"].append(step["id"])
                self.state["active_step"] = None
            self.checkpoint()
            if complete:
                return
            script = c["input"]

    def run(self):
        status, error = "succeeded", None
        try:
            self.initialize()
            for step in self.job["steps"]:
                if step["id"] not in self.state["completed_steps"]:
                    self.run_step(step)
        except Exception as exc:
            status = "interrupted" if self.stopped or isinstance(exc, Interrupted) else "failed"
            error = str(exc)
        try:
            files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
            inventory_error = None
        except Exception as exc:
            files, inventory_error = [], str(exc)
            status, error = "failed", error or inventory_error
        result = {"schema": RESULT_SCHEMA, "operation_id": self.operation_id, "job_id": self.job["id"], "status": status, "error": error, "recipe_sha256": self.recipe, "engine_id": self.engine_id, "engine": self.version, "native_build": self.build, "completed_steps": self.state["completed_steps"], "commands": self.state["commands"], "native_checkpoint_generation": self.state["generation"], "gpu_snapshot_used": False, "retry_semantics": "active stage rerun from last committed workspace unless explicit native continuation", "finished_at": utc(), "files": files, "inventory_error": inventory_error}
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
    print(json.dumps({k: result[k] for k in ("operation_id", "job_id", "status", "error")}))
    raise SystemExit(0 if result["status"] == "succeeded" else 143 if result["status"] == "interrupted" else 1)


if __name__ == "__main__":
    main()

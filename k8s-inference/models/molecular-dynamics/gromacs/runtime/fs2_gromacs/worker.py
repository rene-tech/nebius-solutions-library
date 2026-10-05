"""Native ordered GROMACS workflows with coherent segmented restart.

The companion owns remote artifact credentials. While it publishes a checkpoint
the engine is stopped, so files cannot change underneath a signed upload. The
engine resumes only after an acknowledgement. This is native GROMACS state,
not CUDA process checkpointing and not cloning one random state into replicas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import NVIDIA_IMAGE, RESULT_SCHEMA
from .contracts import canonical, normalize
from .files import FileDigestCache, atomic_json, digest_file, extract_inputs, inventory

DEFAULT_GMX = "/usr/local/gromacs/avx2_256/bin/gmx"
DYNAMICS = {"md", "sd", "bd", "md-vv", "md-vv-avek"}


def utc():
    return datetime.now(timezone.utc).isoformat()


def flag_value(args, flag, default=None):
    positions = [i for i, item in enumerate(args) if item == flag]
    if not positions:
        return default
    if len(positions) != 1 or positions[0] + 1 == len(args):
        raise ValueError(f"{flag} requires one unambiguous value")
    return args[positions[0] + 1]


def parse_mdp(text):
    result = {}
    for line in text.splitlines():
        line = line.split(";", 1)[0]
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip().replace("_", "-")] = value.strip()
    return result


def expand_args(tokens, cwd):
    args = []
    for token in tokens:
        if isinstance(token, str):
            args.append(token)
            continue
        matches = sorted(cwd.glob(token["files"]))
        if not matches:
            raise ValueError("explicit input file pattern matched no files")
        selected = []
        for path in matches:
            if (
                path.is_symlink()
                or not path.is_file()
                or cwd.resolve() not in path.resolve().parents
            ):
                raise ValueError(
                    "input file pattern must select contained regular files"
                )
            # Sparse output cadence can leave a valid closed segment empty.
            # Filter only when explicitly requested, after validating every
            # match. Never remove the original from checkpoint/output history.
            if token.get("nonempty", False) and path.stat().st_size == 0:
                continue
            selected.append(str(path.relative_to(cwd)))
        if not selected:
            raise ValueError("explicit input file pattern matched no nonempty files")
        args.extend(selected)
    # Fourteen days of five-minute segments exceed 4096 trajectory parts once
    # a previous run is continued. Keep each token below Linux MAX_ARG_STRLEN
    # and the total below a conservative exec budget, leaving space for env and
    # argv pointers. This is explicit file expansion, never a shell command.
    encoded = [len(arg.encode()) + 1 for arg in args]
    environment_bytes = sum(len(key.encode()) + len(value.encode()) + 2 for key, value in os.environ.items())
    argument_budget = min(1024 * 1024, max(0, os.sysconf("SC_ARG_MAX") - environment_bytes - 256 * 1024))
    if len(args) > 32768 or any(size > 128 * 1024 for size in encoded) or sum(encoded) > argument_budget:
        raise ValueError("expanded native command exceeds the argument budget")
    return args


def publish_final_coordinates(cwd, args):
    """Keep native part files and provide the documented final-coordinate name.

    -noappend also numbers .gro/.pdb outputs, not just trajectories. A stable
    final coordinate copy lets the next explicit grompp stage use its ordinary
    -c input without guessing a wall-time-dependent simulation-part number.
    """
    default = flag_value(args, "-deffnm", "confout") + ".gro"
    destination = cwd / flag_value(args, "-c", default)
    if not destination.suffix:
        destination = destination.with_suffix(".gro")
    parts = sorted(
        destination.parent.glob(
            destination.stem + ".part[0-9][0-9][0-9][0-9]" + destination.suffix
        )
    )
    if parts:
        shutil.copyfile(parts[-1], destination)


class Interrupted(RuntimeError):
    pass


class TimeLimitExceeded(Interrupted):
    """A local execution budget expired; the artifact companion is still alive."""


def report_time_limit(result):
    if result.get("error_code") == "WORKFLOW_TIME_LIMIT_EXCEEDED":
        try:
            Path("/dev/termination-log").write_text(json.dumps({
                "schema": "fs2-serve.nebius.ai/gromacs-worker-error/v1",
                "code": "WORKFLOW_TIME_LIMIT_EXCEEDED",
            }))
        except OSError:
            pass  # Local executions may not have a Kubernetes termination file.


class Workflow:
    def __init__(
        self,
        request,
        *,
        job_id,
        operation_id,
        workspace,
        gmx=None,
        checkpoint_mode="local",
        mpi=False,
    ):
        self.mpi = mpi
        self.request = normalize(request, mpi=mpi)
        self.job = next(job for job in self.request["jobs"] if job["id"] == job_id)
        self.operation_id = operation_id
        self.root = Path(workspace).resolve()
        self.data = self.root / "data"
        self.meta = self.root / ".fs2"
        self.gmx = gmx or os.environ.get("FS2_GROMACS_BINARY", DEFAULT_GMX)
        self.checkpoint_mode = checkpoint_mode
        self.stopped = False
        self.time_limit_exceeded = False
        self.child = None
        self.started = time.monotonic()
        self.deadline = self.started + self.request["max_wall_seconds"]
        self.engine_id = os.environ.get("FS2_GROMACS_ENGINE_ID", NVIDIA_IMAGE)
        self.mpi_stager = None
        self.file_digests = FileDigestCache()
        if mpi:
            from .mpi_files import InputStager

            self.mpi_stager = InputStager(self.root)
        self.recipe = hashlib.sha256(
            canonical({"request": self.request, "job": job_id, "image": self.engine_id})
        ).hexdigest()
        self.state = {
            "schema": "fs2-serve.nebius.ai/gromacs-checkpoint/v1",
            "operation_id": operation_id,
            "job_id": job_id,
            "recipe_sha256": self.recipe,
            "generation": 0,
            "completed_steps": [],
            "commands": [],
            "active_step": None,
            "elapsed_seconds": 0,
        }
        self.committed_generation = 0

    def command_environment(self, *, distributed=False):
        env = {
            **os.environ,
            "OMP_NUM_THREADS": str(self.request["threads"]),
            "GMX_MAXBACKUP": "-1",
        }
        if self.mpi and not distributed and env.get("FS2_GROMACS_MPI_TRANSPORT") == "ucx-rdma":
            # gmx_mpi --version/dump and analysis commands still call MPI_Init,
            # but run as one coordinator process. They do not use the rank
            # wrapper's one-HCA binding and must not open eight network HCAs.
            # This is singleton-only: actual mdrun's mpirun environment retains
            # the mandatory fail-closed RDMA transport and GPU-aware settings.
            env = {key: value for key, value in env.items()
                   if not key.startswith(("UCX_", "OMPI_MCA_pml_ucx_"))}
            env.update(OMPI_MCA_pml="ob1", OMPI_MCA_btl="self")
        return env

    def stop(self, signum, frame):
        self.stopped = True
        if self.child is not None and self.child.poll() is None:
            # GROMACS handles SIGTERM at a safe MD boundary and writes .cpt.
            # Do not kill the engine before it has had its termination grace.
            os.killpg(self.child.pid, signal.SIGTERM)

    def _wait_json(self, path, predicate, *, seconds=600):
        # Publication gets its own bounded grace even when computation used
        # the budget. Otherwise the final checkpoint would be lost on timeout.
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stopped:
                # Kubernetes terminates the companion too. Waiting for its
                # acknowledgement would consume the Pod grace, turn SIGTERM
                # into SIGKILL/137, and lose the explicit disruption receipt.
                # Recovery uses the previous committed remote generation.
                raise Interrupted(
                    "workflow interrupted; recover the last committed remote checkpoint"
                )
            if (self.meta / "transport-error.json").is_file():
                raise RuntimeError(
                    "durable checkpoint transport failed; see operation logs"
                )
            if path.is_file():
                value = json.loads(path.read_text())
                if predicate(value):
                    return value
            time.sleep(0.25)
        raise TimeoutError("durable checkpoint handoff timed out")

    def initialize(self):
        self.meta.mkdir(parents=True, exist_ok=True)
        if self.checkpoint_mode == "companion":
            self._wait_json(
                self.meta / "restore-complete.json",
                lambda value: value.get("status") == "ready",
            )
        previous = self.meta / "gromacs-state.json"
        if previous.is_file():
            state = json.loads(previous.read_text())
            if (
                state.get("recipe_sha256"),
                state.get("operation_id"),
                state.get("job_id"),
            ) != (
                self.recipe,
                self.operation_id,
                self.job["id"],
            ):
                raise ValueError(
                    "checkpoint belongs to another workflow, job or engine recipe"
                )
            self.state = state
            self.committed_generation = int(state["generation"])
            self.deadline -= float(state["elapsed_seconds"])
        archive = self.root / "input.tar.gz"
        if not self.data.exists() and archive.is_file():
            extract_inputs(
                archive, self.data, max_bytes=self.request["max_output_bytes"]
            )
        self.data.mkdir(parents=True, exist_ok=True)
        # Verified input materializations are read-only by default. Native
        # checkpoint/bias files must be writable in this private run workspace.
        first_path = {}
        for item in self.request.get("continuation_files", []):
            path = self.data / item["path"]
            if item["input_id"] in first_path and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(first_path[item["input_id"]], path)
            first_path.setdefault(item["input_id"], path)
            path.chmod(0o600)
        self.version = subprocess.check_output(
            [self.gmx, "--version"], stderr=subprocess.STDOUT, text=True, timeout=30,
            env=self.command_environment(),
        )
        atomic_json(
            self.meta / "engine.json",
            {"image": self.engine_id, "version": self.version},
        )
        topology = self.meta / "mpi-topology.json"
        if self.mpi and topology.is_file():
            # Export the configured topology with native files. Actual rank/GPU
            # bindings are measured for each launch and retained separately.
            atomic_json(
                self.data / "fs2-mpi-topology.json", json.loads(topology.read_text())
            )

    def checkpoint(self):
        files = inventory(self.data, max_bytes=self.request["max_output_bytes"], digest_cache=self.file_digests)
        self.state["generation"] += 1
        self.state["elapsed_seconds"] += time.monotonic() - self.started
        self.started = time.monotonic()
        atomic_json(self.meta / "gromacs-state.json", self.state)
        marker = {
            "schema": "fs2-serve.nebius.ai/gromacs-checkpoint-ready/v1",
            "state": self.state,
            "files": files,
        }
        atomic_json(self.meta / "checkpoint-ready.json", marker)
        if self.checkpoint_mode == "companion":
            generation = self.state["generation"]
            self._wait_json(
                self.meta / "checkpoint-ack.json",
                lambda value: value.get("generation") == generation
                and value.get("status") == "committed",
            )
        self.committed_generation = self.state["generation"]

    def execute(self, argv, *, cwd, log, stdin="", timeout=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.time_limit_exceeded = True
            raise TimeLimitExceeded("execution budget exhausted; continue from the last committed checkpoint")
        if self.stopped:
            raise Interrupted(
                "workflow stopped before starting the next native command"
            )
        env = self.command_environment(distributed=self.mpi and argv[0] == "mpirun")
        started = time.monotonic()
        with log.open("wb") as output:
            self.child = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                self.child.communicate(
                    stdin.encode(), timeout=min(remaining, timeout or remaining)
                )
            except subprocess.TimeoutExpired:
                # This is not a Pod shutdown: keep the companion alive and
                # wait for its final durable checkpoint acknowledgement.
                self.time_limit_exceeded = True
                if self.child.poll() is None:
                    os.killpg(self.child.pid, signal.SIGTERM)
                try:
                    self.child.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait()
            code = self.child.returncode
            self.child = None
        return code, time.monotonic() - started

    def input_parameters(self, cwd, args, step_id):
        tpr = cwd / flag_value(args, "-s", "topol.tpr")
        if not tpr.is_file():
            raise ValueError(
                "mdrun requires an existing prepared TPR; use a grompp step first"
            )
        output = self.meta / f"{step_id}-tpr.mdp"
        with (self.meta / f"{step_id}-tpr.log").open("wb") as log:
            subprocess.run(
                [self.gmx, "dump", "-s", str(tpr), "-om", str(output)],
                stdout=subprocess.DEVNULL,
                stderr=log,
                check=True,
                timeout=120,
                env=self.command_environment(),
            )
        params = parse_mdp(output.read_text())
        return params, digest_file(tpr)

    def checkpoint_step(self, checkpoint):
        found = None
        process = subprocess.Popen(
            [self.gmx, "dump", "-cp", str(checkpoint)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=self.command_environment(),
        )
        assert process.stdout is not None
        # Stream the dump: checkpoint coordinates can contain millions of lines.
        for line in process.stdout:
            match = re.match(r"\s*step\s*=\s*(\d+)\s*$", line)
            if match:
                found = int(match.group(1))
        if process.wait(timeout=30) or found is None:
            raise ValueError("native checkpoint has no readable simulation step")
        return found

    def run_step(self, step):
        cwd = self.data / step["directory"]
        cwd.mkdir(parents=True, exist_ok=True)
        args = expand_args(step["args"], cwd)
        is_md = step["command"] == "mdrun"
        segmented = False
        target_step = None
        checkpoint = cwd / f"fs2-{step['id']}.cpt"
        if is_md:
            params, tpr_digest = self.input_parameters(cwd, args, step["id"])
            previous = self.state.get("active_step")
            if (
                previous
                and previous["id"] == step["id"]
                and previous["tpr_sha256"] != tpr_digest
            ):
                raise ValueError(
                    "the active checkpoint's TPR changed; refusing an incompatible continuation"
                )
            segmented = params.get("integrator") in DYNAMICS and "-rerun" not in args
            steps = int(params.get("nsteps", "-1"))
            if segmented and steps < 0:
                raise ValueError(
                    "supply a finite nsteps in the TPR; an unbounded simulation has no completion criterion"
                )
            target_step = int(params.get("init-step", "0")) + steps
            if not self.mpi:
                args += ["-ntmpi", "1"]
            args += [
                "-ntomp",
                str(self.request["threads"]),
                "-cpt",
                str(self.request["checkpoint_minutes"]),
                "-cpo",
                checkpoint.name,
                "-noappend",
            ]
            if step.get("plumed_input"):
                plumed = cwd / step["plumed_input"]
                kernel = Path(
                    os.environ.get(
                        "PLUMED_KERNEL", "/opt/plumed/lib/libplumedKernel.so"
                    )
                )
                if not plumed.is_file() or not kernel.is_file():
                    raise ValueError(
                        "PLUMED requires an existing input file and the packaged runtime kernel"
                    )
                args += ["-plumed", step["plumed_input"]]
            if "-deffnm" not in args:
                args += ["-deffnm", step["id"]]
            for option in ("-nb", "-pme", "-bonded", "-update"):
                if option not in args:
                    args += [option, "auto"]
            self.state["active_step"] = {
                "id": step["id"],
                "tpr_sha256": tpr_digest,
                "target_step": target_step,
            }
        segment = sum(
            command["step_id"] == step["id"] for command in self.state["commands"]
        )
        while True:
            segment += 1
            command = [self.gmx, step["command"], *args]
            staging = None
            if is_md:
                if checkpoint.is_file():
                    command += ["-cpi", checkpoint.name]
                elif step.get("restart_checkpoint"):
                    original = cwd / step["restart_checkpoint"]
                    if not original.is_file():
                        raise ValueError(
                            "requested restart checkpoint does not exist; refusing a fresh start"
                        )
                    command += ["-cpi", step["restart_checkpoint"]]
                if segmented:
                    command += ["-maxh", str(self.request["segment_minutes"] / 60)]
            if is_md and self.mpi:
                from .mpi import launch_command

                staging = self.mpi_stager.stage(
                    cwd, command[2:], timeout=self.deadline - time.monotonic()
                )
                command = launch_command(self.request, command)
            log = self.data / f"fs2-{step['id']}-segment-{segment:06d}.log"
            code, wall = self.execute(command, cwd=cwd, log=log, stdin=step["stdin"])
            # Log parsing is bounded; logs themselves are retained in full.
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - 256 * 1024))
                tail = handle.read().decode(errors="replace")
            performance = re.findall(r"Performance:\s+([0-9.eE+-]+)", tail)
            item = {
                "step_id": step["id"],
                "segment": segment,
                "command": command,
                "directory": step["directory"],
                "exit_code": code,
                "wall_seconds": wall,
                "performance_ns_per_day": float(performance[-1])
                if performance
                else None,
                "log": str(log.relative_to(self.data)),
                "finished_at": utc(),
            }
            self.state["commands"].append(item)
            if staging is not None:
                item["mpi_input_staging"] = staging
                from .mpi_rank import read_rank_bindings

                item.update(
                    read_rank_bindings(
                        log,
                        expected_ranks=self.request["nodes"]
                        * self.request.get("gpus_per_node", 1),
                    )
                )
            if code != 0:
                if self.time_limit_exceeded and segmented and checkpoint.is_file():
                    item["checkpoint_step"] = self.checkpoint_step(checkpoint)
                self.checkpoint()
                if self.time_limit_exceeded:
                    raise TimeLimitExceeded("execution budget exhausted; continue from the last committed checkpoint")
                raise RuntimeError(
                    f"native command {step['id']} failed with exit code {code}; see its full log"
                )
            current_step = (
                self.checkpoint_step(checkpoint)
                if segmented and checkpoint.is_file()
                else None
            )
            item["checkpoint_step"] = current_step
            complete = not segmented or (
                current_step is not None and current_step >= target_step
            )
            if complete:
                if is_md:
                    publish_final_coordinates(cwd, args)
                for name in step["expected_outputs"]:
                    path = cwd / name
                    if not path.is_file() or not path.stat().st_size:
                        raise ValueError(
                            f"expected output is missing or empty after {step['id']}: {name}"
                        )
                self.state["completed_steps"].append(step["id"])
                self.state["active_step"] = None
            self.checkpoint()
            if self.time_limit_exceeded and not complete:
                raise TimeLimitExceeded("execution budget exhausted; continue from the last committed checkpoint")
            if self.stopped:
                raise Interrupted(
                    "workflow interrupted; last committed checkpoint is retained"
                )
            if complete:
                return
            if current_step is None:
                raise RuntimeError(
                    "segmented dynamics returned without a native checkpoint"
                )

    def run(self):
        self.version = "unavailable: engine initialization did not complete"
        status, error = "succeeded", None
        try:
            self.initialize()
            for step in self.job["steps"]:
                if step["id"] not in self.state["completed_steps"]:
                    self.run_step(step)
        except Exception as exc:
            status, error = (
                "interrupted"
                if self.stopped or isinstance(exc, Interrupted)
                else "failed",
                str(exc),
            )
        inventory_error = None
        try:
            files = inventory(self.data, max_bytes=self.request["max_output_bytes"], digest_cache=self.file_digests)
        except Exception as exc:
            # Inventory failure must itself have a terminal result. Never turn
            # completed integration with undeliverable output into success, and
            # never delete outputs/checkpoints to make them fit the budget.
            inventory_error = str(exc)
            status = "failed"
            error = "; ".join(
                filter(None, (error, "output inventory failed: " + inventory_error))
            )
            files = self.failure_log_inventory()
        result = {
            "schema": RESULT_SCHEMA,
            "operation_id": self.operation_id,
            "job_id": self.job["id"],
            "status": status,
            "error": error,
            "error_code": "WORKFLOW_TIME_LIMIT_EXCEEDED" if self.time_limit_exceeded and status != "succeeded" else None,
            "recipe_sha256": self.recipe,
            "engine": self.version,
            "nvidia_image": NVIDIA_IMAGE if self.engine_id == NVIDIA_IMAGE else None,
            "engine_id": self.engine_id,
            "mpi_ranks": self.request.get("nodes", 1)
            * self.request.get("gpus_per_node", 1),
            "nodes": self.request.get("nodes", 1),
            "gpus_per_node": self.request.get("gpus_per_node", 1),
            "completed_steps": self.state["completed_steps"],
            "commands": self.state["commands"],
            "native_checkpoint_generation": self.state["generation"],
            "committed_checkpoint_generation": self.committed_generation,
            "checkpoint_commit_scope": "remote-companion"
            if self.checkpoint_mode == "companion"
            else "local-only",
            "gpu_snapshot_used": False,
            "finished_at": utc(),
            "files": files,
            "inventory_complete": inventory_error is None,
            "inventory_error": inventory_error,
            "inventory_scope": "all-native-files"
            if inventory_error is None
            else "bounded-failure-logs-only",
            "scientific_outputs_omitted_from_result": inventory_error is not None,
        }
        atomic_json(self.root / "result.json", result)
        return result

    def failure_log_inventory(self):
        """Best-effort bounded diagnostics, never an incomplete science inventory."""
        import stat

        paths = [item.get("log") for item in reversed(self.state["commands"])]
        files, seen, total = [], set(), 0
        for name in paths:
            if not isinstance(name, str) or name in seen:
                continue
            seen.add(name)
            try:
                path = self.data / name
                before = path.lstat()
                if (
                    self.data.resolve() not in path.resolve().parents
                    or not stat.S_ISREG(before.st_mode)
                ):
                    continue
                if total + before.st_size > min(
                    self.request["max_output_bytes"], 64 * 1024**2
                ):
                    continue
                digest = digest_file(path)
                after = path.lstat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    continue
                files.append(
                    {"path": name, "size_bytes": after.st_size, "sha256": digest}
                )
                total += after.st_size
            except (OSError, ValueError):
                continue
            if len(files) >= 128:
                break
        return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--checkpoint-mode", choices=("local", "companion"), default="companion"
    )
    args = parser.parse_args()
    worker = Workflow(
        json.loads(args.request.read_text()),
        job_id=args.job_id,
        operation_id=args.operation_id,
        workspace=args.workspace,
        checkpoint_mode=args.checkpoint_mode,
    )
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    result = worker.run()
    report_time_limit(result)
    print(
        json.dumps(
            {key: result[key] for key in ("operation_id", "job_id", "status", "error")}
        )
    )
    raise SystemExit(
        0
        if result["status"] == "succeeded"
        else 143
        if result["status"] == "interrupted"
        else 1
    )


if __name__ == "__main__":
    main()

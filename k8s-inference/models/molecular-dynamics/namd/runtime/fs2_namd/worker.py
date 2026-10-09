"""Native NAMD execution with closed-file, acknowledged checkpoint generations.

Scientific configuration is supplied by the scientist. Managed dynamics owns
only run lengths, checkpoint continuation, and unique output basenames. Native
Tcl/psfgen programs checkpoint at completed workflow boundaries. No GPU process
snapshot, RNG cloning, script sandbox, or convergence claim is implied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import statistics
import struct
import subprocess
import time

from fs2_gromacs.files import atomic_json, digest_file, extract_inputs, inventory
from . import ENGINE_ID, RESULT_SCHEMA
from .contracts import canonical, normalize
from .colvars_state import prepare as prepare_bias_state

NAMD = "/usr/local/namd/bin/namd3"
PSFGEN = "/usr/local/namd/bin/psfgen"


def tcl(value):
    value = str(value)
    for char in ("\\", '"', "$", "[", "]"):
        value = value.replace(char, "\\" + char)
    return '"' + value + '"'


def xsc_step(path):
    lines = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if len(lines) != 1:
        raise ValueError("native XSC checkpoint needs exactly one state record")
    values = [float(token) for token in lines[0].split()]
    if len(values) < 13 or not all(math.isfinite(v) for v in values) or values[0] != int(values[0]):
        raise ValueError("invalid or non-finite native cell checkpoint")
    return int(values[0])


def colvars_step(path):
    # The native state begins with its configuration { step ... } block.
    # Validate that bias history belongs to the same coordinate/cell boundary.
    with path.open() as stream:
        head = stream.read(65536)
    match = re.search(r"\bconfiguration\s*\{[^}]*\bstep\s+(\d+)\b", head, re.IGNORECASE)
    if not match:
        raise ValueError("Colvars state lacks a native configuration step")
    return int(match.group(1))


def binary_vectors(path):
    """Validate NAMD binary coordinates/velocities with bounded memory."""
    with path.open("rb") as handle:
        head = handle.read(4)
        if len(head) != 4:
            raise ValueError("truncated binary NAMD vectors")
        endian = next((e for e in ("<", ">") if 4 + 24 * struct.unpack(e + "i", head)[0] == path.stat().st_size), None)
        if endian is None:
            raise ValueError("native vector atom count/size mismatch")
        atoms = struct.unpack(endian + "i", head)[0]
        if atoms <= 0:
            raise ValueError("empty native vectors")
        while block := handle.read(8 * 131072):
            if not all(math.isfinite(v[0]) for v in struct.iter_unpack(endian + "d", block)):
                raise ValueError("non-finite native coordinates/velocities")
    return atoms


def log_metrics(path):
    # NAMD omits ETITLE when firstTimestep is nonzero. This is the pinned
    # 3.0.2 classic-energy layout, replaced by ETITLE whenever emitted.
    fields = "TS BOND ANGLE DIHED IMPRP ELECT VDW BOUNDARY MISC KINETIC TOTAL TEMP POTENTIAL TOTALAVG TEMPAVG PRESSURE GPRESSURE VOLUME PRESSAVG GPRESSAVG".split()
    records, timings, benchmark, first_step = [], [], [], None
    version, dt, atom_count, random_seed, configured_first_step = None, None, None, None, None
    restored_bias = None
    with path.open(errors="replace") as handle:
        for line in handle:
            if line.startswith("ETITLE:"):
                fields = line.split()[1:]
            elif line.startswith("ENERGY:"):
                values = [float(v) for v in line.split()[1:]]
                if not all(math.isfinite(v) for v in values):
                    raise ValueError("non-finite energy/ensemble observables in native log")
                if len(fields) != len(values):
                    raise ValueError("native energy layout is not recognized; retain log for qualification")
                records.append(dict(zip(fields, values)))
                first_step = first_step if first_step is not None else values[0]
                if len(records) > 10000:
                    records.pop(0)
            if line.startswith("TIMING:"):
                match = re.search(r"Wall:\s*[0-9.eE+-]+,\s*([0-9.eE+-]+)/step", line)
                if match:
                    timings.append(float(match.group(1)))
            match = re.search(r"Benchmark time:.*?([0-9.eE+-]+)\s+s/step", line)
            if match:
                benchmark.append(float(match.group(1)))
            if "Info: NAMD " in line:
                version = line.strip()
            if line.startswith("TCL: FS2_COLVARS_RESTORE_VERIFIED "):
                restored_bias = json.loads(line.split("FS2_COLVARS_RESTORE_VERIFIED ", 1)[1])
            match = re.search(r"^Info:\s+TIMESTEP\s+([0-9.eE+-]+)", line)
            if match:
                dt = float(match.group(1))
            match = re.search(r"^Info:\s+(\d+) ATOMS\s*$", line)
            if match:
                atom_count = int(match.group(1))
            match = re.search(r"^Info:\s+RANDOM NUMBER SEED\s+(-?\d+)", line)
            if match:
                random_seed = int(match.group(1))
            match = re.search(r"^Info:\s+FIRST TIMESTEP\s+(\d+)", line)
            if match:
                configured_first_step = int(match.group(1))
    timings = [v for v in timings if math.isfinite(v) and v > 0]
    seconds_step = statistics.median(timings[-10:]) if timings else (benchmark[-1] if benchmark else None)
    return {"engine": version, "atoms": atom_count, "timestep_fs": dt,
            "random_seed": random_seed, "configured_first_step": configured_first_step,
            "colvars_restore_verified": restored_bias,
            "energy_records": len(records), "first_energy_step": first_step,
            "last_energy": records[-1] if records else None,
            "native_seconds_per_step": seconds_step,
            "performance_ns_per_day": 0.0864 * dt / seconds_step if dt and seconds_step else None,
            "observables": {key: {"min": min(r[key] for r in records), "max": max(r[key] for r in records),
                                  "mean": statistics.fmean(r[key] for r in records)}
                            for key in ("TOTAL", "TEMP", "PRESSURE", "VOLUME") if records and key in records[0]}}


# Guards inspect effective NAMD/Colvars values, not arbitrary Tcl source text.
# They prevent accidental use of known affected features; they are not a Tcl
# sandbox and do not make other untested Colvars features qualified.
GUARD = r'''
proc fs2_feature_guard {} {
    if {[isset GBIS] && [istrue GBIS]} {
        error "GBIS is not qualified in NVIDIA NAMD 3.0.2; 3.0.3 fixes GPU-offload crash"
    }
    if {[isset colvars] && [istrue colvars]} {
        set cfg [cv getconfig]
        if {[regexp -nocase {\mspinAngle\M} $cfg]} {
            error "spinAngle is unavailable: this NVIDIA artifact predates the 3.0.3 extended-Lagrangian correctness fix"
        }
    }
}
'''


class Interrupted(RuntimeError):
    pass


class Workflow:
    def __init__(self, request, *, job_id, operation_id, workspace, checkpoint_mode="companion", namd=NAMD):
        self.request = normalize(request)
        self.job = next(j for j in self.request["jobs"] if j["id"] == job_id)
        self.operation_id, self.root = operation_id, Path(workspace).resolve()
        self.data, self.meta = self.root / "data", self.root / ".fs2"
        self.checkpoint_mode, self.namd = checkpoint_mode, namd
        self.started = time.monotonic()
        self.deadline = self.started + self.request["max_wall_seconds"]
        self.child, self.stopped = None, False
        self.recipe = hashlib.sha256(canonical({"request": self.request, "job": job_id, "image": ENGINE_ID})).hexdigest()
        self.state = {"schema": "fs2-serve.nebius.ai/namd-checkpoint/v1", "operation_id": operation_id,
                      "job_id": job_id, "recipe_sha256": self.recipe, "generation": 0, "completed_steps": [],
                      "commands": [], "active_step": None, "elapsed_seconds": 0, "inputs": []}

    def stop(self, signum, frame):
        self.stopped = True
        if self.child and self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)

    def wait_json(self, path, predicate):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.stopped:
                raise Interrupted("worker interrupted; restore the last committed generation")
            if (self.meta / "transport-error.json").exists():
                raise RuntimeError("native checkpoint transport failed")
            if path.is_file() and predicate(json.loads(path.read_text())):
                return
            time.sleep(0.2)
        raise TimeoutError("native checkpoint companion timed out")

    def initialize(self):
        self.meta.mkdir(parents=True, exist_ok=True)
        if self.checkpoint_mode == "companion":
            self.wait_json(self.meta / "restore-complete.json", lambda v: v.get("status") == "ready")
        previous = self.meta / "namd-state.json"
        if previous.is_file():
            state = json.loads(previous.read_text())
            if (state.get("schema"), state.get("recipe_sha256"), state.get("operation_id"), state.get("job_id")) != (
                self.state["schema"], self.recipe, self.operation_id, self.job["id"]
            ):
                raise ValueError("checkpoint belongs to another operation, job, request or engine")
            self.state = state
            self.deadline -= float(state["elapsed_seconds"])
        if not self.data.exists():
            extract_inputs(self.root / "input.tar.gz", self.data, max_bytes=self.request["max_output_bytes"])
        if not self.state["inputs"]:
            self.state["inputs"] = inventory(self.data, max_bytes=self.request["max_output_bytes"])
        self.check_inputs()
        if not self.state["generation"]:
            self.checkpoint()

    def check_inputs(self):
        for item in self.state["inputs"]:
            path = self.contained(self.data, item["path"])
            if not path.is_file() or digest_file(path) != item["sha256"]:
                raise ValueError("immutable native input changed; refusing incompatible continuation")

    def contained(self, cwd, name):
        path = cwd / name
        if path.is_symlink() or self.data not in path.resolve().parents:
            raise ValueError("native file escapes the workspace")
        return path

    def checkpoint(self):
        self.state["generation"] += 1
        self.state["elapsed_seconds"] += time.monotonic() - self.started
        self.started = time.monotonic()
        files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
        atomic_json(self.meta / "namd-state.json", self.state)
        atomic_json(self.meta / "checkpoint-ready.json", {"schema": "fs2-serve.nebius.ai/namd-checkpoint-ready/v1",
                                                          "state": self.state, "files": files})
        if self.checkpoint_mode == "companion":
            generation = self.state["generation"]
            self.wait_json(self.meta / "checkpoint-ack.json", lambda v: v.get("generation") == generation and v.get("status") == "committed")

    def execute(self, command, cwd, log):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.stopped:
            raise Interrupted("execution budget exhausted before the next native segment")
        started = time.monotonic()
        cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        first_energy, scanned, next_budget = None, 0, started + 5
        with log.open("wb") as output:
            self.child = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                          stdin=subprocess.DEVNULL, start_new_session=True)
            while self.child.poll() is None:
                now = time.monotonic()
                if first_energy is None:
                    with log.open("rb") as reader:
                        reader.seek(scanned)
                        block = reader.read(256 * 1024)
                        scanned += len(block)
                        if b"ENERGY:" in block:
                            first_energy = now - started
                if now >= next_budget:
                    total = sum(p.stat().st_size for p in self.data.rglob("*") if p.is_file() and not p.is_symlink())
                    next_budget = now + 5
                    if total > self.request["max_output_bytes"]:
                        self.stop(signal.SIGTERM, None)
                if now >= self.deadline or self.stopped:
                    self.stop(signal.SIGTERM, None)
                    try:
                        self.child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.child.pid, signal.SIGKILL)
                        self.child.wait()
                    break
                time.sleep(0.1)
            code, self.child = self.child.returncode, None
        cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        return code, time.monotonic() - started, {
            "first_energy_log_observed_seconds": first_energy,
            "cpu_user_seconds": cpu_after.ru_utime - cpu_before.ru_utime,
            "cpu_system_seconds": cpu_after.ru_stime - cpu_before.ru_stime}

    def script(self, step, *, prefix=None, current=None, count=None, restart=None, original_bias=None):
        mode = step["mode"]
        if mode == "prepare":
            return f"source {tcl(step['config'])}\n"
        lines = [GUARD]
        if mode == "native":
            # Before every scientific run, inspect effective options. The source
            # can still use arbitrary Tcl; this is a compatibility guard only.
            lines += [f"set fs2_expected_resident {int(step['gpu_mode'] == 'resident')}",
                      "proc fs2_mode_guard {} {",
                      "  set actual 0",
                      "  foreach key {CUDASOAintegrate GPUresident} {if {[isset $key] && [istrue $key]} {set actual 1}}",
                      '  if {$actual != $::fs2_expected_resident} {error "native config and requested gpu_mode disagree"}', "}",
                      "rename run fs2_native_run", "rename minimize fs2_native_minimize",
                      "proc run {args} {fs2_mode_guard; fs2_feature_guard; uplevel 1 [list fs2_native_run {*}$args]}",
                      "proc minimize {args} {fs2_mode_guard; fs2_feature_guard; uplevel 1 [list fs2_native_minimize {*}$args]}",
                      f"source {tcl(step['config'])}", "fs2_feature_guard"]
            return "\n".join(lines) + "\n"
        lines += [f"set fs2_restart {int(bool(restart))}", f"set fs2_first_step {current}",
                  "rename run fs2_native_run", "rename minimize fs2_native_minimize", "rename startup fs2_native_startup",
                  'proc run {args} {error "dynamics config must not execute run; use native mode for complete programs"}',
                  'proc minimize {args} {error "dynamics config must not execute minimize; use native mode for preparation"}',
                  'proc startup {args} {error "dynamics config must not initialize the engine"}',
                  f"source {tcl(step['config'])}",
                  "rename run {}; rename fs2_native_run run", "rename minimize {}; rename fs2_native_minimize minimize",
                  "rename startup {}; rename fs2_native_startup startup",
                  "foreach key {outputName restartName DCDfile velDCDfile forceDCDfile XSTfile numsteps benchmarkTime} {",
                  '  if {[isset $key]} {error "managed dynamics owns $key; omit it from config (output cadence remains native)"}', "}",
                  "if {[isset GBIS] && [istrue GBIS]} {error \"GBIS is unavailable in this pinned 3.0.2 profile\"}",
                  f"set fs2_gpu_resident {int(step['gpu_mode'] == 'resident')}",
                  "foreach key {CUDASOAintegrate GPUresident} {",
                  '  if {[isset $key] && [istrue $key] != $fs2_gpu_resident} {error "config and requested gpu_mode disagree"}', "}",
                  "if {![isset CUDASOAintegrate] && ![isset GPUresident]} {CUDASOAintegrate $fs2_gpu_resident}",
                  f"outputName {tcl(prefix)}", f"restartName {tcl(prefix + '.restart')}",
                  f"DCDfile {tcl(prefix + '.dcd')}", f"XSTfile {tcl(prefix + '.xst')}",
                  f"firsttimestep {current}",
                  'if {[isset binaryoutput]} {if {![istrue binaryoutput]} {error "managed continuation requires binaryoutput yes"}} else {binaryoutput yes}']
        if restart:
            lines += ['if {[isset temperature]} {error "restart config must omit temperature using fs2_restart; velocities are restored"}',
                      "foreach key {cellBasisVector1 cellBasisVector2 cellBasisVector3 cellOrigin} {",
                      '  if {[isset $key]} {error "restart config must omit initial cell using fs2_restart; XSC is restored"}', "}",
                      f"binCoordinates {tcl(restart['coordinates'])}", f"binVelocities {tcl(restart['velocities'])}",
                      f"extendedSystem {tcl(restart['cell'])}"]
            if "colvars_state" in restart:
                lines += [f"colvarsInput {tcl(restart['colvars_state'])}"]
            elif not (step["initialize_colvars"] and current == step["first_step"]):
                lines += ['if {[isset colvars] && [istrue colvars]} {error "enabled Colvars continuation requires matching bias state"}']
        lines += ["startup", "fs2_feature_guard"]
        if original_bias:
            loaded = prefix + ".loaded"
            lines += ['if {![istrue colvars]} {error "provided Colvars restart state was not enabled"}',
                      f"set fs2_bias_handle [open {tcl(loaded + '.colvars.state')} w]",
                      "puts -nonewline $fs2_bias_handle [cv savetostring]; close $fs2_bias_handle",
                      'print "FS2_COLVARS_RESTORE_VERIFIED [exec /usr/bin/python3 -m fs2_namd.colvars_state '
                      f'--original {tcl(original_bias)} --loaded {tcl(loaded + ".colvars.state")} --expected-step {current}]"']
        lines += [f"run {count}", f"output {tcl(prefix)}",
                  f"if {{[istrue colvars]}} {{cv save {tcl(prefix)}}}",
                  f'print "FS2_SEGMENT_COMPLETE {current + count}"']
        return "\n".join(lines) + "\n"

    def run_step(self, step):
        self.check_inputs()
        cwd = self.data if step["directory"] == "." else self.contained(self.data, step["directory"])
        if not cwd.is_dir() or not self.contained(cwd, step["config"]).is_file():
            raise ValueError("native configuration/directory is missing")
        active = self.state["active_step"]
        if active and active["id"] != step["id"]:
            raise ValueError("checkpoint active step does not match workflow order")
        current = active["current_step"] if active else step["first_step"]
        target = step["first_step"] + step.get("steps", 0)
        restart = active.get("restart") if active else step.get("restart")
        while True:
            number = 1 + sum(c["step_id"] == step["id"] for c in self.state["commands"])
            prefix = step.get("output_prefix", step["id"]) + f".part{number:06d}"
            self.contained(cwd, prefix).parent.mkdir(parents=True, exist_ok=True)
            count = min(step["segment_steps"], target - current) if step["mode"] == "dynamics" else None
            if restart:
                for name in restart.values():
                    path = self.contained(cwd, name)
                    if not path.is_file() or not path.stat().st_size:
                        raise ValueError("native restart component is missing")
                if xsc_step(cwd / restart["cell"]) != current:
                    raise ValueError("native restart cell timestep does not match continuation")
                if "colvars_state" in restart and colvars_step(cwd / restart["colvars_state"]) != current:
                    raise ValueError("Colvars bias-state timestep does not match the coordinate/cell checkpoint")
            original_bias, bias_provenance = None, None
            execution_restart = dict(restart) if restart else None
            if restart and "colvars_state" in restart:
                original_bias = restart["colvars_state"]
                derived = prefix + ".colvars.input.state"
                bias_provenance = prepare_bias_state(cwd / original_bias, self.contained(cwd, derived))
                if bias_provenance["repair"]:
                    execution_restart["colvars_state"] = derived
                bias_provenance.update(original=original_bias, loaded_input=execution_restart["colvars_state"])
            script = cwd / f"fs2-{step['id']}-part{number:06d}.namd"
            script.write_text(self.script(step, prefix=prefix, current=current, count=count,
                                          restart=execution_restart, original_bias=original_bias))
            command = [PSFGEN, script.name] if step["mode"] == "prepare" else [self.namd, f"+p{self.request['threads']}", "+devices", "0", script.name]
            log = cwd / f"fs2-{step['id']}-part{number:06d}.log"
            code, wall, usage = self.execute(command, cwd, log)
            item = {"step_id": step["id"], "segment": number, "command": command,
                    "directory": step["directory"], "exit_code": code, "wall_seconds": wall,
                    "log": str(log.relative_to(self.data)), "gpu_mode": step["gpu_mode"]}
            self.state["commands"].append(item)
            if bias_provenance:
                item["colvars_restart"] = bias_provenance
            item.update(usage)
            if code:
                raise Interrupted("native process interrupted; prior committed generation is recoverable") if self.stopped else RuntimeError(f"native step {step['id']} failed ({code}); see its retained log")
            item.update(log_metrics(log))
            complete = True
            if step["mode"] == "dynamics":
                with log.open(errors="replace") as stream:
                    if not any(line.strip() == f"TCL: FS2_SEGMENT_COMPLETE {current + count}" for line in stream):
                        raise ValueError("native run did not reach its finite segment completion marker")
                new = {"coordinates": prefix + ".coor", "velocities": prefix + ".vel", "cell": prefix + ".xsc"}
                atoms = binary_vectors(cwd / new["coordinates"])
                if binary_vectors(cwd / new["velocities"]) != atoms or xsc_step(cwd / new["cell"]) != current + count:
                    raise ValueError("native checkpoint components disagree")
                bias = cwd / (prefix + ".colvars.state")
                if bias.exists():
                    if colvars_step(bias) != current + count:
                        raise ValueError("native Colvars output timestep does not match its cell")
                    new["colvars_state"] = prefix + ".colvars.state"
                if not item["energy_records"]:
                    raise ValueError("managed dynamics returned no finite energy samples")
                current, restart = current + count, new
                self.state["active_step"] = {"id": step["id"], "current_step": current, "restart": new}
                item["checkpoint_step"], item["atoms"] = current, atoms
                complete = current >= target
                if complete:
                    for name in new.values():
                        suffix = name[len(prefix):]
                        shutil.copyfile(cwd / name, cwd / (step["output_prefix"] + suffix))
            if complete:
                for name in step["expected_outputs"]:
                    path = self.contained(cwd, name)
                    if not path.is_file() or not path.stat().st_size:
                        raise ValueError(f"expected native output missing or empty: {name}")
                self.state["completed_steps"].append(step["id"])
                self.state["active_step"] = None
            self.check_inputs()
            self.checkpoint()
            if complete:
                return

    def run(self):
        status, error = "succeeded", None
        try:
            self.initialize()
            for step in self.job["steps"]:
                if step["id"] not in self.state["completed_steps"]:
                    self.run_step(step)
        except Exception as exc:
            status, error = ("interrupted" if self.stopped or isinstance(exc, Interrupted) else "failed"), str(exc)
        try:
            files = inventory(self.data, max_bytes=self.request["max_output_bytes"])
        except Exception as exc:
            files, status, error = [], "failed", str(exc)
        result = {"schema": RESULT_SCHEMA, "operation_id": self.operation_id, "job_id": self.job["id"],
                  "status": status, "error": error, "recipe_sha256": self.recipe, "engine_id": ENGINE_ID,
                  "completed_steps": self.state["completed_steps"], "commands": self.state["commands"],
                  "native_checkpoint_generation": self.state["generation"], "gpu_snapshot_used": False,
                  "native_restart_semantics": {"rng_state_serialized": False, "bitwise_continuation_claimed": False,
                                               "seed_policy": "scientist-owned native configuration; effective seeds are recorded per command"},
                  "files": files}
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
    worker = Workflow(json.loads(args.request.read_text()), job_id=args.job_id, operation_id=args.operation_id,
                      workspace=args.workspace, checkpoint_mode=args.checkpoint_mode)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    result = worker.run()
    print(json.dumps({k: result[k] for k in ("status", "error", "job_id", "operation_id")}))
    raise SystemExit(0 if result["status"] == "succeeded" else 143 if result["status"] == "interrupted" else 1)


if __name__ == "__main__":
    main()

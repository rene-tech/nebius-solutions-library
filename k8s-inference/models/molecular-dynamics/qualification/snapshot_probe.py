"""Bounded native-process compatibility probe using the existing FS2 lifecycle.

This is an isolated qualification harness, not a controller or public snapshot
option. A restored continuation is never labelled an independent replica.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import subprocess
import sys
import time


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def file_digest_compat(source, algorithm):
    """Python 3.10 MD images can reuse the unchanged platform helper."""
    value = hashlib.new(algorithm) if isinstance(algorithm, str) else algorithm()
    for block in iter(lambda: source.read(1024 * 1024), b""):
        value.update(block)
    return value


def load_platform(source):
    if not hasattr(hashlib, "file_digest"):
        hashlib.file_digest = file_digest_compat
    spec = importlib.util.spec_from_file_location("platform_checkpoint", source / "process_checkpoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def relative_path(root, name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("probe paths must stay in their task-owned directory")
    return root / path


def progress(root, plan):
    if plan.get("progress_glob"):
        relative_path(root, plan["progress_glob"])
        paths = list(root.glob(plan["progress_glob"]))
    else:
        paths = [relative_path(root, plan["progress_file"])]
    values = [
        value for path in paths if path.is_file()
        for value in re.findall(plan["progress_pattern"], path.read_text(errors="replace"), re.MULTILINE)
    ]
    return max((int(value) for value in values), default=None)


def process_state(pid):
    path = Path(f"/proc/{pid}/stat")
    try:
        fields = path.read_text().split(") ", 1)[1].split()
        return {"pid": pid, "state": fields[0], "start_ticks": int(fields[19])}
    except FileNotFoundError:
        return None


def same_worker(captured, pod_uid):
    return not pod_uid or not captured.get("pod_uid") or captured["pod_uid"] == pod_uid


def inventory(directory):
    return [
        {"path": str(path.relative_to(directory)), "bytes": path.stat().st_size, "sha256": digest(path)}
        for path in sorted(directory.rglob("*")) if path.is_file()
    ]


def network_rules():
    return subprocess.check_output(["iptables", "-t", "filter", "-S"], text=True, timeout=10).splitlines()


def configure_network_tools(plan):
    if plan.get("pod_local_tcp_locking"):
        os.environ["PATH"] = "/tools/usr/sbin:" + os.environ.get("PATH", "/usr/bin:/bin")


def own_tcp_lock_rule(line):
    words = shlex.split(line)
    try:
        return (
            words[:2] == ["-A", "INPUT"]
            and words[words.index("-j") + 1] == "DROP"
            and int(words[words.index("--mark") + 1], 0) == 0xC114
            and words[words.index("-s") + 1] in {"127.0.0.1", "127.0.0.1/32"}
            and words[words.index("-d") + 1] in {"127.0.0.1", "127.0.0.1/32"}
        )
    except (ValueError, IndexError):
        return False


def clean_own_tcp_locks(before):
    after = network_rules()
    removed = []
    for rule in after:
        if rule in before:
            continue
        if not own_tcp_lock_rule(rule):
            raise RuntimeError("unexpected Pod-local network rule change; refusing broad cleanup")
        subprocess.run(["iptables", "-t", "filter", "-D", *shlex.split(rule)[1:]], check=True, timeout=10)
        removed.append(rule)
    if network_rules() != before:
        raise RuntimeError("Pod-local filter rules were not restored to their initial state")
    return {"before": before, "after_helper": after, "removed_owned_locks": removed, "restored": True}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "checkpoint-helper":
        source = Path(os.environ.get("FS2_PROBE_PLATFORM_SOURCE", "/snapshot-source"))
        platform = load_platform(source)
        sys.argv = [str(source / "process_checkpoint.py"), *sys.argv[2:]]
        platform.main()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "restore"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--platform-source", type=Path, default=Path("/snapshot-source"))
    args = parser.parse_args()
    platform = load_platform(args.platform_source)
    if args.directory is None or args.plan is None:
        parser.error("capture/restore requires --directory and --plan")
    plan = json.loads(args.plan.read_text())
    configure_network_tools(plan)
    directory = args.directory.resolve()
    receipt_path = directory / (args.action + "-probe.json")
    if receipt_path.exists():
        parser.error("never overwrite a previous attempt's receipt")
    work = relative_path(directory, plan.get("work_directory", "work"))
    pod_uid = os.environ.get("FS2_PROBE_POD_UID", "")
    if not pod_uid:
        parser.error("Kubernetes Pod UID is required for independent-worker evidence")
    sys.path.insert(0, str(args.platform_source))
    from supervisor import bind_allocated_gpu, configure_runtime_cache, stop_restored_worker

    bind_allocated_gpu()
    configure_runtime_cache(directory)
    Path("/tmp/empty-criu-plugins").mkdir(exist_ok=True)
    receipt = {
        "action": args.action, "pod_uid": pod_uid,
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "plan_sha256": digest(args.plan), "status": "running",
        "independent_replica": False, "customer_path_tested": False,
        "probe_python": sys.executable,
    }
    child = None
    restore_attempted = False
    started = time.monotonic()
    try:
        if args.action == "capture":
            for item in plan["inputs"]:
                path = relative_path(directory, item["path"])
                if digest(path) != item["sha256"]:
                    raise ValueError(f"input hash differs: {item['path']}")
            for path in (directory, *directory.rglob("*")):
                os.chown(path, 10001, 10001)
            for _ in range(128):
                subprocess.run(["/bin/true"], check=True)
            with (directory / "worker.log").open("wb") as output:
                child = subprocess.Popen(
                    plan["argv"], cwd=work, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                    user=10001, group=10001, extra_groups=[],
                    env={**os.environ, **plan.get("environment", {})},
                )
            receipt["worker_before_capture"] = process_state(child.pid)
            while True:
                step = progress(directory, plan)
                native_checkpoint = plan.get("native_checkpoint")
                checkpoint_ready = not native_checkpoint or relative_path(directory, native_checkpoint).is_file()
                if step is not None and step >= plan["capture_after_step"] and checkpoint_ready:
                    break
                if child.poll() is not None or time.monotonic() - started > 120:
                    raise RuntimeError("worker did not reach the bounded native capture point")
                time.sleep(0.05)
            receipt["native_ready_seconds"] = time.monotonic() - started
            receipt["logged_step_before_capture"] = step
            command = ["capture", "--pid", str(child.pid), "--directory", str(directory / "images"), "--process-tree"]
        else:
            captured = json.loads((directory / "capture-probe.json").read_text())
            if captured.get("status") != "passed" or same_worker(captured, pod_uid):
                raise ValueError("restore requires a successful capture from a different Pod UID")
            if captured["plan_sha256"] != receipt["plan_sha256"]:
                raise ValueError("restore plan differs from captured continuation")
            for item in captured["checkpoint_inventory"]:
                path = relative_path(directory / "images", item["path"])
                if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
                    raise ValueError("checkpoint file bytes differ")
            receipt["donor_pod_uid"] = captured["pod_uid"]
            receipt["captured_logged_step"] = captured["captured_logged_step"]
            command = ["restore", "--directory", str(directory / "images")]
        original_network = network_rules() if plan.get("pod_local_tcp_locking") else None
        helper_started = time.monotonic()
        receipt["pre_helper_seconds"] = helper_started - started
        restore_attempted = args.action == "restore"
        helper = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "checkpoint-helper", *command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            env={**os.environ, "FS2_PROBE_PLATFORM_SOURCE": str(args.platform_source)},
        )
        timed_out = False
        try:
            helper_stdout, helper_stderr = helper.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(helper.pid, signal.SIGKILL)
            helper_stdout, helper_stderr = helper.communicate(timeout=20)
        receipt["checkpoint_helper_seconds"] = time.monotonic() - helper_started
        receipt["checkpoint_helper_returncode"] = helper.returncode
        receipt["checkpoint_helper_timed_out"] = timed_out
        (directory / (args.action + "-helper.json")).write_text(helper_stdout)
        (directory / (args.action + "-helper.stderr")).write_text(helper_stderr)
        if original_network is not None:
            receipt["pod_local_network_cleanup"] = clean_own_tcp_locks(original_network)
        if helper.returncode:
            raise RuntimeError("existing platform checkpoint helper failed; see retained receipt")
        if args.action == "capture":
            receipt["donor_exit_code"] = child.wait(timeout=20)
            receipt["donor_process_terminated"] = process_state(child.pid) is None
            if not receipt["donor_process_terminated"]:
                raise RuntimeError("donor still exists after persistent capture")
            receipt["captured_logged_step"] = progress(directory, plan)
            receipt["checkpoint_inventory"] = inventory(directory / "images")
            receipt["checkpoint_bytes"] = sum(item["bytes"] for item in receipt["checkpoint_inventory"])
            if receipt["checkpoint_bytes"] <= 0 or not any(item["path"].endswith(".img") for item in receipt["checkpoint_inventory"]):
                raise RuntimeError("no persisted CRIU images were produced")
            if plan.get("native_checkpoint"):
                shutil.copytree(work, directory / "native-restart-seed")
        else:
            pid = int((directory / "images/worker-pid").read_text())
            receipt["restored_process"] = process_state(pid)
            while True:
                step = progress(directory, plan)
                if step is not None and step > captured["captured_logged_step"]:
                    receipt["first_new_logged_step"] = step
                    receipt["restore_to_new_logged_step_seconds"] = time.monotonic() - helper_started
                    receipt["probe_start_to_new_logged_step_seconds"] = time.monotonic() - started
                    break
                state = process_state(pid)
                if state is None or state["state"] == "Z" or time.monotonic() - helper_started > 180:
                    raise RuntimeError("restored worker did not produce a later native step")
                time.sleep(0.05)
            while (state := process_state(pid)) is not None and state["state"] != "Z":
                if time.monotonic() - helper_started > 240:
                    raise RuntimeError("restored finite fixture exceeded its bounded completion time")
                time.sleep(0.1)
            receipt["final_logged_step"] = progress(directory, plan)
            if receipt["final_logged_step"] != plan["expected_final_step"]:
                raise RuntimeError("restored process did not finish the requested native steps")
            receipt["native_output_validation_required"] = True
        receipt["status"] = "passed"
    except Exception as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
    finally:
        if restore_attempted and receipt["status"] != "passed":
            stop_restored_worker(directory)
        if child is not None and child.poll() is None:
            try:
                cohort = platform.process_tree(child.pid)
            except FileNotFoundError:
                cohort = []
            for pid in cohort:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=20)
        receipt["total_probe_seconds"] = time.monotonic() - started
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

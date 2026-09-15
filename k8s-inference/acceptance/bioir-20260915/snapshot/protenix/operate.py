#!/usr/bin/env python3
"""Collect exact owned Protenix lifecycle; capture drained actual CUDA worker."""
import argparse
import copy
import hashlib
import io
import json
import subprocess
import tarfile
import time
from control import K, NS, ROOT, LANE, LABELS, CONTAINER, PYTHON, k, save, apply, preflight


def pod(name):
    value = json.loads(k("-n", NS, "get", "pod", name, "-o", "json"))
    assert all(value["metadata"]["labels"].get(key) == expected for key, expected in LABELS.items())
    return value


def directory(value):
    command = value["spec"]["containers"][0]["command"]
    return command[command.index("--directory") + 1]


def execute(name, *args, **kwargs):
    return k("-n", NS, "exec", name, "-c", CONTAINER, "--", *args, **kwargs)


def collect(name):
    value = pod(name)
    save("lifecycle/" + name + "-observed.json", value)
    save("lifecycle/" + name + "-events.json", json.loads(k("-n", NS, "get", "events", "--field-selector", "involvedObject.name=" + name, "-o", "json")))
    base = directory(value)
    target = ROOT / "raw" / name
    target.mkdir(parents=True, exist_ok=True)
    for suffix, path in [("worker.log", base + "/worker.log"), ("supervisor.log", None), ("dump.log", base + "/images/dump.log"), ("restore.log", "/tmp/fs2-checkpoint-work/restore.log"), ("compatibility.json", base + "/images/compatibility.json")]:
        try:
            text = k("-n", NS, "logs", name, "-c", CONTAINER) if path is None else execute(name, "cat", path, stderr=subprocess.DEVNULL)
            (target / suffix).write_text(text)
        except subprocess.CalledProcessError:
            pass
    if value["status"]["phase"] == "Running":
        try:
            (target / "storage-bytes.txt").write_text(execute(name, "du", "-sb", base + "/images", base + "/cache", base + "/runtime-cache", stderr=subprocess.DEVNULL))
        except subprocess.CalledProcessError:
            pass
        with (target / "requests.tgz").open("wb") as stream:
            subprocess.run(K + ["-n", NS, "exec", name, "-c", CONTAINER, "--", "tar", "-czf", "-", "-C", "/mnt/fs2-scientific", "results", "baseline"], stdout=stream, stderr=subprocess.DEVNULL, check=False)


def ready(name):
    start = time.time()
    script = 'import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=2))))'
    while time.time() - start < 1200:
        value = pod(name)
        node = json.loads(k("--request-timeout=10s", "get", "node", value["spec"]["nodeSelector"]["kubernetes.io/hostname"], "-o", "json"))
        if not any(condition["type"] == "Ready" and condition["status"] == "True" for condition in node["status"]["conditions"]):
            save("incident/" + name + "-node.json", {"unix": time.time(), "node": node, "pod": value})
            raise RuntimeError("Assigned node stopped being Ready; preserve cohort and halt without forced cleanup")
        if value["status"]["phase"] == "Failed":
            collect(name)
            raise RuntimeError("Probe failed")
        try:
            health = json.loads(execute(name, PYTHON, "-c", script, stderr=subprocess.DEVNULL))
            assert health["ready"]
            break
        except (subprocess.CalledProcessError, ValueError):
            time.sleep(3)
    else:
        collect(name)
        raise RuntimeError("Readiness timeout")
    save("lifecycle/" + name + "-ready.json", {"unix": time.time(), "observed_wait_seconds": time.time() - start, "health": health, "pod": value})
    print("READY", name, json.dumps(health), flush=True)


def request(name, cases, label):
    pod(name)
    path = ROOT / "raw" / name
    path.mkdir(parents=True, exist_ok=True)
    # Reuse the exact baseline's validated CPU handoffs, as in the model lane.
    # The GPU candidate is not a CPU-prep image (it lacks archive-tool zstd).
    payload = io.BytesIO()
    files = []
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for case in ["ubiquitin-76", "lysozyme-129", "lysozyme-homodimer-258"]:
            for filename in ["input.json", "processed.json", "provenance.json", "localization-pred.json", "prediction-command.json"]:
                source = LANE / "raw/current-h100" / case / filename
                content = source.read_bytes()
                relative = "baseline/" + case + "/" + filename
                info = tarfile.TarInfo(relative)
                info.size, info.uid, info.gid, info.mode = len(content), 10001, 10001, 0o644
                archive.addfile(info, io.BytesIO(content))
                files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    start = time.time()
    subprocess.run(K + ["-n", NS, "exec", "-i", name, "-c", CONTAINER, "--", "tar", "-xf", "-", "-C", "/mnt/fs2-scientific"], input=payload.getvalue(), check=True)
    save("raw/" + name + "/" + label + "-fixture-transfer.json", {"seconds": time.time() - start, "files": files, "source": "exact current-model lane validated CPU handoff; no preprocessing bypass"})
    with (path / (label + ".jsonl")).open("w") as out, (path / (label + "-stderr.log")).open("w") as err:
        result = subprocess.run(K + ["-n", NS, "exec", name, "-c", CONTAINER, "--", PYTHON, "/evaluation/request.py", "--cases", cases, "--label", label], stdout=out, stderr=err)
    rows = [json.loads(line) for line in (path / (label + ".jsonl")).read_text().splitlines()]
    print(json.dumps({"pod": name, "label": label, "returncode": result.returncode, "attempts": [{key: r.get(key) for key in ["case", "status", "wall_seconds", "sequence_valid", "graph_verified"]} for r in rows]}), flush=True)
    collect(name)
    assert result.returncode == 0 and len(rows) == len(cases.split(","))


def capture(name):
    value = pod(name)
    base = directory(value)
    path = ROOT / "raw" / name
    # HTTPServer serializes requests. This synchronous call follows completed
    # validated predictions and drains CUDA before its actual owning PID dump.
    script = 'import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/prepare-snapshot",data=b"{}",headers={"Content-Type":"application/json"}),timeout=120))))'
    save("raw/" + name + "/drain.json", json.loads(execute(name, PYTHON, "-c", script)))
    script = 'import pathlib,subprocess; p=pathlib.Path(' + repr(base) + '); pid=int((p/"live-worker-pid").read_text()); assert pid>1; raise SystemExit(subprocess.call([' + repr(PYTHON) + ',"/snapshot-source/serving_checkpoint.py","capture","--process-tree","--pid",str(pid),"--directory",str(p/"images")]))'
    start = time.time()
    with (path / "capture-stdout.json").open("w") as out, (path / "capture-stderr.log").open("w") as err:
        result = subprocess.run(K + ["-n", NS, "exec", name, "-c", CONTAINER, "--", PYTHON, "-c", script], stdout=out, stderr=err)
    save("lifecycle/" + name + "-capture.json", {"started_unix": start, "completed_unix": time.time(), "returncode": result.returncode})
    collect(name)
    print("CAPTURE", name, result.returncode, flush=True)
    assert result.returncode == 0


def release(name):
    collect(name)
    save("lifecycle/" + name + "-before-delete.json", pod(name))
    started = time.time()
    print(k("-n", NS, "delete", "pod", name, "--wait=true", "--timeout=180s"), flush=True)
    save("lifecycle/" + name + "-released.json", {"unix": time.time(), "delete_started_unix": started, "pod": name})
    preflight(name + "-released")


def restore(source, name, fallback=False):
    assert (ROOT / "lifecycle" / (source + "-released.json")).is_file(), "Donor must be deleted first"
    source = json.loads((ROOT / "lifecycle" / (source + "-before-delete.json")).read_text())
    preflight(name)
    value = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS, "labels": LABELS}, "spec": copy.deepcopy(source["spec"])}
    spec = value["spec"]
    spec.pop("nodeName", None)
    runtime = spec["containers"][0]
    command = runtime["command"]
    base = directory(value)
    relative = base.removeprefix("/checkpoints/")
    command[command.index("donor"):command.index("donor") + 1] = ["--source-directory", "/snapshot-bundle", "restore"]
    if fallback:
        command[command.index("--fallback") + 1] = "normal-load"
        next(v for v in runtime["env"] if v["name"] == "FS2_MODEL_REVISION")["value"] = "intentional-mismatch-fallback-test"
    volume = next(v for v in spec["volumes"] if v["name"] == "snapshot-checkpoints")
    claim = volume["persistentVolumeClaim"]["claimName"]
    volume.clear()
    volume.update(name="snapshot-checkpoints", emptyDir={})
    spec["volumes"].append({"name": "snapshot-bundle", "persistentVolumeClaim": {"claimName": claim, "readOnly": True}})
    bundle = {"name": "snapshot-bundle", "mountPath": "/snapshot-bundle", "subPath": relative, "readOnly": True}
    runtime["volumeMounts"] += [bundle, {"name": "snapshot-bundle", "mountPath": base + "/images", "subPath": relative + "/images", "readOnly": True}]
    init = spec["initContainers"][0]
    init["volumeMounts"].append(bundle)
    init["command"][2] = init["command"][2].replace('"$1/cache" ', '') + ' && for part in runtime-cache tmp; do cp -a /snapshot-bundle/$part/. "$1/$part/"; done'
    apply(name, value)
    save("lifecycle/" + name + "-created.json", {"unix": time.time(), "source_pod": source["metadata"]["name"], "variant": "fallback" if fallback else "restore"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["ready", "request", "capture", "release", "restore", "collect"])
    parser.add_argument("name")
    parser.add_argument("--source")
    parser.add_argument("--cases", default="ubiquitin-76,lysozyme-129")
    parser.add_argument("--label", default="measured")
    parser.add_argument("--fallback", action="store_true")
    args = parser.parse_args()
    if args.action == "request":
        request(args.name, args.cases, args.label)
    elif args.action == "restore":
        restore(args.source, args.name, args.fallback)
    else:
        globals()[args.action](args.name)

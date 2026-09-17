"""Run one isolated, pinned comparator; retain evidence before deleting its pod."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KUBE = ["kubectl", "--kubeconfig", "/home/tux/.local/state/k8s-inference-dual-acceptance/h100/run/kubeconfig", "-n", "fs2-bioir-boltz2"]
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-evaluation/bioir-public@sha256:2fcd9e8fff2e1c20e676657bedc56689ded0abb2fc9863618398390ed22805ce"


def capture(tag, command):
    subprocess.run([sys.executable, str(ROOT / "capture.py"), tag, *command], check=True)


def collect(pod, prefix, paths):
    capture(prefix + "-final-pod", [*KUBE, "get", "pod", pod, "-o", "json"])
    capture(prefix + "-logs", [*KUBE, "logs", pod])
    capture(prefix + "-gpu", [*KUBE, "exec", pod, "--", "nvidia-smi", "--query-gpu=uuid,name,driver_version,memory.used,memory.total,utilization.gpu", "--format=csv"])
    capture(prefix + "-environment", [*KUBE, "exec", pod, "--", "python", "-m", "pip", "freeze"])
    for item in paths:
        capture(prefix + "-copy-" + item, [*KUBE, "cp", pod + ":/models/evidence/" + item, str(ROOT / "evidence" / item)])
    capture(prefix + "-delete", [*KUBE, "delete", "pod", pod, "--wait=true"])


def run(variant, gpu):
    name = f"{variant}-{gpu}"
    pod = "boltz2-" + name
    subprocess.run([sys.executable, str(ROOT / "render_variant.py"), variant, gpu, IMAGE], check=True)
    capture(name + "-create", [*KUBE, "apply", "-f", str(ROOT / (name + ".json"))])
    paths = []
    for suffix, extra in [("matrix", []), ("samples2", ["--samples", "2", "--cases", "T1031", "--repetitions", "1"]),
                          ("heteromer", ["--cases", "7sfy_ac", "--repetitions", "3"])]:
        output = name + "-" + suffix
        capture(output + "-benchmark", [*KUBE, "wait", "--for=condition=Ready", "pod/" + pod, "--timeout=1800s"])
        capture(output + "-requests", [*KUBE, "exec", pod, "--", "python", "/models/benchmark_http.py", "--fixtures", "/models/fixtures", "--out", "/models/evidence/" + output, "--variant", output, *extra])
        paths.append(output)
    schema = name + "-schema.json"
    capture(name + "-schema", [*KUBE, "exec", pod, "--", "python", "/models/feature_errors.py", "/models/evidence/" + schema])
    paths.append(schema)
    if variant == "persistent-tools" and gpu == "l40s":
        capture("final-quality-audit", [*KUBE, "exec", pod, "--", "python", "/models/quality_audit.py",
                                       "--fixtures", "/models/fixtures", "--evidence", "/models/evidence",
                                       "--out", "/models/evidence/quality-audit.json"])
        paths.append("quality-audit.json")
        capture("final-runtime-hashes", [*KUBE, "exec", pod, "--", "sha256sum", "/models/boltz2_conf.ckpt",
                                         "/models/server.py", "/models/bir_worker.py", "/models/persistent_upstream.py",
                                         "/models/benchmark_http.py", "/models/quality_audit.py"])
    collect(pod, name, paths)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("gpu", choices=["h100", "l40s"])
    parser.add_argument("variants", nargs="+", choices=["current", "persistent", "persistent-kernels", "persistent-cue", "persistent-tools", "bir"])
    args = parser.parse_args()
    for variant in args.variants:
        run(variant, args.gpu)

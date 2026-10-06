"""One-GPU Hopper experiments using the existing owned-pod lifecycle."""

import argparse
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
COMMON = HERE.parent / "l40s-incremental-20261006"
sys.path.insert(0, str(COMMON))
import run_experiment as supervisor


def specification(args):
    pools = {"H100": ("h100-1x", "h100-ondemand-1x", "h100-reserved-8x"),
             "H200": ("wan2-h200-1x",)}
    return supervisor.ExperimentSpec(
        task="hopper-single-gpu-20261006", pod_prefix="fs2-lynx-perf-cpu-hopper-",
        allowed_pools=pools[args.gpu], allowed_cpus=(8, 16, 32),
        worker=HERE / "hopper_inside.py",
        extra_sources=((COMMON / "experiment_inside.py", "experiment_inside.py"),),
        worker_args=("--gpu", args.gpu, "--cpu-budget", str(args.cpus)),
        purpose="isolated single-GPU Hopper GROMACS optimization")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("node", "name"):
        parser.add_argument("--" + field, required=True)
    for field in ("tpr", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    parser.add_argument("--gpu", choices=("H100", "H200"), required=True)
    parser.add_argument("--cpus", type=int, choices=(8, 16, 32), default=8)
    parser.add_argument("--mode", choices=("tune", "cpu-envelope", "profile"), required=True)
    parser.add_argument("--profile-tools", type=Path)
    parser.add_argument("--context", default="nebius-mk8s-k8s-inference-h100-e00j5z9te7x5dd9g6a")
    args = parser.parse_args()
    raise SystemExit(supervisor.run(args, specification(args)))

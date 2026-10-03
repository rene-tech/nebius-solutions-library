"""Build immutable fixed-input CPU/GPU PME controls, never submit or tune physics."""
import argparse
import copy
import json
from pathlib import Path
import shutil

from qualify_candidate import normalize, save, sha


def control_request(request, gpus_per_node, pme, nodes=1):
    if pme not in ("cpu", "gpu"):
        raise ValueError("explicit CPU or GPU PME only")
    value = copy.deepcopy(request)
    if "mpi-workflow" not in value["schema"]:
        raise ValueError("matched controls use the exact same external-MPI engine")
    value.update(nodes=nodes, gpus_per_node=gpus_per_node)
    options = {"-nb": "gpu", "-update": "cpu", "-bonded": "cpu", "-pme": pme,
               "-pmefft": pme, "-npme": "0" if pme == "cpu" or nodes * gpus_per_node == 1 else "1"}
    runs = []
    for job in value["jobs"]:
        for step in job["steps"]:
            if step["command"] != "mdrun":
                continue
            old = step["args"]
            if "-resethway" in old:
                raise ValueError("do not mix warm-up-inclusive controls with forced counter reset")
            args, i = [], 0
            while i < len(old):
                if old[i] in options:
                    if i + 1 == len(old) or not isinstance(old[i + 1], str) or old[i + 1].startswith("-"):
                        raise ValueError("missing native option value")
                    i += 2
                elif old[i] in ("-tunepme", "-notunepme"):
                    i += 1
                else:
                    args.append(old[i])
                    i += 1
            for flag, value_ in options.items():
                args += [flag, value_]
            step["args"] = args + ["-notunepme"]
            runs.append(step)
    if len(runs) != 3:
        raise ValueError("bounded control requires exactly three original timing repetitions")
    return normalize(value, mpi=True)


def prepare(source, output, gpus_per_node, pme, nodes=1):
    request = control_request(json.loads((source / "request.json").read_text()), gpus_per_node, pme, nodes)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source / "input.tar.gz", output / "input.tar.gz")
    save(output / "request.json", request)
    save(output / "fixture.json", {
        "protocol": f"fixed-tpr-{pme}-pme-v1", "source_directory": str(source),
        "source_request_sha256": sha(source / "request.json"), "input_sha256": sha(output / "input.tar.gz"),
        "request_sha256": sha(output / "request.json"), "nodes": nodes, "gpus_per_node": gpus_per_node,
        "changed": "Native task placement and PP/PME autotuning only. Original TPR/bundle bytes, finite step count, seeds, timestep, constraints and output cadence unchanged.",
        "warmup": "included; no forced counter reset", "pme_tuning": False,
        "multiple_gpu_fft_requested": False, "public_speedup_claimed": False,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpus-per-node", type=int, choices=(1, 2, 4, 8), required=True)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument("--pme", choices=("cpu", "gpu"), required=True)
    args = parser.parse_args()
    prepare(args.source, args.output, args.gpus_per_node, args.pme, args.nodes)
    print(json.dumps({"fixture": str(args.output), "request_sha256": sha(args.output / "request.json"),
                      "input_sha256": sha(args.output / "input.tar.gz"), "submitted": False}))


if __name__ == "__main__":
    main()

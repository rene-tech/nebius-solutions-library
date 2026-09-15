#!/usr/bin/env python3
"""Render candidate resources without mutating a live resource or serving route."""
import argparse
import copy
import json
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--name", default="protenix-bir-h100")
    parser.add_argument("--graphs", choices=("0", "1"), default="0")
    parser.add_argument("--backend", choices=("bir", "native"), default="bir")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    solution = here.parents[2]
    pod = copy.deepcopy(list(yaml.safe_load_all((here / "baseline.yaml").read_text()))[1])
    pod["metadata"]["name"] = args.name
    pod["metadata"]["labels"]["variant"] = args.backend
    pod["spec"]["nodeSelector"]["kubernetes.io/hostname"] = args.node
    cm_name = args.name + "-source"
    snapshot_source = solution / "models/scientific-snapshot"
    data = {name: (snapshot_source / name).read_text() for name in (
        "protenix_server.py", "scientific_server.py", "protenix_cli_proxy.py",
    )}
    data["bir_server.py"] = (here / "bir_server.py").read_text()
    if args.backend == "native":
        data["sitecustomize.py"] = (solution / "acceptance/scientific-startup/current/structure_sitecustomize.py").read_text()
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": cm_name, "namespace": pod["metadata"]["namespace"],
        "labels": pod["metadata"]["labels"],
    }, "data": data}
    runtime = pod["spec"]["containers"][0]
    runtime["image"] = args.image
    entrypoint = "bir_server.py" if args.backend == "bir" else "protenix_server.py"
    runtime["command"] = ["/opt/protenix-venv/bin/python", "/evaluation/" + entrypoint]
    runtime["env"].extend([
        {"name": "BIOIR_GRAPHS", "value": args.graphs},
        {"name": "FS2_PROTENIX_WORKER_URL", "value": "http://127.0.0.1:8000"},
        {"name": "TRITON_CACHE_DIR", "value": "/cache/triton"},
        {"name": "CUEQ_TRITON_CACHE_DIR", "value": "/cache/cueq"},
        {"name": "TORCH_EXTENSIONS_DIR", "value": "/cache/torch-extensions"},
        {"name": "XDG_CACHE_HOME", "value": "/cache/xdg"},
        {"name": "BIOIR_CACHE", "value": "/cache/bioir"},
        {"name": "MPLCONFIGDIR", "value": "/cache/matplotlib"},
    ])
    if args.backend == "native":
        runtime["env"].extend([
            {"name": "PYTHONPATH", "value": "/evaluation"},
            {"name": "FS2_STARTUP_BENCHMARK_MODEL", "value": "protenix-v2"},
        ])
    runtime["volumeMounts"].extend([
        {"name": "source", "mountPath": "/evaluation", "readOnly": True},
        {"name": "source", "mountPath": "/opt/protenix-venv/bin/protenix",
         "subPath": "protenix_cli_proxy.py", "readOnly": True},
    ])
    pod["spec"]["volumes"].append({"name": "source", "configMap": {
        "name": cm_name, "defaultMode": 365,
    }})
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [cm, pod]}, indent=2))


if __name__ == "__main__":
    main()

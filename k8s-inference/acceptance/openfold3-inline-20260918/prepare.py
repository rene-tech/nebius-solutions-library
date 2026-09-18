#!/usr/bin/env python3
"""Adapt the existing semantic renderer to one route-free, sequential H100 Pod."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shlex
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "84a466792feb69ac852ffdb6124c47706751646c"
CASES = (("1acb", 1), ("1brs", 7), ("2ptc", 42))


def load_renderer():
    path = ROOT / "models/cancer-immunotherapy/images/structure-secondary/qualification/render_semantic_job.py"
    spec = importlib.util.spec_from_file_location("existing_openfold3_renderer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(original, fixtures, image, output, name):
    base = load_renderer()
    old = original["spec"]["containers"][0]
    marker = json.loads(next(e["value"] for e in old["env"] if e["name"] == "FS2_RUNTIME_ARTIFACTS_JSON"))
    mounts = {m["mountPath"]: m for m in old["volumeMounts"]}
    volumes = {v["name"]: v for v in original["spec"]["volumes"]}
    artifacts = []
    for item in marker["artifacts"]:
        mount = mounts[item["mount_path"]]
        if not mount.get("readOnly"):
            raise ValueError("Runtime artifacts must remain read-only")
        artifacts.append({**item, "object_sub_path": mount["subPath"]})
    host_paths = {volumes[mounts[a["mount_path"]]["name"]]["hostPath"]["path"] for a in artifacts}
    if len(host_paths) != 1:
        raise ValueError("Expected the same original public artifact root")
    binding = {"schema": "fs2.nebius.ai/structure-secondary-live-artifact-bindings/v1",
               "source_commit": SOURCE, "public_storage": {"host_path": host_paths.pop()},
               "models": {base.MODEL_ID: {"artifact_binding_state": "complete", "image": image,
                          "variant_id": marker["variant_id"], "stage_id": "inference", "artifacts": artifacts}}}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding_path = output / "artifact-bindings.json"
    binding_path.write_text(json.dumps(binding, indent=2)+"\n")
    rendered = base.render(SimpleNamespace(bindings=binding_path, namespace="fs2-models", task_id="openfold3-inline-20260918"))
    config, job = rendered["items"]
    config["metadata"]["name"] = name+"-input"
    config["data"].pop("raw-input.json")
    spec = job["spec"]["template"]["spec"]
    spec["nodeSelector"] = copy.deepcopy(original["spec"]["nodeSelector"])
    spec["securityContext"] = copy.deepcopy(original["spec"]["securityContext"])
    # The task-owned emptyDir volumes need the same non-root writer UID.
    spec["securityContext"]["fsGroup"] = 10001
    spec["tolerations"] = copy.deepcopy(original["spec"].get("tolerations", []))
    spec["activeDeadlineSeconds"] = 7200
    for volume in spec["volumes"]:
        if volume.get("configMap"):
            volume["configMap"]["name"] = config["metadata"]["name"]
    container = spec["containers"][0]
    container["resources"] = copy.deepcopy(old["resources"])
    container["securityContext"] = copy.deepcopy(old["securityContext"])
    if original["spec"].get("imagePullSecrets"):
        spec["imagePullSecrets"] = copy.deepcopy(original["spec"]["imagePullSecrets"])
    checks = r'''
set -eo pipefail
source /opt/fs2/activate.sh
set -u
mkdir -p /work/home /cache/openfold3/triton /cache/openfold3/torch-extensions /cache/openfold3/xdg
test "$(df -B1 --output=size /dev/shm | tail -1 | tr -d ' ')" = 67108864
nvidia-smi --query-gpu=uuid,name,driver_version,memory.total --format=csv,noheader > /outputs/gpu.csv
df -B1 /dev/shm > /outputs/shm.txt
'''
    for artifact in artifacts:
        for file in artifact.get("files", []):
            path = str(Path(artifact["mount_path"]) / file["path"])
            digest = file["digest"].removeprefix("sha256:")
            checks += f'test "$(sha256sum {shlex.quote(path)} | cut -d\' \' -f1)" = "{digest}"\n'
            checks += f'sha256sum {shlex.quote(path)} >> /outputs/runtime-file-hashes.txt\n'
    commands, records = [checks], []
    for pdb, seed in CASES:
        case = f"{pdb}-s{seed}"
        path = fixtures / "references" / pdb / f"openfold3-openbind-{pdb}-heteromer-s{seed}-input.json"
        raw = path.read_text()
        value = json.loads(raw)
        chains = next(iter(value["queries"].values()))["chains"]
        raw_hash = hashlib.sha256(raw.encode()).hexdigest()
        config["data"][case+".json"] = raw
        base.SEED = seed
        command = base._command(image=image, raw_sha256=raw_hash, token=case, source_commit=SOURCE)
        command = command.replace("/var/run/fs2-source/raw-input.json", f"/var/run/fs2-source/{case}.json")
        command = command.replace("/work/prepared", f"/work/{case}/prepared")
        command = command.replace("/outputs/semantic", f"/outputs/{case}/semantic")
        # Parse the emitted policy through the exact installed upstream config,
        # not only the wrapper's YAML parser, before consuming GPU work.
        before_gpu = f'''PYTHONPATH=/opt/fs2 python -c 'import json,yaml; from openfold3.entry_points.validator import InferenceExperimentConfig; c=InferenceExperimentConfig.model_validate(yaml.safe_load(open("/work/{case}/prepared/runner.yaml"))); assert c.data_module_args.num_workers == 0; assert c.data_module_args.prefetch_factor is None; assert c.data_module_args.persistent_workers is False; assert c.experiment_settings.seeds == [{seed}]; print("CONFIG_ACCEPTED {case}")'
'''
        command = command.replace('export FS2_QUALIFICATION_STARTED_EPOCH=', before_gpu+'export FS2_QUALIFICATION_STARTED_EPOCH=')
        commands.append(f'echo "CASE_START {case}"\n'+command+f'\necho "CASE_COMPLETE {case}"\n')
        records.append({"case_id": case, "raw_sha256": raw_hash, "seed": seed,
                        "chain_lengths": [len(c["sequence"]) for c in chains], "input_path": str(path)})
    commands.append('touch /outputs/COMPLETE\necho "ALL_CASES_COMPLETE"\nsleep 1800\n')
    container["args"] = ["/bin/bash", "-lc", "\n".join(commands)]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": "fs2-models",
           "labels": job["spec"]["template"]["metadata"]["labels"],
           "annotations": job["metadata"]["annotations"]}, "spec": spec}
    result = {"apiVersion": "v1", "kind": "List", "items": [config, pod]}
    (output / "candidate.json").write_text(json.dumps(result, indent=2)+"\n")
    (output / "cases.json").write_text(json.dumps({"source": SOURCE, "image": image,
        "cases": records, "scope": "isolated model compatibility; not public owner/API qualification",
        "shm_bytes": 67108864, "original_resources": old["resources"],
        "changes": ["candidate image", "inline DataLoader", "task-only input/output/cache volumes", "sequential fixture commands", "fsGroup10001 for emptyDir ownership"]}, indent=2)+"\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-pod", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--name", default="fs2-openfold3-inline-20260918")
    args = parser.parse_args()
    prepare(json.loads(args.original_pod.read_text()), args.fixtures, args.image, args.output, args.name)


if __name__ == "__main__":
    main()

"""Render only the allocated evaluation GPU pods; never modifies production."""
import copy
import json
import sys
from pathlib import Path

import yaml

root = Path(__file__).resolve().parent
variant, gpu = sys.argv[1:3]
if variant not in ("current", "persistent", "persistent-kernels", "persistent-cue", "persistent-tools", "bir") or gpu not in ("h100", "l40s"):
    raise ValueError("unknown variant/hardware")
pod = list(yaml.safe_load_all((root / "baseline.yaml").read_text()))[-1]
pod["metadata"]["name"] = f"boltz2-{variant}-{gpu}"
pod["metadata"]["labels"]["variant"] = variant
pod["spec"]["nodeSelector"]["kubernetes.io/hostname"] = {
    "h100": "computeinstance-e00fkt1bsa4ec657sn", "l40s": "computeinstance-e00ax3mgt7y3a0asa6"}[gpu]
container = pod["spec"]["containers"][0]
pod["spec"]["securityContext"]["fsGroupChangePolicy"] = "OnRootMismatch"
if gpu == "l40s":
    # Assigned L40S hosts have 64GB RAM, below live H100's 96Gi request.
    # All L40S variants share this explicit resource-limit cohort.
    container["resources"]["requests"]["memory"] = "48Gi"
    container["resources"]["limits"]["memory"] = "56Gi"
if variant.startswith("persistent"):
    container["command"] = ["python", "-m", "uvicorn", "persistent_upstream:app", "--app-dir", "/models",
                            "--host", "0.0.0.0", "--port", "8000"]
    container["env"].append({"name": "EVAL_NO_KERNELS", "value": "1" if variant == "persistent" else "0"})
    if variant == "persistent-tools":
        container["image"] = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-evaluation/boltz2-upstream-tools@sha256:1f1e94c14d409cb35830cca461bc1160955a4ca421eaf7d271aa908fdeec1415"
    if variant == "persistent-cue":
        target = f"/models/cue081-{gpu}"
        container["env"].append({"name": "PYTHONPATH", "value": target})
        pod["spec"]["initContainers"] = [{
            "name": "pinned-optional-kernels", "image": container["image"],
            "command": ["python", "-m", "pip", "install", "--no-deps", "--target", target,
                        "--report", f"/models/evidence/cue081-{gpu}-install.json",
                        "cuequivariance==0.8.1", "cuequivariance-torch==0.8.1",
                        "cuequivariance-ops-cu13==0.8.1", "cuequivariance-ops-torch-cu13==0.8.1",
                        "opt-einsum==3.4.0", "nvidia-ml-py==13.610.43"],
            "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "2Gi"}},
            "securityContext": container["securityContext"],
            "volumeMounts": [{"name": "cache", "mountPath": "/models"}],
        }]
elif variant == "bir":
    container["image"] = sys.argv[3]
    if "@sha256:" not in container["image"]:
        raise ValueError("candidate requires resolved digest")
    container["command"] = ["sh", "-c", "python /models/gpu_preflight.py && exec python -m uvicorn bir_worker:app --app-dir /models --host 0.0.0.0 --port 8000"]
    container["env"] += [{"name": key, "value": value} for key, value in {
        "BOLTZ_SOURCE_REVISION": "401c6fcc4a43925bcf1342b6c0979b060130b396",
        "BOLTZ_MODEL_REPOSITORY": "boltz-community/boltz-2",
        "BOLTZ_MODEL_REVISION": "6fdef46d763fee7fbb83ca5501ccceff43b85607",
        "BOLTZ_CONF_SHA256": "090e82ac8c92f5e943fa1b39e7410a44027bea7243c0bbb3caa67a77fc1428e1",
        "BOLTZ_AFF_SHA256": "dcc5cd3722b1c9eaa34267e4ae32f55cbbf1963f4c19319381ccfa30fdd2ca9e",
        "BOLTZ_MOLS_SHA256": "39e076d96dbec6b4e86982bbda16f3a53a2a60c9bdc17828d88f6f9a0c7d1fd7",
        "BOLTZ2_CKPT": "/models/boltz2_conf.ckpt",
        "BOLTZ_MOL_DIR": "/models/mols",
        "BIOIR_CACHE": "/models/bioir-cache",
    }.items()]
path = root / f"{variant}-{gpu}.json"
path.write_text(json.dumps(pod, indent=2) + "\n")
print(path)

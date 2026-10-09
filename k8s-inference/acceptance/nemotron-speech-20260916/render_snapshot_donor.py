"""Render isolated speech donors using the platform's existing snapshot tools."""

import argparse
import json
import sys
from pathlib import Path

SNAPSHOTS = Path(__file__).resolve().parent.parent / "h100-fleet/snapshots"
sys.path.insert(0, str(SNAPSHOTS))
from render_serving_probe import render as initial  # noqa: E402
from render_serving_recapture import render as recapture  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--model", choices=("en", "multi"), required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from fs2_speech.contracts import MODELS, RuntimeProfile
    from fs2_speech.probe_job import render_job

    model = "nemotron-speech-en-0.6b" if args.model == "en" else "nemotron-speech-multilingual-0.6b"
    profile = RuntimeProfile(model=model)
    base = render_job(name="fs2-speech-probe-snapshot-" + args.model, namespace="fs2-models",
                      image=args.image, profile=profile, gpu_class="nvidia-h100-sxm5-80gb")
    source = {"metadata": base["metadata"], "spec": {"template": base["spec"]["template"]}}
    runtime = source["spec"]["template"]["spec"]["containers"][0]
    runtime["name"] = "speech"
    runtime["command"] = ["python", "-m", "fs2_speech.server"]
    runtime.pop("args")
    runtime["env"].extend([
        {"name": "FS2_SPEECH_PROFILE_JSON", "value": profile.model_dump_json()},
        {"name": "FS2_SPEECH_ARTIFACT_HOSTS", "value": "storage.eu-north1.nebius.cloud"},
    ])
    tools = json.loads((SNAPSHOTS / "qwen3-8b-bundle.json").read_text())["tools_image"]
    source_map = "fs2-fleet-snapshot-serving-workdir-v8"
    name = "fs2-speech-snapshot-" + args.model + "-" + args.run
    config = argparse.Namespace(container="speech", entrypoint_json="[]", asyncio_loop=False,
                                python="/opt/conda/bin/python3", run=args.run, fallback="fail", request_uid=10001,
                                allow_device_remap=True, mode="donor", tools_image=tools,
                                model_revision=MODELS[model].revision, model_id=model,
                                source_configmap=source_map, pvc="fs2-fleet-snapshots-rwx-r20260907",
                                node=args.node, name=name)
    pod = recapture(initial(source, config), name=name, container="speech", run=args.run,
                    source_configmap=source_map, network_configmap="fs2-fleet-snapshot-net-tools-v1")
    runtime = pod["spec"]["containers"][0]
    next(item for item in runtime["env"] if item["name"] == "PATH")["value"] = (
        "/tools/usr/sbin:/opt/conda/bin:/usr/local/nvidia/bin:/usr/local/cuda/bin:"
        "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    separator = runtime["command"].index("--") + 1
    runtime["command"][separator:separator] = [
        "python3", "/snapshot-source/working_directory_launcher.py", "--directory", "/opt/fs2-speech",
        "--uid", "10001", "--gid", "10001", "--",
    ]
    pod["metadata"]["labels"]["workload.fs2.nebius/owner"] = "nemotron-speech-20260916"
    pod["spec"]["activeDeadlineSeconds"] = 7200
    encoded = json.dumps(pod, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()

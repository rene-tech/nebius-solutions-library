"""Emit an apply_patch binding Transfer's catalog to two native reference calls."""

import argparse
import copy
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CATALOG = ROOT / "catalog/runtime"
SOURCE = "a75a99b42db5d5dab8602bf492a70a395a5059dd"
TREE = "51809d320b9c70b4bc01276cbf6c43cd5ffdc463"


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    calls = []
    for index in (1, 2):
        folder = args.evidence_root / f"reference-transfer-native-r{index}"
        result = json.loads((folder / "result.json").read_text())
        quality = json.loads((folder / "quality.json").read_text())
        if not result["source_unchanged"] or result["http_status"] != 200 or result["inference_attempts"] != 1:
            raise ValueError("Transfer call was not exactly one successful unchanged-source request")
        if quality["motion"]["threshold"] != 0.6 or not result["output"]["frames"]:
            raise ValueError("NVIDIA reference evaluator or output differs")
        calls.append({"request": result, "quality": quality})
    if len({call["request"]["request_sha256"] for call in calls}) != 2 or len({call["request"]["output"]["sha256"] for call in calls}) != 2:
        raise ValueError("Two distinct requests and outputs are required")
    evidence = {"schema": "nvidia-paidf-native-transfer-qualification/v1", "calls": calls,
                "native_transport_qualified": True, "public_app_qualified": False,
                "workbench_qualified": False, "failed_outputs_retained": True,
                "limitations": ["Cloudy output failed the original attribute verifier and remains rejected.",
                                "The separate original snow-falling/night example passed both original evaluators.",
                                "Source MP4 bytes, reference prompts, seed and generation parameters were not modified or retried."]}
    source_path = "k8s-inference/models/general-media/paidf-chat/qualify_transfer.py"
    fixture_path = "k8s-inference/models/general-media/paidf-chat/reference-transfer-qualification-20260920.json"
    outputs = {ROOT.parent / fixture_path: json.dumps(evidence, indent=2) + "\n",
               CATALOG / "packaged-repository" / source_path: (HERE / "qualify_transfer.py").read_text()}
    outputs[CATALOG / "packaged-repository" / fixture_path] = outputs[ROOT.parent / fixture_path]
    native_path = CATALOG / "native/cosmos-transfer2-5-2b.json"
    selected_path = CATALOG / "deployment-runtimes/cosmos-transfer2-5-2b.json"
    native = json.loads(native_path.read_text())
    selected = json.loads(selected_path.read_text())
    record = native["record"]
    record["semantic_validator"].update(
        contract="nvidia-paidf-original-transfer/v1", source_path=source_path,
        source_sha256=hashlib.sha256(outputs[CATALOG / "packaged-repository" / source_path].encode()).hexdigest(),
        fixture_path=fixture_path,
        fixture_sha256=hashlib.sha256(outputs[ROOT.parent / fixture_path].encode()).hexdigest())
    record["support"]["limitations"] = [
        "Internal isolated evaluation, not customer-ready; public gateway/MCP, skill/reasoner, chat preview and human-approved batch are separate gates.",
        "Exactly one H100, the pinned NIM/profile and immutable adapter " + calls[0]["request"]["adapter_image"] + ". No B300, autoscaling or snapshot qualification.",
        "Native input: MP4, 93-480 frames, arbitrary geometry. The platform's explicit 128 MiB upload/transport bound is not a NVIDIA model limit. Native output is silent.",
        "Original edge-conditioned reference: 35 steps, seed 42, guidance 7, resolution 720, sigma_max 90, edge weight 1; no input cropping, trimming, retiming or model/provider substitution.",
        "Original motion threshold 0.6; original one-frame attribute check. Motion failure skips attributes. Media geometry/timing differences are reported, not an invented acceptance gate.",
        "One in-flight transfer; unknown upstream completion blocks further work and no automatic generation retry occurs.",
        "Cloudy request: motion 0.938988 passed, cloudy verifier failed: REJECTED. Original snow/night example: motion 0.758833 and both attributes passed. Passing automated checks does not establish physical fidelity or annotation correctness; human review remains required.",
    ]
    record["evidence"] = [{"classification": "measured-platform", "hardware": "NVIDIA H100 80GB HBM3; driver 580.173.02",
                           "outcome": "live-qualified", "source_commit": SOURCE,
                           "summary": "Two distinct native requests returned complete decoded MP4s through the pinned adapter/NIM using the unchanged 153-frame 1920x1080 NVIDIA input. Receipt SHA256 " + digest(evidence) + ". Cloudy is rejected by the unchanged reference evaluator; the original snow/night case passed without override. This qualifies native model transport, not public App, managed cold start, workbench or batch acceptance."}]
    record["provenance"] = [record["provenance"][0],
                            {"commit": SOURCE, "tree": TREE, "path": source_path, "classification": "measured-handoff"},
                            {"commit": SOURCE, "tree": TREE, "path": "k8s-inference/models/general-media/cosmos-transfer25/adapter/app.py", "classification": "measured-handoff"}]
    native["semantic_requests"]["requests"] = [{"id": "paidf-transfer-reference-" + str(index), "payload_sha256": call["request"]["request_sha256"]} for index, call in enumerate(calls)]
    selected["record"] = copy.deepcopy(record)
    selected["qualification"]["evidence"].update(audited_catalog_sha256=digest(record), retained_deployments_sha256=digest(evidence))
    outputs[native_path] = json.dumps(native, indent=2) + "\n"
    outputs[selected_path] = json.dumps(selected, indent=2) + "\n"
    print("*** Begin Patch")
    for path, value in outputs.items():
        if path.exists():
            print("*** Update File: " + str(path))
            print("@@")
            print("\n".join("-" + line for line in path.read_text().splitlines()))
        else:
            print("*** Add File: " + str(path))
        print("\n".join("+" + line for line in value.rstrip("\n").split("\n")))
    print("*** End Patch")


if __name__ == "__main__":
    main()

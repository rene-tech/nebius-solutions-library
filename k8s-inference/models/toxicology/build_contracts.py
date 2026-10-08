"""Project exact runtime receipts into existing catalog and Terraform contracts.

No route is granted here. Public HTTP/MCP, elasticity and customer readiness
remain false until separate hosted acceptance. --check detects source drift.
"""

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path

from fixtures import semantic_requests
from toxicology.contracts import REQUEST_TYPES

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CATALOG = ROOT / "catalog/runtime"
REVISIONS = {"admet-ai": "c65bf0418e19c65d7228f9e40da5d0152aade756", "ctoxpred2": "2a31aa119e27b6b69a5588d18a01f2a27fef4524"}
SOURCES = {"admet-ai": "swansonk14/admet_ai", "ctoxpred2": "issararab/CToxPred2"}
NAMES = {"admet-ai": "ADMET-AI", "ctoxpred2": "CToxPred2"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def outputs(model_id, image, receipt, source_commit):
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit) or not re.fullmatch(r"[^@]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Require immutable source commit and image digest")
    if not receipt["passed"] or receipt["model_id"] != model_id or receipt["metadata"]["upstream_revision"] != REVISIONS[model_id]:
        raise ValueError("Require a passing computational receipt for this exact source")
    if receipt.get("image") != image:
        raise ValueError("Receipt image identity does not match")
    native = json.loads((CATALOG / "native/phenoage.json").read_text())
    record = native["record"]
    revision, source = REVISIONS[model_id], SOURCES[model_id]
    variant = f"{model_id}-cpu-v1"
    image_digest = image.split("@", 1)[1]
    files = receipt["weight_files"]
    expanded = sum(item["bytes"] for item in files)
    manifest = {
        "schema": "fs2-serve.nebius.ai/artifact-manifest/v1", "model_id": model_id, "kind": "weights",
        "source": {"revision": revision, "uri": f"https://github.com/{source}/tree/{revision}"},
        "content": {"digest": digest(files), "expanded_bytes": expanded, "files": files},
        "license": {"id": "MIT", "state": "verified"}, "entitlement_state": "not-required",
        "owner": "runtime-image", "retention": "retained-platform",
    }
    artifact_digest = digest(manifest)
    record["model"].update(id=model_id, display_name=NAMES[model_id], family="toxicology", tested_lane=True)
    record["model"]["source"] = {
        "kind": "git", "repository": "github.com/" + source, "revision": revision,
        "license": {"id": "MIT", "state": "verified", "notes": "Public upstream MIT repository and its distributed pretrained inference artifacts; exact inventory retained. Dataset provenance is not a clinical or regulatory validation."},
        "entitlement": {"required": False, "state": "not-required", "credential_contract": None, "notes": "No gated model entitlement is required."},
    }
    record["runtime"].update(version=str(receipt["metadata"]["model_version"]), command=["python", "-m", "uvicorn", "toxicology.server:app", "--host", "0.0.0.0", "--port", "8000"])
    record["runtime"]["image"] = {"reference": image, "digest": image_digest, "state": "resolved"}
    record["resources"].update(cpu_millis=2000, memory_bytes=2 * 1024**3)
    record["interface"]["policy"].update(operations=["screen-molecules"], commercial_use="allowed")
    record["startup"]["experiments"][0]["reason"] = "Qualified CPU inference has no CUDA process state; GPU snapshotting is not applicable. Weights are baked into the regional image."
    record["cache"].update(shared_path=f"/mnt/fs2-serve-cache/models/{model_id}", local_path=f"/var/lib/fs2-serve/cache/models/{model_id}")
    record["cache"]["artifact"] = {"state": "platform-verified", "kind": "weights", "manifest_digest": artifact_digest, "expanded_bytes": expanded, "minimum_bytes": expanded, "capacity_bound_bytes": expanded, "staged": False}
    source_path = "k8s-inference/models/toxicology/qualify_runtime.py"
    fixture_path = "k8s-inference/models/toxicology/fixtures.py"
    record["semantic_validator"].update(contract=f"{model_id}-pretrained-cpu-inference/v1", source_path=source_path, source_sha256=hashlib.sha256((HERE / "qualify_runtime.py").read_bytes()).hexdigest(), fixture_path=fixture_path, fixture_sha256=hashlib.sha256((HERE / "fixtures.py").read_bytes()).hexdigest())
    record["support"]["limitations"] = receipt["metadata"]["limitations"] + ["Computational runtime qualification only; hosted HTTP/MCP, concurrency and elasticity have independent release gates.", "CPU runtime only. No CUDA, GPU snapshot or new-node cold-start claim."]
    record["evidence"] = [{"classification": "measured-platform", "hardware": "Linux amd64 CPU container, two CPU cores", "outcome": "live-qualified", "source_commit": source_commit, "summary": "Exact-image computational parity and bounded batch qualification; receipt SHA256 " + digest(receipt) + ". This does not qualify hosted access, clinical accuracy or GPU execution."}]
    record["provenance"] = [{"url": f"https://github.com/{source}/tree/{revision}", "revision": revision, "classification": "reviewed-input"}]
    native.update(variant_id=variant)
    native["semantic_requests"]["serialization"] = "sha256-canonical-json-no-newline/v1"
    native["semantic_requests"]["invocation"]["operation"] = "screen-molecules"
    native["semantic_requests"]["requests"] = [{"id": row["id"], "payload_sha256": row["payload_sha256"]} for row in receipt["semantic_requests"]]
    native["artifact_manifest"] = {"path": f"../deployment-runtimes/artifacts/{model_id}-{artifact_digest}.json", "sha256": artifact_digest}
    selected = json.loads((CATALOG / "deployment-runtimes/phenoage-cpu.json").read_text())
    selected.update(model_id=model_id, variant_id=variant, record=copy.deepcopy(record))
    q = selected["qualification"]
    q.update(model_id=model_id, variant_id=variant)
    q["active_runtime"] = {"model_revision": revision, "runtime_image_digest": image_digest, "service": {"name": model_id, "namespace": "fs2-models", "port": 8000}}
    q["evidence"] = {key: digest(receipt) for key in q["evidence"]}
    q["policy"] = {"license_id": "MIT", "non_clinical": True, "commercial_use": "allowed"}
    q["runtime_origin"].update(variant_id=variant, repository="github.com/" + source)
    q["states"] = {key: key in {"registered", "runtime_ready", "semantic_qualified"} for key in q["states"]}
    schema = REQUEST_TYPES[model_id].model_json_schema()
    inputs = ("smiles", "molecules", "csv", "sdf")
    schema["oneOf"] = [{"required": [name], "properties": {key: ({"not": {"type": "null"}} if key == name else {"type": "null"}) for key in inputs}} for name in inputs]
    schemas_path = ROOT / "components/control-plane/src/fs2_serve/model_input_schemas/runtime-pydantic.json"
    schemas = json.loads(schemas_path.read_text())
    schemas[model_id] = {"schema": schema, "source": "k8s-inference/models/toxicology/toxicology/contracts.py"}
    examples_path = schemas_path.with_name("native-examples.json")
    examples = json.loads(examples_path.read_text())
    examples[model_id] = {"source": fixture_path, "request": semantic_requests()[0]}
    return {
        CATALOG / "native" / f"{model_id}.json": native,
        CATALOG / "deployment-runtimes" / f"{model_id}-cpu.json": selected,
        CATALOG / "deployment-runtimes/artifacts" / f"{model_id}-{artifact_digest}.json": manifest,
        schemas_path: schemas, examples_path: examples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=sorted(NAMES), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = outputs(args.model, args.image, json.loads(args.receipt.read_text()), args.source_commit)
    for path, value in generated.items():
        text = json.dumps(value, indent=2) + "\n"
        if args.check:
            assert path.read_text() == text, f"Generated contract differs: {path}"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    for name in ("fixtures.py", "qualify_runtime.py"):
        target = CATALOG / "packaged-repository/k8s-inference/models/toxicology" / name
        if args.check:
            assert target.read_bytes() == (HERE / name).read_bytes()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((HERE / name).read_bytes())


if __name__ == "__main__":
    main()

"""Add a speech template revision without withdrawing currently serving ones.

Read-only preparation. Publish these additive ConfigMaps through Helm only after
active live sessions complete. Drain task Apps via their API before applying new
runtime template references. Keep old templates valid throughout the cutover.
"""

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

from fs2_serve.model_deployment import InfrastructureEnvelope, LegacyTemplateBundle, canonical_digest, canonical_json
from prepare_registration import IDS, ROOT


def extend(envelope, bundles):
    envelope, bundles = copy.deepcopy((envelope, bundles))
    references = {}
    for identity in IDS:
        resources = list(yaml.safe_load_all((ROOT / "models/speech/k8s" / (identity + ".yaml")).read_text()))
        digest = canonical_digest(resources)
        previous = next(item for item in bundles if item["modelRef"] == identity)
        bundle = {**previous, "resources": resources, "templateDigest": digest}
        LegacyTemplateBundle.model_validate(bundle)
        if not any(b["modelRef"] == identity and b["templateDigest"] == digest for b in bundles):
            bundles.append(bundle)
        qualification = envelope["qualifications"][identity]
        if digest not in qualification["templateDigests"]:
            qualification["templateDigests"].append(digest)
        name = identity + ".cache-scratch-v2"
        qualification["templateRefs"][name] = digest
        qualification["templateCacheTiers"][digest] = "Disabled"
        references[identity] = {"name": name, "digest": digest}
    envelope.pop("revision")
    envelope["revision"] = canonical_digest(envelope)
    InfrastructureEnvelope.model_validate(envelope)
    return envelope, bundles, references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kubeconfig", "context", "envelope-configmap", "bundles-configmap"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = ["kubectl", "--kubeconfig", args.kubeconfig, "--context", args.context, "-n", "fs2-system"]

    def read(name, key):
        data = json.loads(subprocess.check_output(command + ["get", "configmap", name, "-o", "json"]))["data"]
        return json.loads(data[key])

    envelope, bundles, references = extend(read(args.envelope_configmap, "infrastructure-envelope.json"),
                                          read(args.bundles_configmap, "renderer-bundles.json"))
    objects = []

    def config(prefix, key, value):
        data = {key: canonical_json(value).decode()}
        name = prefix + hashlib.sha256(canonical_json(data)).hexdigest()[:16]
        objects.append({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
            "metadata": {"name": name, "namespace": "fs2-system",
                         "labels": {"workload.fs2.nebius/owner": "nemotron-speech-20260916"}}, "data": data})
        return name

    envelope_name = config("fs2-speech-envelope-", "infrastructure-envelope.json", envelope)
    bundle_name = config("fs2-speech-bundles-", "renderer-bundles.json", bundles)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "configmaps.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2)+"\n")
    (args.output / "values.json").write_text(json.dumps({"modelController": {
        "infrastructureEnvelopeConfigMapName": envelope_name, "rendererBundlesConfigMapName": bundle_name}}, indent=2)+"\n")
    (args.output / "template-refs.json").write_text(json.dumps(references, indent=2)+"\n")
    print(json.dumps({"new_template_refs": references, "configmaps": [envelope_name, bundle_name], "applied": False}))


if __name__ == "__main__":
    main()

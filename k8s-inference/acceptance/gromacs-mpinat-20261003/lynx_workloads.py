"""Prepare evidence-labeled Lynx replay inputs offline; never submit work.

Original prompts and structures stay in an explicitly private directory outside
Git. No customer credential is accepted. The resulting case files reuse the
existing isolated QA replay harness; they do not create a client or install tools.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re


ACTUAL_SOURCES = {
    "cif-inventory": "https://nebius.slack.com/archives/C0C3MAREU3D/p1790751815760129",
    "forcefield-recommendation": "https://nebius.slack.com/archives/C0C3MAREU3D/p1790809742846649",
    "openff-parameterization": "https://nebius.slack.com/archives/C0C3MAREU3D/p1790830997262329",
}
ASSUMED_CASES = ("structure-inventory", "force-field-advice", "openff-aspirin",
                 "openff-cation", "undefined-stereochemistry", "missing-file")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cases(path):
    rows = json.loads(path.read_text())["cases"]
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,100}", row.get("case_id", "")) or
            not isinstance(row.get("prompt"), str) or not row["prompt"].strip() for row in rows)
            or len({row["case_id"] for row in rows}) != len(rows)):
        raise ValueError("Case manifest is invalid or duplicated")
    return {row["case_id"]: {"case_id": row["case_id"], "prompt": row["prompt"]} for row in rows}


def prepare(args):
    output = args.output.resolve()
    # Original customer inputs may not become a source-controlled fixture.
    if any((parent / ".git").exists() for parent in (output, *output.parents)):
        raise ValueError("Replay evidence must stay outside every Git checkout")
    if output.exists():
        raise FileExistsError("Use a fresh private output directory; old evidence is preserved")
    qualification = args.client_root / "scripts/qualification"
    public_path = qualification / "default-release-cases.json"
    public = cases(public_path)
    assumed = [{**public[name], "case_id": "assumed-" + name,
                "evidence_class": "assumed_representative"} for name in ASSUMED_CASES]
    hosted_path = qualification / "hosted-default-cases.json"
    hosted = cases(hosted_path)["hosted-gromacs"]
    hosted = {**hosted, "case_id": "assumed-hosted-gromacs",
              "evidence_class": "assumed_representative"}
    public_bindings = []
    if args.public_cif or args.public_cif_sha256:
        if not args.public_cif or not args.public_cif_sha256:
            raise ValueError("Public inventory control requires its explicit CIF and expected hash")
        if digest(args.public_cif) != args.public_cif_sha256:
            raise ValueError("Public inventory control hash differs")
        public_bindings = [{"source": str(args.public_cif.resolve()),
                            "target": "/workspace/inputs/1UBQ.cif",
                            "sha256": args.public_cif_sha256, "read_only": True,
                            "reference_url": "https://files.rcsb.org/download/1UBQ.cif",
                            "evidence_class": "assumed_representative"}]
    actual, bindings = [], []
    private = (args.private_cases, args.private_cases_sha256, args.private_cif, args.private_cif_sha256)
    if any(private):
        if not all(private):
            raise ValueError("Actual replays require both explicit files and their expected hashes")
        if digest(args.private_cases) != args.private_cases_sha256 or digest(args.private_cif) != args.private_cif_sha256:
            raise ValueError("Original customer input hash differs; do not silently substitute a fixture")
        original = cases(args.private_cases)
        if set(original) != set(ACTUAL_SOURCES):
            raise ValueError("Expected the three actual Lynx requests, without assumed hosted cases")
        actual = [{**original[name], "evidence_class": "customer_reported",
                   "source": source} for name, source in ACTUAL_SOURCES.items()]
        targets = re.findall(r"/workspace/[^\s\"'`]+?\.cif", original["cif-inventory"]["prompt"])
        if len(targets) != 1 or ".." in Path(targets[0]).parts:
            raise ValueError("Original CIF prompt must bind one contained workspace input")
        bindings = [{"source": str(args.private_cif.resolve()), "target": targets[0],
                     "sha256": args.private_cif_sha256, "read_only": True,
                     "storage_policy": "private local QA mount; not Git or a customer/system bucket"}]
    plan = {"schema": "fs2.lynx-workload-plan/v1", "prepared_only": True,
            "tenant": "system", "principal": "qa", "customer_credentials_used": False,
            "source_manifests": [{"path": str(path), "sha256": digest(path)}
                                 for path in (public_path, hosted_path)],
            "private_input_bindings": bindings,
            "public_input_bindings": public_bindings,
            "public_inventory_ready": bool(public_bindings),
            "actual_cases": [{"case_id": row["case_id"], "source": row["source"],
                              "evidence_class": row["evidence_class"],
                              "prompt_sha256": hashlib.sha256(row["prompt"].encode()).hexdigest()}
                             for row in actual],
            "assumed_cpu_cases": [row["case_id"] for row in assumed],
            "assumed_hosted_cases": [hosted["case_id"]],
            "actual_prepared_gromacs_pipeline": "blocked: no complete customer native protocol/input bundle retained",
            "gpu_scheduling": "CPU-only replay first; separately coordinate the one-GPU hosted example with the existing QA policy owner",
            "release_qualified": False}
    if actual:
        plan["source_manifests"].append({"path": str(args.private_cases.resolve()),
                                         "sha256": args.private_cases_sha256, "private": True})
    output.mkdir(mode=0o700, parents=True)
    documents = {"actual-agent-cases.json": {"cases": actual},
                 "assumed-cpu-agent-cases.json": {"cases": assumed},
                 "assumed-hosted-agent-cases.json": {"cases": [hosted]},
                 "dispatch-plan.json": plan}
    for name, document in documents.items():
        with os.fdopen(os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, required=True, help="templates/hcls-librechat directory")
    parser.add_argument("--private-cases", type=Path)
    parser.add_argument("--private-cases-sha256")
    parser.add_argument("--private-cif", type=Path)
    parser.add_argument("--private-cif-sha256")
    parser.add_argument("--public-cif", type=Path, help="Retained public 1UBQ CIF for the representative inventory control")
    parser.add_argument("--public-cif-sha256")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    plan = prepare(args)
    print(json.dumps({"prepared_only": True, "actual_cases": len(plan["actual_cases"]),
                      "assumed_cpu_cases": len(plan["assumed_cpu_cases"]),
                      "assumed_hosted_cases": len(plan["assumed_hosted_cases"]),
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()

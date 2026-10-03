"""Build a pinned v3→v4 analysis-only delta and replay retained NVT energy on CPU.

This never seeds a bucket, publishes an image, edits v3, or runs MD. The output
is a draft delta, not a complete qualified v4 starter pack.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import statistics
import subprocess
import tarfile

BASE_MANIFEST_SHA256 = "c8afd07ca1b6734c690839ba6d3eb4b461b65380852bcf705a863e7bd07a7709"
GENERATOR_COMMIT = "92b02648ab3390981561615da4194eb2e37be820"
IMAGE = "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-platform/gromacs@sha256:dc5d908c64503c4c4cdc3bede10987d49a9acdde5ef2f4f1d0e61f0628739f93"
OPERATION = "bc1bb480-4536-4147-aae1-bbb59504d841"
CASES = {"alanine-quickstart": 1, "alanine-1ns": 1, "alanine-replicas": 2}
TERMS = ("Potential", "Total-Energy", "Temperature", "Pressure")
OLD_STDIN = "\n".join((*TERMS, "Density", "0", ""))
NEW_STDIN = "\n".join((*TERMS, "0", ""))
DENSITY = {
    "value": None,
    "unit": "kg/m^3",
    "status": "unavailable_from_native_nvt_energy",
    "reason": "This fixed-volume NVT EDR does not contain a Density observable.",
    "derived_from_mass_and_volume": False,
    "zero_imputed": False,
}


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data if isinstance(data, bytes) else encoded(data))


def safe_relative(name):
    path = PurePosixPath(name)
    if (not name or str(path) != name or path.is_absolute() or ".." in path.parts
            or "\\" in name or any(ord(c) < 32 for c in name)):
        raise ValueError("unsafe_pack_path")
    return path


def read_pack(root, expected_sha=BASE_MANIFEST_SHA256):
    root = root.resolve(strict=True)
    raw = (root / "manifest.json").read_bytes()
    if sha(raw) != expected_sha:
        raise ValueError("unreviewed_v3_manifest")
    manifest = json.loads(raw)
    if (manifest.get("schema") != "fs2-serve.nebius.ai/customer-starter-pack/v1"
            or manifest.get("version") != "v3"
            or manifest.get("release_status") != "qualified"):
        raise ValueError("expected_qualified_v3")
    objects = {}
    for item in manifest["objects"]:
        name = str(safe_relative(item["path"]))
        path = root / name
        if name in objects or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("duplicate_or_external_pack_object")
        data = path.read_bytes()
        if len(data) != item["size_bytes"] or sha(data) != item["sha256"]:
            raise ValueError("source_object_changed:" + name)
        objects[name] = data
    return manifest, objects


def corrected_parameters(original, expected_jobs):
    value = copy.deepcopy(original)
    changes = []
    if len(value["jobs"]) != expected_jobs:
        raise ValueError("unexpected_job_count")
    for j, job in enumerate(value["jobs"]):
        matches = [(i, step) for i, step in enumerate(job["steps"])
                   if step["id"] == "energies-nvt"]
        if len(matches) != 1:
            raise ValueError("nvt_analysis_not_unique")
        i, step = matches[0]
        if step["command"] != "energy" or step.get("stdin") != OLD_STDIN:
            raise ValueError("unexpected_nvt_selection")
        step["stdin"] = NEW_STDIN
        changes.append({"job_id": job["id"], "json_pointer": f"/jobs/{j}/steps/{i}/stdin",
                        "before": OLD_STDIN, "after": NEW_STDIN})
    return value, changes


def archive_inventory(raw):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for item in archive:
            name = str(safe_relative(item.name))
            if not item.isfile() or name in files:
                raise ValueError("invalid_native_archive_member")
            data = archive.extractfile(item).read()
            files[name] = {"sha256": sha(data), "size_bytes": len(data)}
            if name.endswith("nvt.mdp"):
                values = re.findall(r"^\s*pcoupl\s*=\s*([^;\s]+)", data.decode(), re.M)
                if values != ["no"]:
                    raise ValueError("nvt_is_not_explicitly_fixed_volume")
    return files


def build_delta(base, output, *, expected_sha=BASE_MANIFEST_SHA256):
    manifest, objects = read_pack(base, expected_sha)
    if output.exists() or output.resolve().is_relative_to(base.resolve()):
        raise ValueError("output_must_be_fresh_and_outside_v3")
    replacements, changes = {}, []
    for case, jobs in CASES.items():
        prefix = "molecular-dynamics/" + case
        ppath, rpath = prefix + "/gromacs/parameters.json", prefix + "/recipes.json"
        original = json.loads(objects[ppath])
        parameters, edits = corrected_parameters(original, jobs)
        recipes = json.loads(objects[rpath])
        selected = [r for r in recipes["recipes"] if r["model_id"] == "gromacs"]
        if len(selected) != 1 or selected[0]["arguments"]["parameters"] != original:
            raise ValueError("recipe_parameter_binding_mismatch")
        selected[0]["arguments"]["parameters"] = parameters
        replacements[ppath], replacements[rpath] = encoded(parameters), encoded(recipes)
        changes.append({"case_id": prefix, "model_id": "gromacs", "edits": edits,
                        "required_nvt_observables": list(TERMS), "density": DENSITY})
    native = []
    for name, data in objects.items():
        if name.startswith("molecular-dynamics/") and name.endswith("/input.tar.gz"):
            native.append({"path": name, "sha256": sha(data), "size_bytes": len(data),
                           "files": archive_inventory(data), "changed": False})
    # Audit the unchanged archive inventory without extracting or rewriting it.
    if len(native) != 17 or len(replacements) != 6:
        raise ValueError("unexpected_v3_md_coverage")
    output.mkdir(parents=True)
    rows = []
    for name, data in sorted(replacements.items()):
        save_new(output / "replacements" / name, data)
        rows.append({"path": name, "base_sha256": sha(objects[name]),
                     "sha256": sha(data), "size_bytes": len(data)})
    delta = {
        "schema": "fs2.starter-analysis-delta/v1", "version": "v4",
        "release_status": "draft", "customer_ready": False,
        "base_version": "v3", "base_manifest_sha256": expected_sha,
        "authoritative_generator_commit": GENERATOR_COMMIT,
        "base_object_count": len(manifest["objects"]),
        "replacements": rows, "changes": changes,
        "preserved_md_input_archives": native,
        "scientific_parameters_changed": False, "new_simulations": 0,
        "density": DENSITY,
        "scope": "Analysis-only draft; all non-NVT commands and all physical inputs unchanged.",
        "qualification": {"state": "pending_hosted_acceptance", "inherited_v3_is_historical": True},
        "integration": "Apply replacements only to a new full v4 pack after verifying every base hash; reset affected qualification, version documentation/identities, then use the existing qualification and seeding flow. Never edit examples/v3 or auto-promote this delta.",
    }
    save_new(output / "delta.json", delta)
    # A late source change cannot silently receive preservation evidence.
    read_pack(base, expected_sha)
    return delta


def term_key(value):
    return re.sub(r"[\s-]+", "", value).casefold()


def validate_energy(xvg, log, requested, exit_code, *, frames=None, final_ps=None):
    """Require exact named series, finite rectangular rows, cadence and clean selection."""
    errors, legends, rows = [], {}, []
    keys = [term_key(name) for name in requested]
    if not keys or len(keys) != len(set(keys)):
        errors.append("empty_or_ambiguous_requested_terms")
    if exit_code != 0:
        errors.append("native_nonzero_exit")
    diagnostics = [line.strip() for line in log.splitlines() if re.search(
        r"does not match anything|ambiguous|matches (?:more than|multiple)|no energy terms|fatal error",
        line, re.I)]
    if diagnostics:
        errors.append("native_selection_diagnostic")
    for line in xvg.splitlines():
        match = re.fullmatch(r'\s*@\s+s(\d+)\s+legend\s+"([^"\n]+)"\s*', line)
        if match:
            index = int(match[1])
            if index in legends:
                errors.append("duplicate_legend_index")
            legends[index] = match[2]
        elif line.strip() and not line.lstrip().startswith(("#", "@")):
            try:
                row = [float(token) for token in line.split()]
                if len(row) != len(requested) + 1:
                    errors.append("observable_column_count_mismatch")
                if not all(math.isfinite(x) for x in row):
                    errors.append("nonfinite_energy_value")
                rows.append(row)
            except ValueError:
                errors.append("nonnumeric_energy_row")
    names = [legends[i] for i in sorted(legends)]
    if sorted(legends) != list(range(len(requested))):
        errors.append("missing_or_extra_legend")
    if [term_key(name) for name in names] != keys:
        errors.append("observable_identity_or_order_mismatch")
    if len({term_key(name) for name in names}) != len(names):
        errors.append("ambiguous_duplicate_observable")
    if not rows:
        errors.append("empty_energy_series")
    else:
        times = [row[0] for row in rows if row]
        if len(times) != len(rows) or any(a >= b for a, b in zip(times, times[1:])):
            errors.append("nonincreasing_energy_time")
        if frames is not None and len(rows) != frames:
            errors.append("energy_frame_count_mismatch")
        if final_ps is not None and (not times or times[0] != 0 or times[-1] != final_ps):
            errors.append("energy_time_extent_mismatch")
        if frames and frames > 1 and final_ps is not None and len(times) == frames:
            if any(abs(t - i * final_ps / (frames - 1)) > 1e-8 for i, t in enumerate(times)):
                errors.append("energy_cadence_mismatch")
    result = {"status": "failed" if errors else "passed", "errors": sorted(set(errors)),
              "native_exit_code": exit_code, "selection_diagnostics": diagnostics,
              "requested_observables": list(requested), "observed_legends": names,
              "frames": len(rows), "density": DENSITY}
    result["numeric_rows_sha256"] = (sha(encoded(rows)) if rows and all(
        math.isfinite(x) for row in rows for x in row) else None)
    if not errors:
        result.update(first_time_ps=rows[0][0], last_time_ps=rows[-1][0],
                      series=[{"name": name, "sample_mean": statistics.mean(row[i + 1] for row in rows)}
                              for i, name in enumerate(names)])
    return result


def resolve_local_image(local_image, output):
    """Join the digest-pinned OCI manifest to the local immutable image config."""
    raw = subprocess.check_output(["skopeo", "inspect", "--raw", "docker://" + IMAGE])
    if sha(raw) != IMAGE.split("sha256:")[1]:
        raise ValueError("registry_manifest_digest_mismatch")
    save_new(output / "image-manifest.json", raw)
    manifest = json.loads(raw)
    if "manifests" in manifest:
        selected = [m for m in manifest["manifests"] if m.get("platform", {}).get("os") == "linux"
                    and m["platform"].get("architecture") == "amd64"]
        if len(selected) != 1:
            raise ValueError("amd64_manifest_not_unique")
        child = IMAGE.split("@")[0] + "@" + selected[0]["digest"]
        raw = subprocess.check_output(["skopeo", "inspect", "--raw", "docker://" + child])
        if "sha256:" + sha(raw) != selected[0]["digest"]:
            raise ValueError("platform_manifest_digest_mismatch")
        save_new(output / "image-platform-manifest.json", raw)
        manifest = json.loads(raw)
    identity = subprocess.check_output(["docker", "image", "inspect", local_image,
                                        "--format", "{{.Id}}"], text=True).strip()
    if identity != manifest["config"]["digest"]:
        raise ValueError("local_image_not_exact_registry_config")
    return identity


def energy_command(native, output, image_id, label):
    return ["docker", "run", "--rm", "-i", "--network", "none", "--cpus", "1", "--memory", "512m",
            "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=16m",
            "--entrypoint", "/usr/local/gromacs/avx2_256/bin/gmx",
            "-e", "OMP_NUM_THREADS=1", "-e", "GMX_MAXBACKUP=-1", "-e", "NVIDIA_VISIBLE_DEVICES=void",
            "--mount", f"type=bind,src={native.resolve()},dst=/input,readonly",
            "--mount", f"type=bind,src={output.resolve()},dst=/output", image_id,
            "energy", "-f", "/input/nvt.part0001.edr", "-o", f"/output/{label}.xvg"]


def replay(native, source_receipt, local_image, output):
    if output.resolve().is_relative_to(native.resolve()):
        raise ValueError("replay_output_must_be_outside_native_source")
    verification = json.loads(source_receipt.read_bytes())
    if verification["operation_id"] != OPERATION:
        raise ValueError("unexpected_source_operation")
    inventory = {r["native_path"]: r for r in verification["hash_verified_authenticated_downloads"]}
    names = ["nvt.part0001.edr", "nvt-energy.xvg", "fs2-energies-nvt-segment-000001.log"]
    before = {}
    for name in names:
        data = (native / name).read_bytes()
        item = inventory[name]
        if sha(data) != item["sha256"] or len(data) != item["size_bytes"]:
            raise ValueError("source_native_artifact_changed")
        before[name] = sha(data)
    output.mkdir(parents=True, exist_ok=False)
    image_id = resolve_local_image(local_image, output)
    runs = []
    # Keep the known defect as an explicit negative, not a silently accepted run.
    for label, selection, terms in (("v3-negative", OLD_STDIN, (*TERMS, "Density")),
                                    ("v4-corrected", NEW_STDIN, TERMS)):
        command = energy_command(native, output, image_id, label)
        proc = subprocess.run(command, input=selection.encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60)
        save_new(output / (label + ".log"), proc.stdout)
        xvg = output / (label + ".xvg")
        observed = validate_energy(xvg.read_text() if xvg.exists() else "", proc.stdout.decode(),
                                   terms, proc.returncode, frames=21, final_ps=20)
        observed.update(case=label, command=command, stdin=selection,
                        log_sha256=sha(proc.stdout), xvg_sha256=sha(xvg.read_bytes()) if xvg.exists() else None)
        runs.append(observed)
    after = {name: sha((native / name).read_bytes()) for name in names}
    legacy = validate_energy((native / names[1]).read_text(), (native / names[2]).read_text(),
                             (*TERMS, "Density"), 0, frames=21, final_ps=20)
    numerical_parity = (legacy["numeric_rows_sha256"] == runs[0]["numeric_rows_sha256"]
                        == runs[1]["numeric_rows_sha256"] and legacy["numeric_rows_sha256"] is not None)
    successful = (runs[0]["native_exit_code"] == 0 and runs[0]["status"] == "failed"
                  and runs[1]["status"] == "passed" and legacy["status"] == "failed"
                  and before == after and numerical_parity)
    receipt = {"schema": "fs2.starter-nvt-analysis-replay/v1", "status": "passed" if successful else "failed",
               "source_operation_id": OPERATION, "source_verification_sha256": sha(source_receipt.read_bytes()),
               "runtime_image": IMAGE, "local_image_config": image_id, "source_artifacts": before,
               "source_artifacts_unchanged": before == after, "retained_v3_validation": legacy, "runs": runs,
               "all_four_numeric_series_equal_retained_values": numerical_parity,
               "density": DENSITY, "new_simulations": 0, "gpu_used": False, "customer_ready": False,
               "scope": "CPU analysis of one retained 20 ps NVT EDR; no new hosted workflow or equilibrium claim."}
    save_new(output / "receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build")
    build.add_argument("--base", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    native = sub.add_parser("replay")
    native.add_argument("--native", type=Path, required=True)
    native.add_argument("--source-receipt", type=Path, required=True)
    native.add_argument("--local-image", required=True)
    native.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "build":
        result = build_delta(args.base, args.output)
        print(json.dumps({"version": result["version"], "status": result["release_status"],
                          "replacements": len(result["replacements"]), "customer_ready": False}))
    else:
        result = replay(args.native, args.source_receipt, args.local_image, args.output)
        print(json.dumps({"status": result["status"], "runs": [{"case": r["case"], "status": r["status"],
                          "exit_code": r["native_exit_code"]} for r in result["runs"]]}))
        raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()

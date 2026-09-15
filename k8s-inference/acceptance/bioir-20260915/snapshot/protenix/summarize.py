#!/usr/bin/env python3
"""Regenerate lifecycle clocks and semantic output checks from retained evidence."""
from datetime import datetime
import hashlib
import itertools
import json
import math
from pathlib import Path
import shlex
import statistics
import tarfile
from control import ROOT, BASE, PREFIX, LANE, IMAGE, TOOLS, NODE, PVC


def read(path):
    return json.loads(path.read_text())


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def cif_ca(path):
    columns = []
    chains = {}
    for line in path.read_text().splitlines():
        if line.startswith("_atom_site."):
            columns.append(line.split()[0].removeprefix("_atom_site."))
        elif line.startswith("ATOM "):
            row = dict(zip(columns, shlex.split(line)))
            if row.get("label_atom_id") == "CA":
                chain = row.get("auth_asym_id", row["label_asym_id"])
                chains.setdefault(chain, []).append(tuple(float(row["Cartn_" + a]) for a in "xyz"))
    assert chains
    return chains


def lddt(prediction, reference):
    assert len(prediction) == len(reference)
    scores = []
    for i in range(len(reference)):
        for j in range(i):
            distance = math.dist(reference[i], reference[j])
            if 0 < distance < 15:
                error = abs(math.dist(prediction[i], prediction[j]) - distance)
                scores.append(sum(error < bound for bound in (0.5, 1, 2, 4)) / 4)
    return statistics.mean(scores)


def main():
    structures = read(BASE.parents[1] / "coverage/fixtures/structures.json")["structures"]
    references = {}
    for code in ["1UBQ", "1LYZ"]:
        coordinates = []
        seen = set()
        for line in structures[code]["pdb"].splitlines():
            if line.startswith("ATOM  ") and line[12:16].strip() == "CA" and line[21] == "A" and line[16] in " A":
                key = line[22:27]
                if key not in seen:
                    seen.add(key)
                    coordinates.append(tuple(float(line[a:b]) for a, b in [(30, 38), (38, 46), (46, 54)]))
        references[code] = coordinates
    attempts = []
    lifecycle = []
    source_cifs = {}
    for folder in sorted((ROOT / "raw").glob("bir-protenix-*")):
        # Collection writes the tar stream before release. Never summarize a
        # live partial archive; closed pods must still pass every hash check.
        if not (ROOT / "lifecycle" / (folder.name + "-released.json")).is_file():
            continue
        archive = folder / "requests.tgz"
        extracted = folder / "artifacts"
        if archive.is_file() and archive.stat().st_size:
            extracted.mkdir(exist_ok=True)
            with tarfile.open(archive) as stream:
                stream.extractall(extracted, filter="data")
        for rows_file in sorted(folder.glob("*.jsonl")):
            for line in rows_file.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                row.update(pod=folder.name, label=rows_file.stem)
                row["outputs"] = []
                for artifact in row["artifacts"]:
                    if artifact["path"].endswith(".cif"):
                        path = extracted / "results" / rows_file.stem / artifact["path"]
                        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
                        chains = cif_ca(path)
                        reference = references["1UBQ" if row["case"].startswith("ubiquitin") else "1LYZ"]
                        scores = [lddt(ca, reference) for ca in chains.values()]
                        row["outputs"].append({"path": str(path.relative_to(ROOT)), "sha256": artifact["sha256"], "chains": list(chains), "residues": [len(ca) for ca in chains.values()], "reference_ca_lddt": scores})
                        source_cifs[(folder.name, row["label"], row["case"])] = (artifact["sha256"], chains)
                attempts.append(row)
        ready_path = ROOT / "lifecycle" / (folder.name + "-ready.json")
        if not ready_path.is_file():
            continue
        ready = read(ready_path)
        value = ready["pod"]
        started = timestamp(value["status"]["containerStatuses"][0]["state"]["running"]["startedAt"])
        created = timestamp(value["metadata"]["creationTimestamp"])
        initial = value["status"]["initContainerStatuses"][0]["state"]["terminated"]
        requests = [row for row in attempts if row["pod"] == folder.name]
        first = min(requests, key=lambda row: row["created_at"]) if requests else None
        events_file = ROOT / "lifecycle" / (folder.name + "-events.json")
        events = read(events_file)["items"] if events_file.exists() else []
        record = {"pod": folder.name, "variant": "restore" if "-restore-" in folder.name else "normal" if "-normal-" in folder.name else "fallback" if folder.name.endswith("fallback") else "donor", "pod_uid": value["metadata"]["uid"], "created_unix": created, "container_started_unix": started, "ready_unix": ready["unix"], "create_to_ready_seconds": ready["unix"] - created, "container_to_ready_seconds": ready["unix"] - started, "init_seconds": timestamp(initial["finishedAt"]) - timestamp(initial["startedAt"]), "image_pull_events": [e["message"] for e in events if e["reason"] in ["Pulling", "Pulled"]], "ready_metadata": ready["health"], "first_request_seconds": first["wall_seconds"] if first else None, "create_to_first_validated_seconds": timestamp(first["created_at"]) + first["wall_seconds"] - created if first else None, "container_to_first_validated_seconds": timestamp(first["created_at"]) + first["wall_seconds"] - started if first else None}
        record["first_request_case"] = first["case"] if first else None
        released = ROOT / "lifecycle" / (folder.name + "-released.json")
        if released.exists():
            released = read(released)
            lower = released.get("delete_started_unix", max([timestamp(row["created_at"]) + row["wall_seconds"] for row in requests] or [ready["unix"]]))
            record["gpu_slot_seconds_bounds"] = [lower - timestamp(value["status"]["startTime"]), released["unix"] - timestamp(value["status"]["startTime"])]
        lifecycle.append(record)
    pairs = []
    for case in ["ubiquitin-76", "lysozyme-129"]:
        baseline = source_cifs.get((PREFIX + "-normal-1", "measured", case))
        if not baseline:
            continue
        for (pod, label, candidate_case), (digest, chains) in source_cifs.items():
            if candidate_case != case:
                continue
            pairs.append({"pod": pod, "label": label, "case": case, "byte_identical": digest == baseline[0], "ca_lddt_to_comparison": [lddt(ca, ref) for ca, ref in zip(chains.values(), baseline[1].values())], "comparison": "normal control1,76→129→76 request history", "matched_request_history": label == "measured" and ("-restore-" in pod or "-normal-" in pod)})
    summaries = []
    for variant in ["donor", "restore", "normal", "fallback"]:
        cohort = [row for row in lifecycle if row["variant"] == variant]
        if cohort:
            summaries.append({"variant": variant, "n": len(cohort), **{field: {"samples": [row[field] for row in cohort], "median": statistics.median(row[field] for row in cohort)} for field in ["create_to_ready_seconds", "container_to_ready_seconds", "create_to_first_validated_seconds", "container_to_first_validated_seconds", "first_request_seconds"] if all(row[field] is not None for row in cohort)}})
    dispersion = []
    for variant in ["restore", "normal"]:
        for case in ["ubiquitin-76", "lysozyme-129"]:
            values = [(key, value) for key, value in source_cifs.items() if "-" + variant + "-" in key[0] and key[1] == "measured" and key[2] == case]
            for (a_key, a), (b_key, b) in itertools.combinations(values, 2):
                dispersion.append({"variant": variant, "case": case, "pods": [a_key[0], b_key[0]], "byte_identical": a[0] == b[0], "ca_lddt": [lddt(ca, ref) for ca, ref in zip(a[1].values(), b[1].values())]})
    capture = json.JSONDecoder().raw_decode((ROOT / "raw" / (PREFIX + "-donor") / "capture-stdout.json").read_text())[0]
    result = {"node": NODE, "image": IMAGE, "tools": TOOLS, "checkpoint_capture": capture, "lifecycle": lifecycle, "summaries": summaries, "attempts": attempts, "paired_outputs": pairs, "source_sha256": read(ROOT / "inventory/source-hashes.json"), "limitations": ["Technical restore compatibility only: model-lane Protenix scientific-quality regression remains disqualifying.", "Same GPU UUID, image, driver and kernel only; no device remapping or driver-upgrade qualification.", "Tiny fixed-seed public cohort does not establish biological non-inferiority or tail latency.", "Preparation handoffs reused exactly from validated current CPU lane; candidate image lacks zstd for standalone CPU handoff packaging.", "Readiness polling and kubectl overhead are included; health model_load_seconds is inherited captured metadata, not restore time.", "Initial donor includes first image pull and troubleshooting; excluded from matched restore/normal ratio."]}
    result["within_cohort_numerical_dispersion"] = dispersion
    (ROOT / "analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"summaries": summaries, "valid": len(attempts), "byte_identical_pairs": sum(p["byte_identical"] for p in pairs), "pairs": len(pairs)}, indent=2))


if __name__ == "__main__":
    main()

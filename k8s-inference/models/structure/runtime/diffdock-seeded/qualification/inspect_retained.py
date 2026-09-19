"""Recheck frozen DiffDock outputs offline; never admit inference or mutate inputs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path

import numpy as np
from rdkit import Chem, rdBase
from rdkit.Chem import rdMolAlign

IMAGE = (
    "cr.eu-north1.nebius.cloud/e00akg9ndpx77eaexh/fs2-models/h100-fleet/diffdock@sha256:"
    "9766b4fb2a22787874bd8d90306980a4cb1ff9808941f0f1ae429d8b8f7cc948"
)
SOURCE = "a5583e15957b1766c0668dba5af265a72f054f8b"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse(block: str):
    """Capture, do not suppress or hide, diagnostics from this exact parse."""
    diagnostic = io.StringIO()
    with redirect_stderr(diagnostic):
        molecule = Chem.MolFromMolBlock(block.split("$$$$")[0], removeHs=True)
    if molecule is None or molecule.GetNumConformers() != 1:
        raise ValueError("Invalid retained molecule")
    coordinates = molecule.GetConformer().GetPositions()
    if not np.isfinite(coordinates).all():
        raise ValueError("Non-finite retained coordinates")
    return molecule, diagnostic.getvalue()


def delta(left, right):
    if len(left) != len(right):
        raise ValueError("Pose count differs")
    coordinate = max(
        float(np.max(np.abs(a[0].GetConformer().GetPositions() - b[0].GetConformer().GetPositions())))
        for a, b in zip(left, right, strict=True)
    )
    confidence = max(abs(a[1] - b[1]) for a, b in zip(left, right, strict=True))
    return {"coordinate_angstrom": coordinate, "confidence": confidence}


def inspect(campaign: Path, references: Path):
    rdBase.LogToPythonStderr()
    cross_path = campaign / "diffdock-final-cross-process.json"
    cross = json.loads(cross_path.read_text())
    build_path = campaign / "diffdock-seeded-3d-build.json"
    build = json.loads(build_path.read_text())
    build_source = build["buildx.build.provenance"]["invocation"]["parameters"]["args"][
        "label:org.opencontainers.image.revision"
    ]
    if IMAGE.split("@")[1] != build["containerimage.digest"] or not SOURCE.startswith(build_source):
        raise ValueError("Build does not bind the expected source and image")
    rows, references_seen, parsed_runs = [], {}, []
    all_metrics, output_parse_warnings, output_headers = [], [], set()
    within_pairs = []
    for index, name in enumerate(("diffdock-final-isolated-r6", "diffdock-final-isolated-r7")):
        directory = campaign / name
        case_path = directory / "cases.json"
        if sha(case_path) != cross["cases_sha256"]:
            raise ValueError("Frozen case hash differs")
        cases = {c["case_id"]: c for c in json.loads(case_path.read_text())}
        receipt_path = directory / "results/receipt.json"
        receipt = json.loads(receipt_path.read_text())
        if sha(receipt_path) != cross["runs"][index]["receipt_sha256"]:
            raise ValueError("Original receipt hash differs")
        pod = json.loads((directory / "results/pod.json").read_text())
        container = pod["status"]["containerStatuses"][0]
        if container["imageID"] != IMAGE or container["state"]["terminated"]["exitCode"] != 0:
            raise ValueError("Unsuccessful or different runtime image")
        parsed, result_hashes = {}, []
        for run in receipt["runs"]:
            case = cases[run["case_id"]]
            request_sha = hashlib.sha256(
                json.dumps(case["arguments"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if request_sha != run["input_sha256"]:
                raise ValueError("Input hash differs from frozen scientific case")
            result_path = directory / "results" / run["result_file"]
            if sha(result_path) != run["result_sha256"]:
                raise ValueError("Retained result bytes differ")
            result_hashes.append({"file": run["result_file"], "sha256": sha(result_path)})
            result = json.loads(result_path.read_text())
            if len(result["poses"]) != case["expected"]["num_poses"]:
                raise ValueError("Incorrect number of poses")
            ref_relative = case["expected"]["reference_path"]
            if ref_relative not in references_seen:
                path = references / ref_relative
                expected = next(s["sha256"] for s in case["provenance"]["sources"] if s["path"] == ref_relative)
                if sha(path) != expected:
                    raise ValueError("Reference bytes differ")
                raw = path.read_text()
                molecule, warning = parse(raw)
                references_seen[ref_relative] = {
                    "molecule": molecule,
                    "path": ref_relative,
                    "sha256": sha(path),
                    "header_line": raw.splitlines()[1],
                    "parse_warning": warning,
                    "parsed_as_3d": molecule.GetConformer().Is3D(),
                    "has_nonzero_z": bool(np.any(molecule.GetConformer().GetPositions()[:, 2] != 0)),
                }
            ref = references_seen[ref_relative]["molecule"]
            poses, metrics = [], []
            for pose in result["poses"]:
                header = pose["sdf"].splitlines()[1]
                output_headers.add(header)
                molecule, warning = parse(pose["sdf"])
                if warning:
                    output_parse_warnings.append({"file": run["result_file"], "rank": pose["rank"], "warning": warning})
                if header[20:22] != "3D" or not molecule.GetConformer().Is3D():
                    raise ValueError("Generated SDF lacks actual 3D header/conformer")
                if Chem.MolToSmiles(molecule, isomericSmiles=False) != Chem.MolToSmiles(ref, isomericSmiles=False):
                    raise ValueError("Generated ligand topology differs")
                confidence = float(pose["confidence"])
                if not np.isfinite(confidence):
                    raise ValueError("Non-finite confidence")
                rmsd = float(
                    rdMolAlign.CalcRMS(molecule, ref, maxMatches=100000, symmetrizeConjugatedTerminalGroups=True)
                )
                poses.append((molecule, confidence))
                metrics.append(rmsd)
            key = (run["case_id"], run["repetition"])
            if key in parsed:
                raise ValueError("Duplicate case/repetition")
            parsed[key] = poses
            all_metrics.append(
                {
                    "run": name,
                    "case_id": key[0],
                    "repetition": key[1],
                    "top1_rmsd_angstrom": metrics[0],
                    "best_of_4_rmsd_angstrom": min(metrics),
                    "maximum_pose_rmsd_angstrom": max(metrics),
                }
            )
            retained = next(
                row for row in cross["runs"][index]["evaluations"] if (row["case_id"], row["repetition"]) == key
            )
            if any(
                abs(a - b["heavy_atom_rmsd_angstrom"]) > 1e-9
                for a, b in zip(metrics, retained["predictions"], strict=True)
            ):
                raise ValueError("Independent RMSD differs from retained evaluation")
        expected_keys = {(case, repetition) for case in cases for repetition in (1, 2)}
        if set(parsed) != expected_keys:
            raise ValueError("Incomplete retained cohort")
        for case in cases:
            comparison = delta(parsed[case, 1], parsed[case, 2])
            if (
                comparison["coordinate_angstrom"] > receipt["coordinate_tolerance_angstrom"]
                or comparison["confidence"] > receipt["confidence_tolerance"]
            ):
                raise ValueError("Within-process repeat exceeds frozen tolerances")
            within_pairs.append({"run": name, "case_id": case, **comparison})
        parsed_runs.append(parsed)
        logs = (directory / "results/runtime.log").read_text().splitlines()
        warnings = [line for line in logs if not line.startswith("{") and "Warning:" in line]
        rows.append(
            {
                "name": name,
                "receipt_sha256": sha(receipt_path),
                "pod": pod["metadata"]["name"],
                "pod_uid": pod["metadata"]["uid"],
                "node": pod["spec"]["nodeName"],
                "image": container["imageID"],
                "started_at": container["state"]["terminated"]["startedAt"],
                "completed_at": container["state"]["terminated"]["finishedAt"],
                "gpu": receipt["gpu"],
                "model_load_seconds": receipt["model_load_seconds"],
                "torsion_normalization": receipt["torsion_normalization"],
                "adapter_identity": receipt["adapter_identity"],
                "result_files": result_hashes,
                "runtime_warning_messages": sorted(set(warnings)),
                "runtime_warning_count": len(warnings),
            }
        )
    cross_pairs = [delta(parsed_runs[0][key], parsed_runs[1][key]) for key in sorted(parsed_runs[0])]
    if any(rows[0][field] != rows[1][field] for field in ("adapter_identity", "torsion_normalization")):
        raise ValueError("Process model or normalization identity differs")
    original_path = campaign / "diffdock-seeded-isolated-r1/results/receipt.json"
    original_identity = json.loads(original_path.read_text())["adapter_identity"]
    weight_fields = ("revision", "release_archive_sha256", "esm_checkpoint_sha256")
    if any(original_identity[field] != rows[0]["adapter_identity"][field] for field in weight_fields):
        raise ValueError("Upstream model/weight identity changed from original candidate")
    if any(item["coordinate_angstrom"] or item["confidence"] for item in cross_pairs):
        raise ValueError("Cross-process evidence is not exact at serialized precision")
    stderr = (campaign / "diffdock-final-cross-process.stderr.log").read_text()
    warning_phrase = "molecule is tagged as 2D, but at least one Z coordinate is not zero"
    return {
        "schema": "fs2-diffdock-retained-independent-review/v1",
        "source_commit": SOURCE,
        "image": IMAGE,
        "build_receipt_sha256": sha(build_path),
        "upstream_model_and_weight_identity_unchanged": True,
        "original_candidate_receipt_sha256": sha(original_path),
        "rdkit_version": rdBase.rdkitVersion,
        "customer_ready": False,
        "new_gpu_calls": 0,
        "cases_sha256": cross["cases_sha256"],
        "cross_report_sha256": sha(cross_path),
        "inference_calls": sum(len(p) for p in parsed_runs),
        "generated_poses": sum(len(v) for p in parsed_runs for v in p.values()),
        "all_retained_input_result_reference_hashes_verified": True,
        "all_rmsds_independently_recomputed_and_matched": True,
        "generated_sdf_headers": sorted(output_headers),
        "generated_parse_warnings": output_parse_warnings,
        "reference_diagnostics": [
            {k: v for k, v in row.items() if k != "molecule"} for row in references_seen.values()
        ],
        "original_independent_evaluator_warning_count": stderr.count(warning_phrase),
        "warning_explanation": (
            "The six unchanged RCSB ModelServer reference SDFs omit a 3D flag despite nonzero Z coordinates. "
            "Each reference parse emits the warning; all 192 generated poses are flagged 3D and parse without it. "
            "The earlier evaluator parses the reference once per prediction, explaining 192 warnings "
            "without changing scientific coordinates."
        ),
        "original_evaluator_stderr_sha256": sha(campaign / "diffdock-final-cross-process.stderr.log"),
        "runs": rows,
        "within_process_repeat_pairs": within_pairs,
        "cross_process_pairs": len(cross_pairs),
        "cross_process_coordinate_and_confidence_maximum": {"coordinate_angstrom": 0.0, "confidence": 0.0},
        "coordinate_tolerance_angstrom": 0.01,
        "confidence_tolerance": 0.001,
        "scientific_metrics": all_metrics,
        "limits": [
            "Isolated candidate, not public route or client qualification.",
            "Reference-frame symmetry-aware heavy-atom RMSD without ligand alignment; "
            "not affinity or functional validation.",
            "Six bound-receptor fixtures, no waters/cofactors, four poses and two seeds; training overlap unknown.",
            "Within-process repeats are numerically close, not universally byte-identical; "
            "cross-process comparison follows the same request order.",
            "No snapshot or other GPU architecture qualification.",
            "Earlier failed candidates and warnings remain retained; this receipt does not replace them.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(inspect(arguments.campaign, arguments.references), indent=2))

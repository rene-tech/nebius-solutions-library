"""Typed file-to-native-command mapping; scientific input files stay native."""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator
from fs2_gromacs.contracts import canonical, relative_path

from . import PARAMETER_SCHEMA

NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,47}$"}
PATH = {"type": "string", "minLength": 1, "maxLength": 512}
BACKENDS = ["cuda-spfp", "cuda-dpfp", "cpu"]
KINDS = ["pmemd", "tleap", "cpptraj", "parmed", "antechamber", "parmchk2", "mmpbsa"]


def request_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fs2-serve.nebius.ai/schema/amber-workflow-request/v1",
        "type": "object", "additionalProperties": False,
        "required": ["schema", "jobs"],
        "description": "Ordered native AMBER workflows in a complete gzip-tar bundle. MDIN, topology, coordinates, restraints, reference files and alchemical context remain explicit. Native PMEMD CUDA SPFP/DPFP and CPU are distinct choices. Completed stages checkpoint the full stopped workspace; an interrupted active stage retries from the preceding committed stage, not an arbitrary partial restart.",
        "properties": {
            "schema": {"const": PARAMETER_SCHEMA},
            "jobs": {"type": "array", "minItems": 1, "maxItems": 128, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "steps"], "properties": {
                    "id": NAME,
                    "steps": {"type": "array", "minItems": 1, "maxItems": 128, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["id", "input"], "properties": {
                            "id": NAME,
                            "kind": {"enum": KINDS, "default": "pmemd"},
                            "input": {**PATH, "description": "Native MDIN or native tool script, relative to the step directory. No shell command or arbitrary executable interface is added."},
                            "directory": {**PATH, "default": "."},
                            "topology": {**PATH, "description": "PMEMD topology; optional default topology for cpptraj or ParmEd."},
                            "coordinates": {**PATH, "description": "PMEMD initial coordinates/restart; optional ParmEd coordinates."},
                            "reference": {**PATH, "description": "PMEMD restraint reference, passed with -ref. All auxiliary native dependencies must also be bundled."},
                            "backend": {"enum": BACKENDS, "description": "PMEMD-only per-stage precision/accelerator override."},
                            "task": {"enum": ["dynamics", "minimization"], "description": "PMEMD completion assertion only; never rewrites imin or the native scientific settings."},
                            "expected_nsteps": {"type": "integer", "minimum": 1, "maximum": 2147483647, "description": "Required for dynamics: exact requested native nstlim, checked against native input echo and final NSTEP. A timlim early exit is incomplete."},
                            "output_prefix": {**PATH, "description": "Native output stem, defaulting to step ID: PMEMD uses .mdout/.rst7/.nc/.mdvel/.mden/.mdinfo; MMPBSA uses .dat/.csv plus a stage-specific intermediate prefix."},
                            "output": {**PATH, "description": "Antechamber MOL2 or parmchk2 FRCMOD output filename."},
                            "input_format": {"enum": ["pdb", "mol2", "sdf"], "description": "Explicit molecular input format; parmchk2 currently accepts MOL2 only."},
                            "atom_types": {"enum": ["gaff", "gaff2"], "description": "Explicit native small-molecule parameter set; default GAFF2."},
                            "net_charge": {"type": "integer", "minimum": -20, "maximum": 20, "description": "Antechamber requested total molecular charge in electron units."},
                            "multiplicity": {"type": "integer", "minimum": 1, "maximum": 9},
                            "residue_name": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9]{0,2}$"},
                            "charge_equivalence": {"enum": [0, 1, 2], "description": "Native-eq option:0none,1atomic paths,2paths and stereochemistry. Default1; changing it is a scientific input change."},
                            "charge_tolerance_e": {"type": "number", "minimum": 0.000001, "maximum": 0.05, "description": "Explicit final MOL2 charge-sum assertion; default0.001e. Residual is always reported, never silently corrected."},
                            "complex_topology": PATH,
                            "receptor_topology": PATH,
                            "ligand_topology": PATH,
                            "solvated_topology": PATH,
                            "trajectories": {"type": "array", "minItems": 1, "maxItems": 128, "items": PATH, "uniqueItems": True, "description": "MMPBSA explicit trajectory filenames in order, not shell globs."},
                            "expected_frames": {"type": "integer", "minimum": 1, "maximum": 1000000, "description": "MMPBSA exact analyzed-frame assertion for every component energy table; never rewrites native frame selection."},
                            "use_mdins": {"type": "boolean", "description": "MMPBSA-only native-use-mdins: caller supplies complete prefix.intermediate_gb.mdin/pb.mdin context. No native MDIN is rewritten by the worker."},
                            "expected_outputs": {"type": "array", "items": PATH, "maxItems": 256, "uniqueItems": True, "default": [], "description": "Additional nonempty native outputs required for completion. Required nonempty for preparation/analysis tools; their scientific interpretation remains workflow-specific."},
                        },
                    }},
                },
            }},
            "backend": {"enum": BACKENDS, "default": "cuda-spfp"},
            "threads": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1, "description": "CPU/tool threading limit; one process and at most one GPU. No multi-GPU/MPI qualification is implied."},
            "max_wall_seconds": {"type": "integer", "minimum": 60, "maximum": 259200, "default": 21600},
            "max_output_bytes": {"type": "integer", "minimum": 1048576, "maximum": 51539607552, "default": 4294967296, "description": "Full workspace budget; individual files above 5 GiB are not qualified."},
            "output_destination": {"enum": ["customer-bucket", "platform-artifacts"], "default": "customer-bucket"},
            "output_prefix": {**PATH, "default": "runs/amber"},
        },
    }


def normalize(value: object) -> dict[str, Any]:
    schema = request_schema()
    Draft202012Validator(schema).validate(value)
    result = copy.deepcopy(value)
    for name, spec in schema["properties"].items():
        if "default" in spec:
            result.setdefault(name, copy.deepcopy(spec["default"]))
    relative_path(result["output_prefix"])
    jobs = result["jobs"]
    if len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("job IDs must be unique")
    for job in jobs:
        if len({step["id"] for step in job["steps"]}) != len(job["steps"]):
            raise ValueError("step IDs must be unique within each job")
        outputs = set()
        charge_directories = set()
        for step in job["steps"]:
            step.setdefault("kind", "pmemd")
            step.setdefault("directory", ".")
            step.setdefault("expected_outputs", [])
            relative_path(step["directory"], allow_dot=True)
            for key in ("input", "topology", "coordinates", "reference", "output_prefix", "output", "complex_topology", "receptor_topology", "ligand_topology", "solvated_topology"):
                if key in step:
                    relative_path(step[key])
            for path in step["expected_outputs"]:
                relative_path(path)
            for path in step.get("trajectories", []):
                relative_path(path)
            common = {"id", "kind", "directory", "input", "expected_outputs"}
            allowed = {
                "pmemd": common | {"topology", "coordinates", "reference", "backend", "task", "expected_nsteps", "output_prefix"},
                "tleap": common,
                "cpptraj": common | {"topology"},
                "parmed": common | {"topology", "coordinates"},
                "antechamber": common | {"output", "input_format", "atom_types", "net_charge", "multiplicity", "residue_name", "charge_equivalence", "charge_tolerance_e"},
                "parmchk2": common | {"output", "input_format", "atom_types"},
                "mmpbsa": common | {"output_prefix", "complex_topology", "receptor_topology", "ligand_topology", "solvated_topology", "trajectories", "expected_frames", "use_mdins"},
            }[step["kind"]]
            if set(step) - allowed:
                raise ValueError("step contains fields not supported by its native tool kind")
            if step["kind"] == "pmemd":
                if not {"topology", "coordinates"} <= step.keys():
                    raise ValueError("PMEMD requires topology and coordinates")
                step.setdefault("backend", result["backend"])
                step.setdefault("task", "dynamics")
                step.setdefault("output_prefix", step["id"])
                if step["task"] == "dynamics" and "expected_nsteps" not in step:
                    raise ValueError("PMEMD dynamics requires an exact expected_nsteps assertion")
                if step["task"] == "minimization" and "expected_nsteps" in step:
                    raise ValueError("minimization can converge before maxcyc; expected_nsteps is dynamics-only")
                key = (step["directory"], step["output_prefix"])
                if key in outputs:
                    raise ValueError("PMEMD output prefixes must be unique within each directory")
                outputs.add(key)
                generated = {step["output_prefix"] + suffix for suffix in (".mdout", ".rst7", ".nc", ".mdvel", ".mden", ".mdinfo")}
                if generated.intersection(step[k] for k in ("input", "topology", "coordinates", "reference") if k in step):
                    raise ValueError("PMEMD outputs must not overwrite their own native inputs")
            elif step["kind"] in {"antechamber", "parmchk2"}:
                if not {"output", "input_format"} <= step.keys():
                    raise ValueError("molecular preparation requires explicit input_format and output")
                if step["input"] == step["output"]:
                    raise ValueError("native molecular output must not overwrite its input")
                step.setdefault("atom_types", "gaff2")
                if step["kind"] == "antechamber":
                    if step["directory"] in charge_directories:
                        raise ValueError("each Antechamber calculation needs a distinct directory to preserve SQM/intermediate files")
                    charge_directories.add(step["directory"])
                    for key, value in (("net_charge", 0), ("multiplicity", 1), ("residue_name", "LIG"), ("charge_equivalence", 1), ("charge_tolerance_e", 0.001)):
                        step.setdefault(key, value)
                elif step["input_format"] != "mol2":
                    raise ValueError("parmchk2 initial typed surface requires MOL2 input")
                if step["output"] not in step["expected_outputs"]:
                    step["expected_outputs"].append(step["output"])
            elif step["kind"] == "mmpbsa":
                step.setdefault("use_mdins", False)
                if not {"complex_topology", "trajectories", "expected_frames"} <= step.keys():
                    raise ValueError("MMPBSA requires a complex topology, explicit trajectories and expected_frames")
                if ("receptor_topology" in step) != ("ligand_topology" in step):
                    raise ValueError("binding analysis requires both receptor and ligand topologies; omit both only for stability analysis")
                step.setdefault("output_prefix", step["id"])
                key = (step["directory"], step["output_prefix"])
                if key in outputs:
                    raise ValueError("native output prefixes must be unique within each directory")
                outputs.add(key)
                generated = {step["output_prefix"] + suffix for suffix in (".dat", ".csv")}
                if generated.intersection([step["input"], *step["trajectories"], *(step[key] for key in ("complex_topology", "receptor_topology", "ligand_topology", "solvated_topology") if key in step)]):
                    raise ValueError("MMPBSA output must not overwrite a native input")
                for name in sorted(generated):
                    if name not in step["expected_outputs"]:
                        step["expected_outputs"].append(name)
            else:
                if not step["expected_outputs"]:
                    raise ValueError("preparation/analysis steps require expected_outputs")
    return result

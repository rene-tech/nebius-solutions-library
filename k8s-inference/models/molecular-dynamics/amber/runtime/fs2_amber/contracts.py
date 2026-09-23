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
KINDS = ["pmemd", "tleap", "cpptraj", "parmed"]


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
                            "output_prefix": {**PATH, "description": "PMEMD-only native output stem, defaulting to step ID. Generates .mdout, .rst7, .nc, .mdvel, .mden and .mdinfo names using fixed native flags."},
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
        for step in job["steps"]:
            step.setdefault("kind", "pmemd")
            step.setdefault("directory", ".")
            step.setdefault("expected_outputs", [])
            relative_path(step["directory"], allow_dot=True)
            for key in ("input", "topology", "coordinates", "reference", "output_prefix"):
                if key in step:
                    relative_path(step[key])
            for path in step["expected_outputs"]:
                relative_path(path)
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
            else:
                if not step["expected_outputs"]:
                    raise ValueError("preparation/analysis steps require expected_outputs")
                forbidden = {"backend", "task", "expected_nsteps", "reference", "output_prefix"}
                if step["kind"] == "tleap":
                    forbidden |= {"topology", "coordinates"}
                if step["kind"] == "cpptraj":
                    forbidden.add("coordinates")
                if forbidden.intersection(step):
                    raise ValueError("step contains fields not supported by its native tool kind")
    return result


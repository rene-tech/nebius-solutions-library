"""Typed native script workflow; no script rewriting or synthetic physics."""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator
from fs2_gromacs.contracts import canonical, relative_path

from . import PARAMETER_SCHEMA

NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,47}$"}
PATH = {"type": "string", "minLength": 1, "maxLength": 512}
BACKENDS = ["kokkos-cuda", "cpu"]


def request_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fs2-serve.nebius.ai/schema/lammps-workflow-request/v1",
        "type": "object", "additionalProperties": False,
        "required": ["schema", "jobs"],
        "description": "Ordered native LAMMPS scripts in a complete gzip-tar bundle: include/data/potential files and explicit continuation context. Scientific parameters and units remain in the scripts. Independent jobs have isolated files and random seeds chosen by the customer.",
        "properties": {
            "schema": {"const": PARAMETER_SCHEMA},
            "jobs": {"type": "array", "minItems": 1, "maxItems": 128, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "steps"], "properties": {
                    "id": NAME,
                    "steps": {"type": "array", "minItems": 1, "maxItems": 64, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["id", "input"], "properties": {
                            "id": NAME,
                            "input": {**PATH, "description": "Native LAMMPS input script relative to directory. Includes, native variables, loops, minimization, dynamics and analysis retain native semantics."},
                            "directory": {**PATH, "default": "."},
                            "backend": {"enum": BACKENDS, "description": "Optional per-stage accelerator profile; CPU fallback must be explicit."},
                            "variables": {"type": "object", "maxProperties": 64, "propertyNames": {"pattern": "^[A-Za-z][A-Za-z0-9_]{0,63}$"}, "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4096}, "default": {}, "description": "Native index variables supplied as separate -var name value argv entries. Names beginning fs2_ are reserved."},
                            "expected_outputs": {"type": "array", "items": PATH, "maxItems": 256, "uniqueItems": True, "default": []},
                            "continuation": {
                                "type": "object", "additionalProperties": False,
                                "required": ["input", "restart_file", "progress_file", "target_step"],
                                "description": "Opt-in native segmentation. Scripts must write a closed restart and integer timestep after each run, use fs2_segment_seconds timer timeout, and recreate complete restart context. Worker independently reads the restart timestep. Outputs must preserve segment history.",
                                "properties": {"input": PATH, "restart_file": PATH, "progress_file": PATH, "target_step": {"type": "integer", "minimum": 1, "maximum": 9007199254740991}},
                            },
                        },
                    }},
                },
            }},
            "backend": {"enum": BACKENDS, "default": "kokkos-cuda", "description": "One MPI rank and at most one GPU. KOKKOS acceleration applies only to installed accelerated styles; retain engine logs to inspect dispatch. The separate GPU package is not built into this pinned NVIDIA image."},
            "threads": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1, "description": "OpenMP threads for native CPU styles. The pinned KOKKOS CUDA build has a Serial host backend and always uses one KOKKOS host thread."},
            "segment_seconds": {"type": "integer", "minimum": 5, "maximum": 3600, "default": 300, "description": "Offered native timer duration for scripts opting into continuation. Native timestep boundaries can delay exit. Does not rewrite arbitrary scripts."},
            "max_wall_seconds": {"type": "integer", "minimum": 60, "maximum": 259200, "default": 21600},
            "max_output_bytes": {"type": "integer", "minimum": 1048576, "maximum": 51539607552, "default": 4294967296, "description": "Total workspace budget. Individual files above 5 GiB are not qualified; output cadence and total retained size must fit the assigned shape."},
            "output_destination": {"enum": ["customer-bucket", "platform-artifacts"], "default": "customer-bucket"},
            "output_prefix": {**PATH, "default": "runs/lammps"},
        },
    }


def normalize(value: object) -> dict[str, Any]:
    schema = request_schema()
    Draft202012Validator(schema).validate(value)
    result = copy.deepcopy(value)
    for name, spec in schema["properties"].items():
        if "default" in spec:
            result.setdefault(name, spec["default"])
    relative_path(result["output_prefix"])
    jobs = result["jobs"]
    if len({j["id"] for j in jobs}) != len(jobs):
        raise ValueError("job IDs must be unique")
    for job in jobs:
        if len({s["id"] for s in job["steps"]}) != len(job["steps"]):
            raise ValueError("step IDs must be unique within each job")
        for step in job["steps"]:
            step.setdefault("directory", ".")
            step.setdefault("variables", {})
            step.setdefault("expected_outputs", [])
            step.setdefault("backend", result["backend"])
            relative_path(step["directory"], allow_dot=True)
            for name in [step["input"], *step["expected_outputs"]]:
                relative_path(name)
            if c := step.get("continuation"):
                for name in ("input", "restart_file", "progress_file"):
                    relative_path(c[name])
                if len({c["input"], c["restart_file"], c["progress_file"]}) != 3:
                    raise ValueError("continuation script, restart and progress paths must differ")
            for name, argument in step["variables"].items():
                if name.startswith("fs2_") or any(ord(c) < 32 for c in argument):
                    raise ValueError("reserved variable name or control character in native variable")
    return result

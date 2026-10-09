"""Native configuration/Tcl workflows; physics remains in the supplied files."""
from __future__ import annotations

import copy

from jsonschema import Draft202012Validator
from fs2_gromacs.contracts import canonical, relative_path

from . import PARAMETER_SCHEMA

NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,47}$"}
PATH = {"type": "string", "minLength": 1, "maxLength": 512}


def request_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fs2-serve.nebius.ai/schema/namd-workflow-request/v1",
        "type": "object", "additionalProperties": False, "required": ["schema", "jobs"],
        "description": "NVIDIA NAMD 3.0.2 single-GPU native workflows. One immutable gzip-tar bundle holds config/Tcl, PSF/PDB or compatible topology, parameters, and restart/bias files. Native Tcl is executable user code inside an isolated worker, not a sandboxed expression language. GBIS and spinAngle Colvars are unavailable in this pinned build. No multi-node or persistent GPU snapshot claim.",
        "properties": {
            "schema": {"const": PARAMETER_SCHEMA},
            "threads": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4},
            "max_wall_seconds": {"type": "integer", "minimum": 60, "maximum": 259200, "default": 21600},
            "max_output_bytes": {"type": "integer", "minimum": 1048576, "maximum": 51539607552, "default": 4294967296},
            "output_destination": {"enum": ["customer-bucket", "platform-artifacts"], "default": "customer-bucket"},
            "output_prefix": {**PATH, "default": "runs/namd"},
            "jobs": {"type": "array", "minItems": 1, "maxItems": 128, "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "steps"],
                "properties": {"id": NAME, "steps": {"type": "array", "minItems": 1, "maxItems": 64, "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "config", "mode"],
                    "properties": {
                        "id": NAME, "config": PATH,
                        "directory": {**PATH, "default": "."},
                        "mode": {"enum": ["dynamics", "native", "prepare"], "description": "dynamics: configuration-only Tcl plus finite wrapper-managed run segments; native: complete NAMD Tcl program, completion-boundary recovery only; prepare: bundled psfgen Tcl script, completion-boundary recovery only."},
                        "gpu_mode": {"enum": ["resident", "offload"], "default": "resident"},
                        "steps": {"type": "integer", "minimum": 1, "maximum": 2000000000},
                        "segment_steps": {"type": "integer", "minimum": 100, "maximum": 10000000, "default": 50000,
                                          "description": "Finite native run length between closed-output checkpoints. Must align with stepsPerCycle and nonbonded/PME intervals. Each segment creates independent part files; cadence stays in the native config."},
                        "first_step": {"type": "integer", "minimum": 0, "maximum": 2000000000, "default": 0},
                        "output_prefix": {**PATH, "description": "Managed dynamics output basename; each segment gets .partNNNNNN, and final .coor/.vel/.xsc aliases are produced. Omit outputName/DCDfile/restartName from configuration-only scripts."},
                        "restart": {"type": "object", "additionalProperties": False,
                                    "required": ["coordinates", "velocities", "cell"],
                                    "properties": {"coordinates": PATH, "velocities": PATH, "cell": PATH, "colvars_state": PATH},
                                    "description": "External native continuation. Coordinates, velocities, cell and enabled Colvars bias state must come from the same checkpoint; XSC timestep must equal first_step. Config uses fs2_restart to omit fresh velocity/cell initialization."},
                        "initialize_colvars": {"type": "boolean", "default": False, "description": "Explicitly introduce a new bias when starting this dynamics stage from an unbiased native checkpoint. Subsequent segments must restore the resulting bias state."},
                        "expected_outputs": {"type": "array", "maxItems": 256, "uniqueItems": True, "items": PATH, "default": []},
                    },
                    "allOf": [{"if": {"properties": {"mode": {"const": "dynamics"}}},
                               "then": {"required": ["steps", "output_prefix"]},
                               "else": {"not": {"anyOf": [{"required": ["steps"]}, {"required": ["restart"]}, {"required": ["output_prefix"]}]}}}],
                }}}}},
        },
    }


def normalize(value):
    schema = request_schema()
    Draft202012Validator(schema).validate(value)
    result = copy.deepcopy(value)
    for key, spec in schema["properties"].items():
        if "default" in spec:
            result.setdefault(key, spec["default"])
    relative_path(result["output_prefix"])
    ids = [job["id"] for job in result["jobs"]]
    if len(set(ids)) != len(ids):
        raise ValueError("job IDs must be unique")
    step_props = schema["properties"]["jobs"]["items"]["properties"]["steps"]["items"]["properties"]
    for job in result["jobs"]:
        ids = [step["id"] for step in job["steps"]]
        if len(set(ids)) != len(ids):
            raise ValueError("step IDs must be unique")
        for step in job["steps"]:
            for key, spec in step_props.items():
                if "default" in spec:
                    step.setdefault(key, spec["default"])
            relative_path(step["directory"], allow_dot=True)
            relative_path(step["config"])
            if "/" in step["config"]:
                raise ValueError("config must be a filename within directory; NAMD changes directory to its configuration")
            for path in step.get("restart", {}).values():
                relative_path(path)
            for path in step["expected_outputs"]:
                relative_path(path)
            if "output_prefix" in step:
                relative_path(step["output_prefix"])
            if step["mode"] != "dynamics" and not step["expected_outputs"]:
                raise ValueError("native/prepare steps require explicit expected_outputs")
    return result

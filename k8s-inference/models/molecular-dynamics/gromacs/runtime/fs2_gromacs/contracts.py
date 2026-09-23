"""One native workflow contract, projected into REST/MCP by build_contracts.py.

The App does not invent a scientific protocol. It executes native GROMACS tools
with explicit input files and noninteractive selections, inside a run workspace.
Platform checkpoint/thread flags are separate from the customer's scientific
parameters. No shell, arbitrary executables, or caller-provided environment.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import PurePosixPath
from typing import Any, cast

from jsonschema import Draft202012Validator

from . import PARAMETER_SCHEMA

# Captured from `gmx help commands` in the pinned NVIDIA image, not inferred
# from the latest upstream manual. tune_pme can launch arbitrary mdrun binaries
# and is deliberately an operator benchmark tool, not a tenant workflow step.
COMMANDS = tuple("""
anaeig analyze angle awh bar bundle check chi cluster clustsize confrms
convert-tpr convert-trj covar current density densmap densorder dielectric
dipoles disre distance dos dssp dump dyecoupl editconf eneconv enemat energy
extract-cluster filter freevolume gangle genconf genion genrestr grompp gyrate
gyrate-legacy h2order hbond hbond-legacy helix helixorient help hydorder
insert-molecules lie make_edi make_ndx mdmat mdrun mindist mk_angndx msd
nmeig nmens nmr nmtraj nonbonded-benchmark order pairdist pdb2gmx pme_error
polystat potential principal rama rdf report-methods rms rmsdist rmsf rotacf
rotmat saltbr sans-legacy sasa saxs-legacy scattering select sham sigeps solvate
sorient spatial spol tcaf traj trajectory trjcat trjconv trjorder vanhove
velacc wham wheel x2top xpm2ps
""".split())
MANAGED_MDRUN = frozenset({
    "-cpi", "-cpo", "-cpt", "-cpnum", "-nocpnum", "-maxh", "-append", "-noappend",
    "-nt", "-ntmpi", "-ntomp", "-ntomp_pme", "-multidir", "-replex", "-nex", "-reseed",
    "-nsteps", "-plumed", "-imdwait", "-imdpull", "-imdport", "-imdterm",
})
NAME = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,47}$"}
RELATIVE = {"type": "string", "minLength": 1, "maxLength": 512}


def request_schema() -> dict[str, Any]:
    step = {
        "type": "object", "additionalProperties": False,
        "required": ["id", "command", "args"],
        "properties": {
            "id": NAME,
            "command": {"type": "string", "enum": list(COMMANDS),
                        "description": "Native tool from the pinned NVIDIA GROMACS distribution; not a shell command."},
            "args": {"type": "array", "maxItems": 256, "items": {"oneOf": [
                {"type": "string", "minLength": 1, "maxLength": 4096},
                {"type": "object", "additionalProperties": False, "required": ["files"],
                 "properties": {"files": RELATIVE}},
            ]}, "description": "Exact argv tokens, or an explicit {files: relative-pattern} expansion for trajectory/energy parts. Patterns must match existing files. No shell expansion. Do not include gmx or platform-managed mdrun flags."},
            "directory": {**RELATIVE, "default": ".", "description": "Working directory relative to the extracted input bundle."},
            "stdin": {"type": "string", "maxLength": 16384, "default": "",
                      "description": "Explicit newline-separated interactive selections, e.g. Protein\\n0\\n. EOF follows; no interactive terminal."},
            "restart_checkpoint": {**RELATIVE, "description": "Optional native .cpt path relative to directory. Missing files are errors, never a silent fresh simulation."},
            "expected_outputs": {"type": "array", "maxItems": 256, "uniqueItems": True,
                                 "items": RELATIVE, "default": [],
                                 "description": "Files relative to directory which must exist and be nonempty after this command. Do not use globs."},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://fs2-serve.nebius.ai/schema/gromacs-workflow-request/v1",
        "type": "object", "additionalProperties": False,
        "required": ["schema", "jobs"],
        "properties": {
            "schema": {"const": PARAMETER_SCHEMA},
            "jobs": {
                "type": "array", "minItems": 1, "maxItems": 128,
                "description": "Independent single-GPU jobs, e.g. replicas or lambda windows. Steps within each job run in order. Different jobs do not share mutable files.",
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["id", "steps"], "properties": {
                              "id": NAME,
                              "steps": {"type": "array", "minItems": 1, "maxItems": 64, "items": step},
                          }},
            },
            "threads": {"type": "integer", "minimum": 1, "maximum": 8, "default": 8,
                        "description": "OpenMP threads per single thread-MPI rank, within the initial eight-vCPU execution shape."},
            "checkpoint_minutes": {"type": "number", "minimum": 0.1, "maximum": 60, "default": 5,
                                   "description": "Native .cpt write cadence. A local checkpoint alone does not survive node loss."},
            "segment_minutes": {"type": "number", "minimum": 0.1, "maximum": 60, "default": 5,
                                "description": "MD stops cleanly at this wall-time interval to publish a coherent checkpoint and closed output segment; then resumes with -noappend. Native neighbor-search boundaries can delay the stop."},
            "max_wall_seconds": {"type": "integer", "minimum": 60, "maximum": 259200, "default": 21600,
                                 "description": "Per-job execution budget, including checkpoint I/O. Exhaustion is reported as incomplete, not scientific success."},
            "max_output_bytes": {"type": "integer", "minimum": 1048576, "maximum": 51539607552, "default": 4294967296,
                                 "description": "Maximum total files in the job workspace. Must fit the assigned scratch and customer bucket quota. Larger budgets require an operator execution shape."},
            "output_destination": {"enum": ["customer-bucket", "platform-artifacts"], "default": "customer-bucket",
                                   "description": "Copy coherent native checkpoints and trajectory segments into the submitting user's assigned tenant/user bucket, as well as durable platform artifacts. Customer objects are retained until the customer deletes them or applies their bucket lifecycle; the platform never silently deletes them."},
            "output_prefix": {**RELATIVE, "default": "runs/gromacs",
                              "description": "Customer-bucket prefix. The platform appends operation ID and replica ID, so independent runs cannot overwrite each other."},
        },
        "description": "NVIDIA-optimized GROMACS native preparation, MD, minimization, analysis and free-energy tools. Supply one immutable gzip-tar input bundle containing all required topology/force-field includes, coordinates, MDP/TPR and optional checkpoints. Physics and trajectory cadence remain in the supplied protocol. Docking pose search and external extension builds are separate capabilities.",
    }


def relative_path(value: str, *, allow_dot: bool = False) -> str:
    if value == "." and allow_dot:
        return value
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(p in {"", ".", ".."} for p in value.split("/"))
            or any(ord(c) < 32 for c in value) or len(value) > 512):
        raise ValueError("paths must be normalized relative workspace paths")
    if PurePosixPath(value).parts[0] == ".fs2":
        raise ValueError(".fs2 is reserved for platform state")
    return value


def normalize(value: object) -> dict[str, Any]:
    Draft202012Validator(request_schema()).validate(value)
    result = copy.deepcopy(cast(dict[str, Any], value))
    for name, spec in request_schema()["properties"].items():
        if "default" in spec:
            result.setdefault(name, spec["default"])
    relative_path(result["output_prefix"])
    ids = [job["id"] for job in result["jobs"]]
    if len(set(ids)) != len(ids):
        raise ValueError("job IDs must be unique")
    for job in result["jobs"]:
        seen = set()
        for step in job["steps"]:
            if step["id"] in seen:
                raise ValueError("step IDs must be unique within a job")
            seen.add(step["id"])
            step.setdefault("directory", ".")
            step.setdefault("stdin", "")
            step.setdefault("expected_outputs", [])
            relative_path(step["directory"], allow_dot=True)
            if "restart_checkpoint" in step:
                if step["command"] != "mdrun":
                    raise ValueError("restart_checkpoint is only valid for mdrun")
                relative_path(step["restart_checkpoint"])
            for path in step["expected_outputs"]:
                relative_path(path)
            for token in step["args"]:
                if isinstance(token, dict):
                    relative_path(token["files"])
                    continue
                if (any(ord(c) < 32 for c in token) or token.startswith("/") or "\\" in token
                        or re.search(r"(^|[/=\s])\.\.([/\s]|$)", token)):
                    raise ValueError("arguments must use workspace-relative paths without control characters")
                if step["command"] == "mdrun" and token in MANAGED_MDRUN:
                    raise ValueError(f"{token} is platform-managed or requires a different execution shape")
    return result


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

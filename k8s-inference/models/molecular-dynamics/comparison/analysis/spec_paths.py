"""Explicit spec-relative paths; never depend on the caller's working directory."""
from copy import deepcopy
from pathlib import Path


def map_paths(spec, transform):
    result = deepcopy(spec)

    def path(value):
        if isinstance(value, list):
            return [path(item) for item in value]
        if not isinstance(value, str) or not value or "://" in value:
            raise ValueError("analysis dependencies must be explicit local paths")
        return str(transform(value))

    result["master_directory"] = path(result["master_directory"])
    for run in result["runs"]:
        for key in ("trajectory", "native_topology", "production_log", "provenance_files"):
            if key in run:
                run[key] = path(run[key])
        run["thermo"]["path"] = path(run["thermo"]["path"])
        if run.get("pressure_observations"):
            for key in ("path", "provenance_files"):
                run["pressure_observations"][key] = path(run["pressure_observations"][key])
    return result


def resolve_spec(spec, spec_file):
    mode = spec.get("path_base", "absolute")
    if mode not in ("absolute", "spec-directory"):
        raise ValueError("unknown analysis path_base")
    base = Path(spec_file).resolve().parent

    def resolve(value):
        path = Path(value)
        if not path.is_absolute():
            if mode != "spec-directory":
                raise ValueError("relative dependency requires path_base=spec-directory")
            path = base / path
        return path.resolve()

    return map_paths(spec, resolve)

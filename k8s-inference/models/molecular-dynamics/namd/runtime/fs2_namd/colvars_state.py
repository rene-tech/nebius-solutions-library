"""Version-scoped NAMD 3.0.2 explicit-hill state compatibility and verification.

The shipped Colvars reader skips old explicit hills unless its *state* contains
keepHills on. The ungridded writer omits that marker. Preserve original bytes,
derive only the missing marker, then verify an actual native save after loading
and before advancing dynamics. No scientific configuration is rewritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

REPAIR_ID = "nvidia-namd-3.0.2-colvars-2024-06-04-ungridded-explicit-hills/v1"


def blocks(text, keyword):
    found = []
    for match in re.finditer(r"(?m)^\s*" + re.escape(keyword) + r"\s*\{", text):
        start, depth, index = match.end(), 1, match.end()
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise ValueError("unterminated native Colvars state block")
        found.append((start, index - 1, text[start:index - 1]))
    return found


def metadynamics(text):
    result = {}
    for start, end, body in blocks(text, "metadynamics"):
        config = blocks(body, "configuration")
        if len(config) != 1:
            raise ValueError("unsupported native metadynamics state configuration")
        name = re.search(r"(?m)^\s*name\s+(\S+)\s*$", config[0][2])
        if not name or name[1] in result:
            raise ValueError("native metadynamics state needs unique bias names")
        hills = []
        for _, _, hill in blocks(body, "hill"):
            row = {}
            for line in hill.splitlines():
                if not line.strip():
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) != 2 or parts[0] in row or parts[0] not in {"step", "weight", "centers", "widths", "replicaID"}:
                    raise ValueError("unsupported native metadynamics hill field")
                tokens = re.findall(r"[(),]|[^\s(),]+", parts[1])
                values = []
                for token in tokens:
                    try:
                        number = float(token)
                    except ValueError:
                        if parts[0] != "replicaID" and token not in {"(", ")", ","}:
                            raise ValueError("unsupported native metadynamics hill value")
                        values.append(token)
                    else:
                        if not math.isfinite(number):
                            raise ValueError("non-finite native metadynamics hill")
                        values.append(number)
                row[parts[0]] = values
            if not {"step", "weight", "centers", "widths"}.issubset(row):
                raise ValueError("incomplete native metadynamics hill")
            hills.append(row)
        result[name[1]] = {"hills": hills, "has_grids": bool(re.search(r"\bhills_energy(?:_gradients)?\s*\{", body)),
                           "keeps_hills": bool(re.search(r"(?mi)^\s*keepHills\s+(?:on|yes|true|1)\s*$", config[0][2])),
                           "configuration_end": start + config[0][1]}
    return result


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(original, derived):
    text = original.read_text()
    parsed = metadynamics(text)
    repairs = []
    for name, value in parsed.items():
        if value["hills"] and not value["has_grids"] and not value["keeps_hills"]:
            configuration = next(body for _, _, body in blocks(text, "metadynamics")
                                 if re.search(r"(?m)^\s*name\s+" + re.escape(name) + r"\s*$", body))
            if re.search(r"(?mi)^\s*keepHills\s+", blocks(configuration, "configuration")[0][2]):
                raise ValueError("unsupported explicit keepHills override in ungridded native state")
            repairs.append((value["configuration_end"], name))
    if repairs:
        version = re.search(r"(?m)^\s*version\s+(\S+)\s*$", text)
        if not version or version[1] != "2024-06-04":
            raise ValueError("explicit-hill compatibility repair is scoped to the pinned Colvars 2024-06-04 state format")
        if original.resolve() == derived.resolve():
            raise ValueError("derived restart must preserve the original native bias-state bytes")
    for index, name in sorted(repairs, reverse=True):
        text = text[:index] + "  keepHills on\n  " + text[index:]
    target = derived if repairs else original
    if repairs:
        derived.write_text(text)
    return {"repair": REPAIR_ID if repairs else None, "biases_repaired": [name for _, name in repairs],
            "original_sha256": sha(original), "loaded_input_sha256": sha(target),
            "original": original.name, "loaded_input": target.name,
            "explicit_hills_before": {name: len(value["hills"]) for name, value in parsed.items()}}


def verify(original, loaded, expected_step):
    # Import locally to avoid a worker/module import cycle.
    from .worker import colvars_step
    if colvars_step(original) != expected_step or colvars_step(loaded) != expected_step:
        raise ValueError("Colvars pre-run round-trip timestep differs from native checkpoint")
    before, after = metadynamics(original.read_text()), metadynamics(loaded.read_text())
    for name, prior in before.items():
        if name not in after or len(prior["hills"]) != len(after[name]["hills"]):
            raise ValueError("native Colvars restore lost explicit metadynamics hills before advancing")
        for left, right in zip(prior["hills"], after[name]["hills"]):
            if left.keys() != right.keys():
                raise ValueError("native Colvars restore changed hill fields")
            for key in left:
                if len(left[key]) != len(right[key]):
                    raise ValueError("native Colvars restore changed hill dimensions")
                for a, b in zip(left[key], right[key]):
                    same = math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-14) if isinstance(a, float) and isinstance(b, float) else a == b
                    if not same:
                        raise ValueError("native Colvars restore changed explicit hill values")
    return {"verified_step": expected_step, "explicit_hills_restored": {name: len(value["hills"]) for name, value in before.items()},
            "original_sha256": sha(original), "native_loaded_state_sha256": sha(loaded)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--loaded", type=Path, required=True)
    parser.add_argument("--expected-step", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.original, args.loaded, args.expected_step), sort_keys=True))


if __name__ == "__main__":
    main()

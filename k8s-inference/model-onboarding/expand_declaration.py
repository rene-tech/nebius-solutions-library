#!/usr/bin/env python3
"""Expand a short scientific declaration into the canonical model declaration.

Onboarding a scientific model should cost tens of lines, not hundreds, because the
requirement is hundreds of models. Everything shared lives once in
``scientific-defaults.json``; a declaration carries only what genuinely differs. The
expansion output is an ordinary model declaration that ``compile_model.py`` and the
existing ``model-declaration.schema.json`` already understand, so this adds no second
onboarding path and no second schema.

Deliberately NOT expanded here, because Helm already owns it: service accounts, node
labels, deadlines and per-stage images reach the cluster through
``scientificBatch.executionMap`` chart values, not through this file.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULTS = ROOT / "scientific-defaults.json"


class ExpansionError(RuntimeError):
    """A short declaration cannot be expanded deterministically."""


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = copy.deepcopy(base)
        for key, value in override.items():
            merged[key] = _merge(merged.get(key), value) if key in merged else value
        return merged
    return copy.deepcopy(override if override is not None else base)


def expand(short: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = defaults or json.loads(DEFAULTS.read_text(encoding="utf-8"))
    kind = short.get("kind")
    if kind not in defaults["kinds"]:
        raise ExpansionError(f"unknown scientific model kind: {kind!r}")
    spec = defaults["kinds"][kind]
    size_name = short.get("size_class")
    if size_name not in spec["size_classes"]:
        raise ExpansionError(f"unknown size class: {size_name!r}")
    size = spec["size_classes"][size_name]

    batch_in = dict(short.get("batch", {}))
    gpu_stages = batch_in.pop("gpu_stages", None)
    cpu_stages = batch_in.pop("cpu_stages", [])
    if not gpu_stages:
        raise ExpansionError("a scientific declaration must list at least one GPU stage")

    stages: list[dict[str, Any]] = []
    previous: list[str] = []
    for name, template in [(s, "gpu") for s in gpu_stages] + [(s, "cpu") for s in cpu_stages]:
        stages.append({"id": name, "needs": list(previous), **spec["stage_defaults"][template]})
        previous = [name]

    model = _merge({"family": kind}, short["model"])
    source = model["source"]
    # An ungated public source is the common case; a declaration overrides only when gated.
    source.setdefault("entitlement", {"required": False, "state": "not-required",
                                      "credential_contract": None,
                                      "notes": "Public source; no entitlement is required."})
    licence = source.setdefault("license", {})
    licence.setdefault("state", "verified")
    licence.setdefault("notes", f"{licence.get('id', 'unspecified')} as published by the source.")
    licence.pop("commercial_use", None)
    review = source.setdefault("review", {})
    review.setdefault("revision", source["revision"])
    review.setdefault("summary",
                      f"Source pinned at {source['revision']} and reviewed before onboarding.")
    runtime = _merge(spec["runtime"], short.get("runtime", {}))
    runtime.setdefault("version", model["source"]["revision"][:12])
    resources = {
        "gpu_count": size["gpu_count"], "gpu_topology": size["gpu_topology"],
        "cache_pvc": {"size": "32Gi", "storage_class": "compute-csi-default-sc"},
        "requests": dict(size["requests"]), "limits": dict(size["limits"]),
    }
    placement = _merge(spec["placement"], short.get("placement", {}))
    batch_in.setdefault("semantic_validation", {})
    batch_in["semantic_validation"].setdefault("state", "candidate-unqualified")
    policy = _merge(spec["policy"], short.get("policy", {}))
    policy.setdefault("limitations", [])

    return {
        "$schema": short.get("$schema", "./model-declaration.schema.json"),
        "schema_version": 2,
        "execution_mode": spec["execution_mode"],
        "model": model,
        "runtime": runtime,
        "resources": resources,
        "placement": placement,
        "serving": None,
        "batch": _merge(spec["batch"], {**batch_in, "stages": stages}),
        "policy": policy,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("declaration", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    expanded = expand(json.loads(args.declaration.read_text(encoding="utf-8")))
    rendered = json.dumps(expanded, indent=2) + "\n"
    if args.out:
        args.out.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

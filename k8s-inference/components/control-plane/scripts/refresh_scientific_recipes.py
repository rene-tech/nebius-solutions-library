#!/usr/bin/env python3
"""Keep scientific profiles and their execution map on one digest chain.

``runtime_recipe_sha256`` hashes each registered adapter together with every shared
execution contract, including the localization contract and its schema. Editing
any of those changes the profile execution identity, which changes the matching
execution-map identity and finally the exact map digest rendered by Helm. This
tool derives that whole chain before it writes either contract.

Run with ``--check`` in CI to fail on drift, or without it to rewrite the values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
SOLUTION_ROOT = CONTROL_PLANE_ROOT.parents[1]
PROFILE_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-workload-profiles.json"
EXECUTION_MAP_PATH = SOLUTION_ROOT / "catalog/runtime/contracts/scientific-execution-map.json"

sys.path.insert(0, str(CONTROL_PLANE_ROOT / "src"))

from fs2_serve.scientific_batch.adapters.common import (  # noqa: E402
    ScientificAdapterError,
    runtime_recipe_sha256,
)

_IDENTITY_FIELDS = (
    "model_revision",
    "runtime_image_digest",
    "runtime_recipe_sha256",
    "workload_recipe_sha256",
    "artifact_manifest_digest",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _helm_to_json_bytes(value: object) -> bytes:
    """Match Go ``encoding/json`` bytes used by Helm/Sprig ``toJson``.

    Python and Go both emit compact, key-sorted JSON for this contract, but
    their defaults differ for non-ASCII and HTML-sensitive characters. Go
    retains UTF-8 while escaping ``<``, ``>``, ``&``, U+2028, and U+2029.
    """

    rendered = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for character, escape in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        rendered = rendered.replace(character, escape)
    return rendered.encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _replace(value: dict[str, Any], key: str, expected: object, label: str, drifted: list[str]) -> None:
    recorded = value.get(key)
    if recorded == expected:
        return
    drifted.append(f"{label}: {recorded} -> {expected}")
    value[key] = expected


def _derive_contracts(
    profiles_document: dict[str, Any],
    execution_map: dict[str, Any],
    *,
    solution_root: Path,
    recipe_digest: Callable[[Path, str], str],
) -> list[str]:
    raw_profiles = profiles_document.get("profiles")
    raw_models = execution_map.get("models")
    if not isinstance(raw_profiles, list) or not isinstance(raw_models, list):
        raise SystemExit("scientific profile or execution-map contract is malformed")
    profiles = {
        profile["model_id"]: profile
        for profile in raw_profiles
        if isinstance(profile, dict) and isinstance(profile.get("model_id"), str)
    }
    if len(profiles) != len(raw_profiles):
        raise SystemExit("scientific workload profiles must have unique string model IDs")

    drifted: list[str] = []
    for model_id, profile in profiles.items():
        identity = profile.get("execution_identity")
        workload = profile.get("workload")
        if not isinstance(identity, dict) or not isinstance(workload, dict):
            raise SystemExit(f"{model_id} has no execution identity or workload recipe")
        try:
            expected_runtime_recipe = recipe_digest(solution_root, model_id)
        except ScientificAdapterError as error:
            raise SystemExit(f"{model_id} has no refreshable runtime recipe: {error}") from error
        _replace(
            identity,
            "runtime_recipe_sha256",
            expected_runtime_recipe,
            f"{model_id} runtime_recipe_sha256",
            drifted,
        )
        _replace(
            identity,
            "workload_recipe_sha256",
            _sha256(workload),
            f"{model_id} workload_recipe_sha256",
            drifted,
        )

    derived_identities: dict[str, str | None] = {}
    for model_id, profile in profiles.items():
        identity = profile.get("execution_identity")
        if not isinstance(identity, dict) or any(field not in identity for field in _IDENTITY_FIELDS):
            raise SystemExit(f"{model_id} has an incomplete execution identity")
        identity_payload = {key: value for key, value in identity.items() if key != "execution_identity_sha256"}
        expected_identity = (
            None if any(value is None for value in identity_payload.values()) else _sha256(identity_payload)
        )
        _replace(
            identity,
            "execution_identity_sha256",
            expected_identity,
            f"{model_id} execution_identity_sha256",
            drifted,
        )
        derived_identities[model_id] = expected_identity

    mapped_profiles: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for model in raw_models:
        if not isinstance(model, dict) or not isinstance(model.get("model_id"), str):
            raise SystemExit("scientific execution-map models must have string model IDs")
        model_id = model["model_id"]
        if model_id in seen_models:
            raise SystemExit(f"scientific execution map repeats {model_id}")
        seen_models.add(model_id)
        profile = profiles.get(model_id)
        identity = derived_identities.get(model_id)
        if profile is None or identity is None:
            raise SystemExit(f"execution-map model {model_id} has no complete profile identity")
        _replace(
            model,
            "execution_identity_sha256",
            identity,
            f"{model_id} execution-map identity",
            drifted,
        )
        mapped_profiles.append(profile)

    execution_map_sha256 = hashlib.sha256(_helm_to_json_bytes(execution_map)).hexdigest()
    for profile in mapped_profiles:
        model_id = profile["model_id"]
        qualification = profile.get("qualification")
        if not isinstance(qualification, dict):
            raise SystemExit(f"execution-map model {model_id} has no qualification contract")
        _replace(
            qualification,
            "execution_map_sha256",
            execution_map_sha256,
            f"{model_id} qualification execution_map_sha256",
            drifted,
        )
    return drifted


def _stage_payload(path: Path, payload: bytes, purpose: str) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.{purpose}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _atomic_write(payloads: dict[Path, bytes]) -> None:
    """Stage all bytes and roll back completed replacements on an error."""

    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    replaced: list[Path] = []
    retained: set[Path] = set()
    try:
        for path, payload in payloads.items():
            staged[path] = _stage_payload(path, payload, "new")
        for path in payloads:
            backups[path] = _stage_payload(path, path.read_bytes(), "rollback")
        try:
            for path, temporary_path in staged.items():
                os.replace(temporary_path, path)
                replaced.append(path)
        except BaseException as replacement_error:
            rollback_errors: list[str] = []
            for path in reversed(replaced):
                backup = backups[path]
                try:
                    os.replace(backup, path)
                except OSError as rollback_error:
                    retained.add(backup)
                    rollback_errors.append(f"{path}: {rollback_error}")
            if rollback_errors:
                locations = ", ".join(str(path) for path in sorted(retained))
                detail = "; ".join(rollback_errors)
                raise RuntimeError(
                    f"contract update failed and rollback was incomplete ({detail}); backups retained at {locations}"
                ) from replacement_error
            raise
    finally:
        for temporary_path in (*staged.values(), *backups.values()):
            if temporary_path not in retained:
                temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift instead of rewriting")
    options = parser.parse_args(argv)

    profile_document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    execution_map = json.loads(EXECUTION_MAP_PATH.read_text(encoding="utf-8"))
    if not isinstance(profile_document, dict) or not isinstance(execution_map, dict):
        raise SystemExit("scientific profile and execution-map contracts must be JSON objects")
    drifted = _derive_contracts(
        profile_document,
        execution_map,
        solution_root=SOLUTION_ROOT,
        recipe_digest=runtime_recipe_sha256,
    )

    if not drifted:
        print("scientific runtime recipes are current")
        return 0
    if options.check:
        for row in drifted:
            print(f"drift: {row}")
        print("run scripts/refresh_scientific_recipes.py to update them")
        return 1
    _atomic_write(
        {
            PROFILE_PATH: (json.dumps(profile_document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            EXECUTION_MAP_PATH: (json.dumps(execution_map, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        }
    )
    for row in drifted:
        print(f"updated {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
